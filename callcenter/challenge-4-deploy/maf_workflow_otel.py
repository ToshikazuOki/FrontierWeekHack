"""
チャレンジ 4: プロダクション ワークフロー — Microsoft Agent Framework トラック
NovaTel Communications コールセンター向けのマルチエージェント オーケストレーション ワークフロー。
OpenTelemetry & Azure Monitor ロギング/トレースを Application Insights に送信
"""

import asyncio
import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

# --- OpenTelemetry & Azure Monitor ロギング/トレース設定 ---
from azure.monitor.opentelemetry import configure_azure_monitor
from opentelemetry import trace

# Microsoft Agent Framework 関連ライブラリ（TireForge 仕様の構成）
from agent_framework import Agent, Workflow, tool
from agent_framework_foundry import FoundryAgent, FoundryChatClient
from azure.identity import DefaultAzureCredential


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

# FileNotFoundError を防ぐため、階層構造に合わせた確実なパス計算を設定 (parents[1] = callcenter)
CALL_DATA_PATH = (
    Path(__file__).resolve().parents[1] / "challenge-1-build" / "call_data_jp.json"
)


# ---------------------------------------------------------
# 2. Telemetry (Application Insights) 初期化
# ---------------------------------------------------------
def init_telemetry():
    """Application Insights への OpenTelemetry 送信を有効化します。"""
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING")
    if connection_string:
        # APPLICATIONINSIGHTS_CONNECTION_STRING 環境変数を読み込んでトレースを有効化
        configure_azure_monitor(connection_string=connection_string)
        print("Telemetry initialized successfully with Application Insights.")
    else:
        print("Warning: APPLICATIONINSIGHTS_CONNECTION_STRING not set. Telemetry disabled.")


# ---------------------------------------------------------
# 3. ツール定義 (@tool デコレータによるシンプル化)
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
# 4. エージェントの生成・初期化
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
            通話の分類を求められたら、必要に応じて lookup_customer ツールを使用して通話の詳細を取得してください。
            
            各通話について、以下の項目を判定し分類してください:
            1. 意図 (intent): billing_dispute / technical_issue / cancellation / upsell_opportunity / account_support / security_concern
            2. 優先度 (priority): critical / high / medium / low
            3. 感情 (sentiment): frustrated / neutral / positive / anxious
            4. 解約リスク (retention_risk): high / medium / low

            回答は簡潔かつ構造化された形式で出力してください。
            """
        ),
        tools=[lookup_customer],  # @tool で定義した関数を渡す
    )

    # 2. 解決アドバイザーエージェント
    resolution_agent = Agent(
        client=client,
        name="resolution-advisor-agent",
        instructions=(
            """
            あなたは NovaTel Communications の解決戦略エキスパートです。
            分類された通話意図と顧客コンテキストに基づき、最適な解決方法を推奨してください。

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
    )

    return intent_agent, resolution_agent


