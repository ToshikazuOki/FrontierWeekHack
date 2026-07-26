"""
Challenge 4: Production Workflow -- Microsoft Agent Framework Track
Multi-agent orchestration workflow for TireForge Industries using Microsoft Agent Framework.
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# Microsoft Agent Framework 関連ライブラリ
from agent_framework import Agent, Workflow, tool
from agent_framework_foundry import FoundryAgent, FoundryChatClient
from azure.identity import DefaultAzureCredential


# ---------------------------------------------------------
# 1. 設定 & ユーティリティ
# ---------------------------------------------------------
def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists():
            return parent
    return Path(__file__).resolve().parents[2]


env_path = _find_repo_root() / ".env"
load_dotenv(env_path)

PROJECT_CONNECTION_STRING = os.getenv("PROJECT_CONNECTION_STRING")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-5.4")
SENSOR_DATA_PATH = (
    Path(__file__).resolve().parent.parent / "challenge-1-build" / "sensor_data.json"
)
MACHINES = ["MX-001", "EX-002", "CP-003", "CU-004", "IS-005"]


# ---------------------------------------------------------
# 2. ツール定義 (@tool デコレータでシンプル化)
# ---------------------------------------------------------
@tool(description="Check sensor readings against thresholds for a given machine.")
def check_thresholds(machine_id: str) -> str:
    """指定されたマシンIDのセンサーデータと閾値をチェックして異常を返します。"""
    if not SENSOR_DATA_PATH.exists():
        return json.dumps({"error": f"File not found: {SENSOR_DATA_PATH}"})

    with open(SENSOR_DATA_PATH, "r") as f:
        data = json.load(f)

    machine = next(
        (
            m
            for m in data["machines"]
            if m["machine_id"] == machine_id or m["name"] == machine_id
        ),
        None,
    )
    if not machine:
        return json.dumps({"error": f"Machine not found: {machine_id}"})

    results = {
        "machine_id": machine["machine_id"],
        "name": machine["name"],
        "status": machine["status"],
        "anomalies": [],
        "all_readings": {},
    }

    for sensor, reading in machine["readings"].items():
        value = reading["value"]
        threshold = machine["thresholds"][sensor]
        in_spec = threshold["min"] <= value <= threshold["max"]

        results["all_readings"][sensor] = {
            "value": value,
            "unit": reading["unit"],
            "min": threshold["min"],
            "max": threshold["max"],
            "in_spec": in_spec,
        }

        if not in_spec:
            direction = "above max" if value > threshold["max"] else "below min"
            ref = threshold["max"] if value > threshold["max"] else threshold["min"]
            pct = abs(value - ref) / ref * 100
            results["anomalies"].append(
                {
                    "sensor": sensor,
                    "value": value,
                    "unit": reading["unit"],
                    "deviation": f"{pct:.1f}% {direction}",
                }
            )

    return json.dumps(results, indent=2)


# ---------------------------------------------------------
# 3. エージェントの生成・初期化
# ---------------------------------------------------------
def create_agents():
    """Agent Framework の Agent オブジェクトを作成します。"""
    client = FoundryChatClient(
        project_endpoint=PROJECT_CONNECTION_STRING,
        model=MODEL_DEPLOYMENT_NAME,
        credential=DefaultAzureCredential(),
    )

    # 1. 異常検知エージェント
    anomaly_agent = Agent(
        client=client,
        name="anomaly-detection-agent",
        instructions=(
            """
            あなたは TireForge Industries の産業用センサー異常検知のエキスパートです。
            マシンのチェックを求められたら、各マシンに対して check_thresholds ツールを使用してください。
            各マシンについて、以下を報告してください:
            - マシン名と ID
            - ステータス（正常 / 警告 / 危険）
            - 規格外となっている各センサーの測定値: 現在値、違反したしきい値、乖離幅
            警告には ⚠️ を、危険な異常には 🔴 を使用してください。
            すべての測定値が規格内である場合は、マシンを「正常」とマークしてください。
            簡潔かつ構造化された形式で出力してください。
            """
        ),
        tools=[check_thresholds],  # 関数をそのまま渡すだけ
    )

    # 2. 原因診断エージェント
    diagnosis_agent = Agent(
        client=client,
        name="fault-diagnosis-agent",
        instructions=(
            """
            あなたは TireForge Industries の機械障害診断のエキスパートです。
            マシンから検出されたセンサー異常のリストが与えられたら、以下の対応を行ってください:
            1. 異常のパターンに基づいて、最も可能性の高い根本原因を特定する:
            - 高温 + 高圧 → 閉塞または流路制限の可能性
            - 高振動のみ → 軸受の磨耗、芯ブレ、またはアンバランスの可能性
            - 高温 + 高振動 → 軸受の損傷または潤滑不良の可能性
            - 複数のセンサーが危険値 → 複合障害。直ちにエスカレーションすること
            2. 具体的で実行可能なメンテナンス手順を推奨する。
            3. 緊急度を推定する: 即時（今すぐ停止）、24時間以内、または監視。
            簡潔に記述してください。回答は以下のフォーマットで出力してください:
            考えられる原因: ...
            メンテナンス手順: ...
            緊急度: ...
            """
        ),
    )

    return anomaly_agent, diagnosis_agent


# ---------------------------------------------------------
# 4. マルチエージェント オーケストレーション (Workflow)
# ---------------------------------------------------------
async def run_factory_pipeline():
    anomaly_agent, diagnosis_agent = create_agents()

    print("=== Step 1: Anomaly Scanning ===")
    prompt = f"すべてのマシン ({', '.join(MACHINES)}) をチェックし、規格外のセンサー測定値を報告してください。"

    # 1. 異常検知エージェントを実行（ツールの呼び出し含む）
    anomaly_response = await anomaly_agent.run(prompt)
    print("\n[Anomaly Report]")
    print(anomaly_response.text)

    print("\n=== Step 2: Fault Diagnosis ===")
    # 2. Step 1 の結果をそのまま原因診断エージェントに入力
    diagnosis_prompt = (
        f"以下の異常報告に基づいて原因を診断し、対応策を提示してください:\n\n"
        f"{anomaly_response.text}"
    )
    diagnosis_response = await diagnosis_agent.run(diagnosis_prompt)

    print("\n[Diagnosis Output]")
    print(diagnosis_response.text)


# ---------------------------------------------------------
# 5. エントリーポイント
# ---------------------------------------------------------
if __name__ == "__main__":
    if not PROJECT_CONNECTION_STRING:
        print("PROJECT_CONNECTION_STRING not set.")
        sys.exit(1)

    # Agent Framework は async/await ベースのため asyncio で実行
    asyncio.run(run_factory_pipeline())