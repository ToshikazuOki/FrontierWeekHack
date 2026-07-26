"""
Challenge 1: Build Agents — SDK Track
TireForge Industries 向け異常検知エージェントおよび障害診断エージェント

使用方法:
    python agents.py

システムプロンプト、ツール、および会話処理を備えた両方のエージェントを構築
"""

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from azure.ai.projects import AIProjectClient
from azure.ai.projects.models import FunctionTool, PromptAgentDefinition
from azure.identity import DefaultAzureCredential
from openai.types.responses.response_input_param import FunctionCallOutput


# リポジトリルートを親ディレクトリから .env を探索して特定
def _find_repo_root() -> Path:
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists():
            return parent
    return Path(__file__).resolve().parents[2]


REPO_ROOT = _find_repo_root()

# 環境変数の読み込み
env_path = REPO_ROOT / ".env"
load_dotenv(env_path)

PROJECT_CONNECTION_STRING = os.getenv("PROJECT_CONNECTION_STRING")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-5.4")
SENSOR_DATA_PATH = Path(__file__).resolve().parent / "sensor_data.json"


def _load_sensor_batch() -> list[dict]:
    """デモ用リクエストでバッチペイロードとして送信するマシンデータを読み込み"""
    with open(SENSOR_DATA_PATH, "r") as f:
        data = json.load(f)
    return data.get("machines", [])


# =============================================================================
# Tool Function: check_thresholds
# これは実装済みです — エージェントはこれを呼び出してしきい値分析を取得できます
# =============================================================================