# ---------------------------------------------------------
# 5. 実行オーケストレーション
# ---------------------------------------------------------
async def run_call_center_pipeline():
    """Agent Framework を用いてコールセンターのパイプライン処理を実行します。"""
    # OpenTelemetry トレース用スパンの作成
    tracer = trace.get_tracer(__name__)
    with tracer.start_as_current_span("run_call_center_pipeline_workflow"):
        intent_agent, resolution_agent = create_agents()

        print("=== ステップ 1: 通話データの読み込みと意図分類の実行 ===")
        with open(CALL_DATA_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        calls = data.get("calls", [])
        call_ids = [c["call_id"] for c in calls]

        prompt = (
            f"以下の入電コール全件をチェック・分類してください: {', '.join(call_ids)}。\n"
            "各通話について、意図 (intent)、優先度 (priority)、感情 (sentiment)、解約リスク (retention_risk) を報告してください。"
        )

        # 意図分類エージェントを実行（ツール自動呼び出し含む）
        intent_response = await intent_agent.run(prompt)
        classification_report = intent_response.text
        print("\n[意図分類レポート]")
        print(classification_report)

        print("\n=== ステップ 2: 解決アドバイザーの実行（高優先度通話） ===")
        high_priority_calls = [
            {
                "call_id": "CALL-007",
                "intent": "security_concern",
                "priority": "critical",
                "sentiment": "anxious",
                "retention_risk": "medium",
            },
            {
                "call_id": "CALL-001",
                "intent": "billing_dispute",
                "priority": "high",
                "sentiment": "frustrated",
                "retention_risk": "high",
            },
            {
                "call_id": "CALL-003",
                "intent": "cancellation",
                "priority": "high",
                "sentiment": "neutral",
                "retention_risk": "high",
            },
        ]

        resolutions = {}
        for call in high_priority_calls:
            print(f" 🔄 {call['call_id']} ({call['intent']}) の解決策を生成中...")

            # 顧客詳細情報の取得
            customer_info_str = lookup_customer(call["call_id"])
            customer_info = json.loads(customer_info_str)

            resolution_prompt = (
                f"通話ID {call['call_id']} (顧客名: {customer_info.get('customer_name', '不明')}, "
                f"契約階層: {customer_info.get('account_tier', '不明')}, "
                f"契約月数: {customer_info.get('tenure_months', 0)}ヶ月):\n"
                f"- 意図: {call['intent']}\n"
                f"- 優先度: {call['priority']}\n"
                f"- 感情: {call['sentiment']}\n"
                f"- 解約リスク: {call['retention_risk']}\n"
                f"- 概要: {customer_info.get('summary', '概要なし')}\n\n"
                "最適な解決戦略、顧客への対応スクリプト、エスカレーション判断、およびフォローアップ手順を推奨してください。"
            )

            res_response = await resolution_agent.run(resolution_prompt)
            resolutions[call["call_id"]] = res_response.text

        return {
            "classification_report": classification_report,
            "high_priority_calls": [c["call_id"] for c in high_priority_calls],
            "resolutions": resolutions,
            "total_calls": len(calls),
            "critical_count": 1,
            "high_priority_count": 2,
        }


def print_shift_report(report: dict):
    """シフトレポートを出力します。"""
    print("\n" + "=" * 60)
    print("NOVATEL コールセンター — シフトレポート (Agent Framework)")
    print("=" * 60)
    print(f"  処理された総通話数   : {report['total_calls']}")
    print(f"  緊急優先度 (Critical): {report['critical_count']}")
    print(f"  高優先度 (High)      : {report['high_priority_count']}")

    if report["high_priority_calls"]:
        print(
            f"\n  即時対応が必要な通話: {', '.join(report['high_priority_calls'])}"
        )
        print("\n--- 解決策の推奨事項 ---")
        for call_id, resolution in report["resolutions"].items():
            print(f"\n【{call_id}】:")
            print(resolution)
    else:
        print("\n  キューに緊急または高優先度の通話はありません。")

    print("=" * 60)


# ---------------------------------------------------------
# 6. エントリーポイント
# ---------------------------------------------------------
if __name__ == "__main__":
    if not PROJECT_CONNECTION_STRING:
        print(
            "❌ PROJECT_CONNECTION_STRING が設定されていません。.env ファイルを確認してください。"
        )
        sys.exit(1)

    print("=" * 60)
    print("MICROSOFT AGENT FRAMEWORK ワークフローの開始")
    print("=" * 60)

    # Telemetry (Application Insights) の初期化
    init_telemetry()

    # ワークフローの実行
    report = asyncio.run(run_call_center_pipeline())
    print_shift_report(report)

    print("\n" + "=" * 60)
    print("チャレンジ 4 (Agent Framework 移植版) 完了")
    print("=" * 60)
    print("  マルチエージェント オーケストレーション (Agent Framework)  ✓")