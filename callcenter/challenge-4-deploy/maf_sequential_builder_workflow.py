"""
チャレンジ 4: プロダクション ワークフロー — Microsoft Agent Framework トラック
NovaTel Communications コールセンター向けのマルチエージェント オーケストレーション ワークフロー。
(SequentialBuilder / Sequential Workflow パターン適用版)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from agent_framework import Agent, Workflow, tool
from agent_framework.orchestrations import SequentialBuilder
from agent_framework_foundry import FoundryAgent, FoundryChatClient
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv


# ---------------------------------------------------------
# 1. 設定 & ユーティリティ
# ---------------------------------------------------------
def _find_repo_root() -> Path:
    """親ディレクトリ内の .env を検索してリポジトリのルートパスを特定します。"""
    for parent in Path(__file__).resolve().parents:
        if (parent / ".env").exists():
            return parent
    return Path(__file__).resolve().parents[2]


env_path = _find_repo_root() / ".env"
load_dotenv(env_path)

PROJECT_CONNECTION_STRING = os.getenv("PROJECT_CONNECTION_STRING")
MODEL_DEPLOYMENT_NAME = os.getenv("MODEL_DEPLOYMENT_NAME", "gpt-5.4")
CALL_DATA_PATH = (
    Path(__file__).resolve().parent.parent / "challenge-1-build" / "call_data_jp.json"
)


# ---------------------------------------------------------
# 2. ツール定義 (@tool デコレータによるシンプル化)
# ---------------------------------------------------------
@tool(description="Look up call and customer details by call ID or customer ID.")
def lookup_customer(call_id: str) -> str:
    """
    call_data_jp.json から通話および顧客の詳細情報を検索して返します。

    :param call_id: 通話ID (例: 'CALL-001') または 顧客ID (例: 'CUST-4421')
    """
    if not CALL_DATA_PATH.exists():
        return json.dumps(
            {"error": f"データファイルが見つかりません: {CALL_DATA_PATH}"},
            ensure_ascii=False,
        )

    with open(CALL_DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    call = next(
        (
            c
            for c in data.get("calls", [])
            if c.get("call_id") == call_id or c.get("customer_id") == call_id
        ),
        None,
    )
    if not call:
        return json.dumps(
            {"error": f"通話または顧客が見つかりません: {call_id}"}, ensure_ascii=False
        )

    return json.dumps(call, indent=2, ensure_ascii=False)


# ---------------------------------------------------------
# 3. エージェントの生成・初期化
# ---------------------------------------------------------
def create_agents():
    """FoundryChatClient と Agent クラスを用いてエージェントオブジェクトを生成します。"""
    client = FoundryChatClient(
        project_endpoint=PROJECT_CONNECTION_STRING,
        model=MODEL_DEPLOYMENT_NAME,
        credential=DefaultAzureCredential(),
    )

    # 1. 意図分類エージェント
    intent_agent = Agent(
        client=client,
        name="intent-classification-agent",
        instructions=(
            """
            あなたは NovaTel Communications のコールセンターにおける意図分類スペシャリストです。
            与えられた通話IDまたは全入電データについて、lookup_customer ツールを使用して必要な詳細情報を取得してください。

            各通話について、以下の項目を判定・分類して結果を出力してください:
            1. 通話ID (call_id)
            2. 意図 (intent): billing_dispute / technical_issue / cancellation / upsell_opportunity / account_support / security_concern
            3. 優先度 (priority): critical / high / medium / low
            4. 感情 (sentiment): frustrated / neutral / positive / anxious
            5. 解約リスク (retention_risk): high / medium / low
            """
        ),
        tools=[lookup_customer],
    )

    # 2. 解決アドバイザーエージェント
    resolution_agent = Agent(
        client=client,
        name="resolution-advisor-agent",
        instructions=(
            """
            あなたは NovaTel Communications の解決戦略エキスパートです。
            前段の分類結果と顧客コンテキストに基づき、最適な解決方法を推奨してください。
            必要に応じて lookup_customer ツールで顧客の詳細情報を補完取得してください。

            以下の項目を含めて出力してください:
            - 推奨される対応案 (RECOMMENDED ACTION)
            - スクリプト提案 (SCRIPT SUGGESTION)
            - エスカレーションの判断 (ESCALATION: Yes/No + 理由)
            - 適用可能なオファー (OFFERS AVAILABLE)
            - フォローアップタスク (FOLLOW-UP)

            【運用ルール】
            - セキュリティ懸念 (security_concern) は【必ず】エスカレーションしてください。
            - 法人アカウント (business) を優先対応してください。
            - 長期契約顧客にはリテンションオファーを提示してください。
            """
        ),
        tools=[lookup_customer],
    )

    return intent_agent, resolution_agent


# ---------------------------------------------------------
# 4. SequentialBuilder によるワークフロー構築と実行
# ---------------------------------------------------------
def build_sequential_workflow(
    intent_agent: Agent, resolution_agent: Agent
) -> Workflow:
    """SequentialBuilder に participants 引数を渡してワークフローを構築します。"""
    workflow = SequentialBuilder(
        participants=[intent_agent, resolution_agent]
    ).build()

    return workflow


async def run_call_center_pipeline():
    """Sequential ワークフローを用いてコールセンターのパイプライン処理を実行します。"""
    intent_agent, resolution_agent = create_agents()

    # SequentialBuilder によるパイプラインの作成
    workflow = build_sequential_workflow(intent_agent, resolution_agent)

    print("=== ステップ 1: 通話データの読み込みと Sequential ワークフローの準備 ===")
    with open(CALL_DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    calls = data.get("calls", [])
    call_ids = [c["call_id"] for c in calls]

    prompt = (
        f"以下の入電コールを処理してください: {', '.join(call_ids)}。\n"
        "まず、各通話の意図 (intent)、優先度 (priority)、感情 (sentiment)、解約リスク (retention_risk) を分類・整理してください。\n"
        "続いて、特に緊急・高優先度 (critical / high) の通話について最適な解決戦略とスクリプトを作成してください。"
    )

    print(
        "\n 🔄 Sequential ワークフローを起動中 (Intent Agent -> Resolution Agent)..."
    )

    # ワークフローを実行（WorkflowEvent のリストが返る）
    events = await workflow.run(prompt)

    # イベントループ処理による最終回答テキストの抽出
    outputs = []

    for event in events:
        if hasattr(event, "data") and event.data:
            data = event.data

            # AgentExecutorResponse オブジェクトから AgentResponse を参照
            if hasattr(data, "agent_response") and data.agent_response:
                agent_res = data.agent_response
                if hasattr(agent_res, "text") and agent_res.text:
                    outputs.append(agent_res.text)
            elif hasattr(data, "text") and data.text:
                outputs.append(data.text)

    # 出力が取得できた場合は最後の応答（Resolution Agent の結果）を採用、できなければ生イベント出力
    final_text = outputs[-1] if outputs else str(events)

    print("\n[Sequential ワークフロー実行結果]")
    print(final_text)

    return {
        "final_output": final_text,
        "total_calls": len(calls),
        "target_calls": call_ids,
    }


def print_shift_report(report: dict):
    """シフトレポートを出力します。"""
    print("\n" + "=" * 60)
    print("NOVATEL コールセンター — シフトレポート (Sequential Workflow)")
    print("=" * 60)
    print(f"  処理対象通話数   : {report['total_calls']}")
    print(f"  対象通話ID一覧   : {', '.join(report['target_calls'])}")
    print("\n--- 最終出力結果 (マルチエージェント オーケストレーション) ---")
    print(report["final_output"])
    print("=" * 60)


# ---------------------------------------------------------
# 5. エントリーポイント
# ---------------------------------------------------------
if __name__ == "__main__":
    if not PROJECT_CONNECTION_STRING:
        print(
            "❌ PROJECT_CONNECTION_STRING が設定されていません。.env ファイルを確認してください。"
        )
        sys.exit(1)

    print("=" * 60)
    print("MICROSOFT AGENT FRAMEWORK ワークフロー (SequentialBuilder) の開始")
    print("=" * 60)

    report = asyncio.run(run_call_center_pipeline())
    print_shift_report(report)

    print("\n" + "=" * 60)
    print("チャレンジ 4 (SequentialBuilder 移植版) 完了")
    print("=" * 60)
    print("  マルチエージェント オーケストレーション (Sequential Workflow)  ✓")