def check_thresholds(machine_id: str) -> str:
    """
    sensor_data.json を読み込み、マシンの測定値がしきい値内にあるかチェック
    分析結果を JSON 文字列として返却
    """
    with open(SENSOR_DATA_PATH, "r") as f:
        data = json.load(f)

    machine = None
    for m in data["machines"]:
        if m["machine_id"] == machine_id or m["name"] == machine_id:
            machine = m
            break

    if not machine:
        return json.dumps({"error": f"Machine '{machine_id}' not found"})

    results = {
        "machine_id": machine["machine_id"],
        "name": machine["name"],
        "status": machine["status"],
        "last_maintenance": machine["last_maintenance"],
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
            deviation = ""
            if value > threshold["max"]:
                pct = ((value - threshold["max"]) / threshold["max"]) * 100
                deviation = f"{pct:.1f}% above max"
            elif value < threshold["min"]:
                pct = ((threshold["min"] - value) / threshold["min"]) * 100
                deviation = f"{pct:.1f}% below min"

            results["anomalies"].append({
                "sensor": sensor,
                "value": value,
                "unit": reading["unit"],
                "threshold_min": threshold["min"],
                "threshold_max": threshold["max"],
                "deviation": deviation,
            })

    return json.dumps(results, indent=2)


# エージェント用ツールの定義（Foundry FunctionTool 形式）
CHECK_THRESHOLDS_TOOL = FunctionTool(
    name="check_thresholds",
    description="マシンのセンサー測定値が正常な運用しきい値内にあるか確認。規格外の測定値がある場合は異常情報を返却",
    parameters={
        "type": "object",
        "properties": {
            "machine_id": {
                "type": "string",
                "description": "確認対象のマシン ID（例: 'MX-001'）または名称（例: 'mixer'）",
            }
        },
        "required": ["machine_id"],
        "additionalProperties": False,
    },
    strict=False,
)


# =============================================================================
# Anomaly Detection Agent
# =============================================================================

class AnomalyDetectionAgent:
    def __init__(self):
        self.agent = None
        self.client = None
        self.openai = None

    def create(self):
        """Foundry 内に異常検知エージェントを作成"""
        self.client = AIProjectClient(
            endpoint=PROJECT_CONNECTION_STRING,
            credential=DefaultAzureCredential(),
        )
        self.openai = self.client.get_openai_client()

        system_prompt = """
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

        self.agent = self.client.agents.create_version(
            agent_name="anomaly-detection-agent",
            definition=PromptAgentDefinition(
                model=MODEL_DEPLOYMENT_NAME,
                instructions=system_prompt,
                tools=[CHECK_THRESHOLDS_TOOL],
            ),
        )

        return self.agent

    def run(self, input_text: str) -> str:
        """指定された入力で異常検知エージェントを実行"""
        conversation = self.openai.conversations.create()

        response = self.openai.responses.create(
            input=input_text,
            conversation=conversation.id,
            extra_body={"agent_reference": {"name": self.agent.name, "type": "agent_reference"}},
        )

        # 関数の呼び出しループを処理
        while True:
            function_calls = [item for item in response.output if item.type == "function_call"]
            if not function_calls:
                break

            input_list = []
            for item in function_calls:
                if item.name == "check_thresholds":
                    args = json.loads(item.arguments)
                    result = check_thresholds(args["machine_id"])
                else:
                    result = json.dumps({"error": f"Unknown tool '{item.name}'"})

                input_list.append(
                    FunctionCallOutput(
                        type="function_call_output",
                        call_id=item.call_id,
                        output=result,
                    )
                )

            response = self.openai.responses.create(
                input=input_list,
                conversation=conversation.id,
                extra_body={"agent_reference": {"name": self.agent.name, "type": "agent_reference"}},
            )

        self.openai.conversations.delete(conversation_id=conversation.id)
        return response.output_text

    def cleanup(self):
        """エージェントバージョンを削除し、接続を削除"""
        if self.agent:
            self.client.agents.delete_version(
                agent_name=self.agent.name,
                agent_version=self.agent.version,
            )
        if self.client:
            self.client.close()


# =============================================================================
# Fault Diagnosis Agent
# =============================================================================

class FaultDiagnosisAgent:
    def __init__(self):
        self.agent = None
        self.client = None
        self.openai = None

    def create(self):
        """Foundry 内に障害診断エージェントを作成"""
        self.client = AIProjectClient(
            endpoint=PROJECT_CONNECTION_STRING,
            credential=DefaultAzureCredential(),
        )
        self.openai = self.client.get_openai_client()

        system_prompt = """
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

        self.agent = self.client.agents.create_version(
            agent_name="fault-diagnosis-agent",
            definition=PromptAgentDefinition(
                model=MODEL_DEPLOYMENT_NAME,
                instructions=system_prompt,
            ),
        )

        return self.agent

    def run(self, input_text: str) -> str:
        """指定された入力で障害診断エージェントを実行"""
        conversation = self.openai.conversations.create()

        response = self.openai.responses.create(
            input=input_text,
            conversation=conversation.id,
            extra_body={"agent_reference": {"name": self.agent.name, "type": "agent_reference"}},
        )

        self.openai.conversations.delete(conversation_id=conversation.id)
        return response.output_text

    def cleanup(self):
        """エージェントバージョンを削除し、接続を削除"""
        if self.agent:
            self.client.agents.delete_version(
                agent_name=self.agent.name,
                agent_version=self.agent.version,
            )
        if self.client:
            self.client.close()


# =============================================================================
# Main — 両方のエージェントをテスト
# =============================================================================

def main():
    if not PROJECT_CONNECTION_STRING:
        print("❌ PROJECT_CONNECTION_STRING が設定されていません。最初に Challenge 0 を実行してください")
        sys.exit(1)

    print("=== 異常検知エージェント ===")
    print("エージェントを作成中...")

    anomaly_agent = AnomalyDetectionAgent()
    anomaly_agent.create()
    print(f"✅ 作成完了: {anomaly_agent.agent.name} (バージョン {anomaly_agent.agent.version})")

    print("\nすべてのマシンを分析中...")
    machine_batch = _load_sensor_batch()
    machine_ids = [machine["machine_id"] for machine in machine_batch]
    anomaly_result = anomaly_agent.run(
        "1回の実行で処理する必要があるマシンのバッチペイロードを受信しています。"
        "ペイロード内の各 machine_id に対して check_thresholds を使用し、異常サマリーを返してください。\n\n"
        f"BATCH_MACHINE_IDS: {json.dumps(machine_ids)}\n"
        "BATCH_MACHINE_DATA:\n"
        f"{json.dumps(machine_batch, indent=2)}"
    )
    print(anomaly_result)

    print("\n=== 障害診断エージェント ===")
    print("エージェントを作成中...")

    diagnosis_agent = FaultDiagnosisAgent()
    diagnosis_agent.create()
    print(f"✅ 作成完了: {diagnosis_agent.agent.name} (バージョン {diagnosis_agent.agent.version})")

    print("\n危険なマシンを診断中: curing_press...")
    critical_batch = [machine for machine in machine_batch if machine["status"] in {"critical", "warning"}]
    diagnosis_result = diagnosis_agent.run(
        "以下の危険マシンのバッチを診断し、各項目に対して根本原因、"
        "メンテナンス手順、および緊急度を提示してください。\n\n"
        "CRITICAL_MACHINE_BATCH:\n"
        f"{json.dumps(critical_batch, indent=2)}"
    )
    print(diagnosis_result)

    # クリーンアップ — Foundry ポータルでエージェントを確認可能にする場合はコメントアウトのままにします
    # print("\nエージェントをクリーンアップ中...")
    # anomaly_agent.cleanup()
    # diagnosis_agent.cleanup()
    # print("✅ 完了")


if __name__ == "__main__":
    main()