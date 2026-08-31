"""
チャレンジ 1: エージェントの構築 — SDK トラック
NovaTel Communications 向けの意図分類エージェント（Intent Classification Agent）および解決アドバイザーエージェント（Resolution Advisor Agent）。

使用方法:
    python agents.py

システムプロンプト、ツール、会話処理を備えた両エージェントを構築します。
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


# 親ディレクトリ内の .env を検索してリポジトリのルートパスを特定します。
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
CALL_DATA_PATH = Path(__file__).resolve().parent / "call_data_jp.json"


def _load_call_batch() -> list[dict]:
    """デモ要求でバッチペイロードとして送信する通話記録を読み込みます。"""
    with open(CALL_DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data.get("calls", [])


# =============================================================================
# ツール関数: lookup_customer
# これは既に実装されています — エージェントはこれを呼び出して顧客コンテキストを取得できます
# =============================================================================

def lookup_customer(call_id: str) -> str:
    """
    call_data.json を読み込み、指定された通話の完全なコンテキストを返します:
    顧客情報、アカウント詳細、通話要約、および履歴。
    """
    with open(CALL_DATA_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)

    call = None
    for c in data["calls"]:
        if c["call_id"] == call_id or c["customer_id"] == call_id:
            call = c
            break

    if not call:
        return json.dumps({"error": f"通話または顧客 '{call_id}' が見つかりません"}, ensure_ascii=False)

    return json.dumps({
        "call_id": call["call_id"],
        "customer_id": call["customer_id"],
        "customer_name": call["customer_name"],
        "account_tier": call["account_tier"],
        "tenure_months": call["tenure_months"],
        "summary": call["summary"],
        "transcript_snippet": call["transcript_snippet"],
        "open_tickets": call["open_tickets"],
        "last_interaction": call["last_interaction"],
        "status": call["status"],
    }, indent=2, ensure_ascii=False)


# エージェント用ツール定義 (Foundry FunctionTool 形式)
LOOKUP_CUSTOMER_TOOL = FunctionTool(
    name="lookup_customer",
    description="通話ID（例: 'CALL-001'）または顧客ID（例: 'CUST-4421'）を使用して、顧客および通話の詳細を検索します。アカウント階層、契約月数、通話要約、文字起こし、および対応履歴を返します。",
    parameters={
        "type": "object",
        "properties": {
            "call_id": {
                "type": "string",
                "description": "検索する通話ID（例: 'CALL-001'）または顧客ID（例: 'CUST-4421'）",
            }
        },
        "required": ["call_id"],
        "additionalProperties": False,
    },
    strict=False,
)


# =============================================================================
# 意図分類エージェント（Intent Classification Agent）
# =============================================================================

class IntentClassificationAgent:
    def __init__(self):
        self.agent = None
        self.client = None
        self.openai = None

    def create(self):
        """Foundry 内に意図分類エージェントを作成します。"""
        self.client = AIProjectClient(
            endpoint=PROJECT_CONNECTION_STRING,
            credential=DefaultAzureCredential(),
        )
        self.openai = self.client.get_openai_client()

        system_prompt = """
        あなたは NovaTel Communications のコールセンターにおける意図分類スペシャリストです。
        通話の分類を求められたら、lookup_customer ツールを使用して通話の詳細を取得してください。
        各通話について、以下を判定してください:

        1. 主な意図（PRIMARY INTENT） — 以下のいずれか:
            - billing_dispute: 身に覚えのない請求、返金リクエスト、請求エラー
            - technical_issue: サービス障害、接続の問題、端末の不具合
            - cancellation: 解約希望、または他社への乗り換えを示唆している
            - upsell_opportunity: サービスの追加、アップグレード、または拡張を希望している
            - account_support: 一般的な質問、アプリの操作ヘルプ、ナビゲーションの問題
            - security_concern: 不正利用、未承認のアクセス、不審なアクティビティ
        2. 優先度（PRIORITY） — critical（緊急） / high（高） / medium（中） / low（低）
        3. 感情（SENTIMENT） — frustrated（苛立ち・不満） / neutral（中立） / positive（好意的） / anxious（不安）
        4. 解約リスク（RETENTION RISK） — high（高） / medium（中） / low（低）（顧客が解約・他社移転する可能性）

        回答は、各通話に対する構造化された分類結果として整形してください。
        緊急（critical）には 🔴、高（high）には ⚠️、低リスク項目には ✅ を使用してください。
        """

        self.agent = self.client.agents.create_version(
            agent_name="intent-classification-agent",
            definition=PromptAgentDefinition(
                model=MODEL_DEPLOYMENT_NAME,
                instructions=system_prompt,
                tools=[LOOKUP_CUSTOMER_TOOL],
            ),
        )

        return self.agent

    def run(self, input_text: str) -> str:
        """指定された入力で意図分類エージェントを実行します。"""
        conversation = self.openai.conversations.create()

        response = self.openai.responses.create(
            input=input_text,
            conversation=conversation.id,
            extra_body={"agent_reference": {"name": self.agent.name, "type": "agent_reference"}},
        )

        # 関数呼び出しのループ処理
        while True:
            function_calls = [item for item in response.output if item.type == "function_call"]
            if not function_calls:
                break

            input_list = []
            for item in function_calls:
                if item.name == "lookup_customer":
                    args = json.loads(item.arguments)
                    result = lookup_customer(args["call_id"])
                else:
                    result = json.dumps({"error": f"不明なツール '{item.name}' です"}, ensure_ascii=False)

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
        """エージェントバージョンを削除し、接続を閉じます。"""
        if self.agent:
            self.client.agents.delete_version(
                agent_name=self.agent.name,
                agent_version=self.agent.version,
            )
        if self.client:
            self.client.close()


# =============================================================================
# 解決アドバイザーエージェント（Resolution Advisor Agent）
# =============================================================================

class ResolutionAdvisorAgent:
    def __init__(self):
        self.agent = None
        self.client = None
        self.openai = None

    def create(self):
        """Foundry 内に解決アドバイザーエージェントを作成します。"""
        self.client = AIProjectClient(
            endpoint=PROJECT_CONNECTION_STRING,
            credential=DefaultAzureCredential(),
        )
        self.openai = self.client.get_openai_client()

        system_prompt = """
        あなたは NovaTel Communications コールセンターの解決戦略エキスパートです。
        分類された通話の意図と顧客コンテキストに基づき、最適な解決プロセスを提案してください。

        提案には以下を考慮する必要があります:
        - アカウント階層（プレミアム/ビジネスは優先対応および柔軟な対応）
        - 顧客の契約月数（長年の顧客にはリテンションオファー）
        - 未解決チケット（既存の問題がある場合は重複問い合わせを示すため — エスカレーション）
        - 感情および解約リスク

        各通話に対して、以下を提供してください:
        1. 推奨される対応案（RECOMMENDED ACTION） — 担当者が提示すべき具体的なステップ
        2. スクリプト提案（SCRIPT SUGGESTION） — 顧客に伝えるべき言葉（1〜2文）
        3. エスカレーション（ESCALATION） — はい/いいえ およびその理由
        4. 適用可能なオファー（OFFERS AVAILABLE） — 案内可能な返金・クレジット、割引、リテンションオファー
        5. フォローアップ（FOLLOW-UP） — 通話後のタスク（チケット作成、折り返し連絡のスケジュールなど）

        意図別の対応ガイドライン:
        - billing_dispute: 請求内容を確認。100ドル未満の場合は即時返金/クレジットを提供、それ以上の場合はエスカレーション
        - technical_issue: まず既知の障害を確認。継続している場合は技術者を派遣
        - cancellation: リテンションパッケージ（割引/1ヶ月無料など）を提案。法人アカウントの場合はエスカレーション
        - upsell_opportunity: 削減額を計算し、セット割引を提案。フォローアップを予定
        - account_support: 解決手順を案内。複雑な場合は折り返し連絡を提案
        - security_concern: 必ずセキュリティチームへエスカレーションし、即座にアカウントをロック

        簡潔かつ実践的に記述し、見出しを使って分かりやすく構造化してください。
        """

        self.agent = self.client.agents.create_version(
            agent_name="resolution-advisor-agent",
            definition=PromptAgentDefinition(
                model=MODEL_DEPLOYMENT_NAME,
                instructions=system_prompt,
            ),
        )

        return self.agent

    def run(self, input_text: str) -> str:
        """指定された入力で解決アドバイザーエージェントを実行します。"""
        conversation = self.openai.conversations.create()

        response = self.openai.responses.create(
            input=input_text,
            conversation=conversation.id,
            extra_body={"agent_reference": {"name": self.agent.name, "type": "agent_reference"}},
        )

        self.openai.conversations.delete(conversation_id=conversation.id)
        return response.output_text

    def cleanup(self):
        """エージェントバージョンを削除し、接続を閉じます。"""
        if self.agent:
            self.client.agents.delete_version(
                agent_name=self.agent.name,
                agent_version=self.agent.version,
            )
        if self.client:
            self.client.close()


# =============================================================================
# メイン — 両エージェントのテスト
# =============================================================================

def main():
    if not PROJECT_CONNECTION_STRING:
        print("❌ PROJECT_CONNECTION_STRING が設定されていません。先にチャレンジ 0 を実行してください！")
        sys.exit(1)

    print("=== 意図分類エージェント ===")
    print("エージェントを作成中...")

    intent_agent = IntentClassificationAgent()
    intent_agent.create()
    print(f"✅ 作成完了: {intent_agent.agent.name} (バージョン {intent_agent.agent.version})")

    print("\nすべての入電を分類中...")
    call_batch = _load_call_batch()
    call_ids = [call["call_id"] for call in call_batch]
    intent_result = intent_agent.run(
        "一度の実行で分類するための通話バッチペイロードを受け取っています。"
        "ペイロード内の各 call_id に対して lookup_customer を使用し、分類結果を出力してください。\n\n"
        f"BATCH_CALL_IDS: {json.dumps(call_ids)}\n"
        "BATCH_CALL_DATA:\n"
        f"{json.dumps(call_batch, indent=2, ensure_ascii=False)}"
    )
    print(intent_result)

    print("\n=== 解決アドバイザーエージェント ===")
    print("エージェントを作成中...")

    resolution_agent = ResolutionAdvisorAgent()
    resolution_agent.create()
    print(f"✅ 作成完了: {resolution_agent.agent.name} (バージョン {resolution_agent.agent.version})")

    print("\n高優先度の通話バッチに対してアドバイスを作成中...")
    high_priority_batch = [
        call for call in call_batch if call["call_id"] in {"CALL-001", "CALL-006", "CALL-007"}
    ]
    resolution_result = resolution_agent.run(
        "高優先度の通話バッチペイロードを受け取っています。各通話について、以下を提供してください: "
        "推奨される対応案、スクリプト提案、エスカレーションの判断、適用可能なオファー、フォローアップの手順。\n\n"
        "HIGH_PRIORITY_CALL_BATCH:\n"
        f"{json.dumps(high_priority_batch, indent=2, ensure_ascii=False)}"
    )
    print(resolution_result)

    # クリーンアップ — Foundry ポータル上でエージェントを確認・維持したい場合はコメントアウトしたままにしてください
    # print("\nエージェントを削除中...")
    # intent_agent.cleanup()
    # resolution_agent.cleanup()
    # print("✅ 完了しました！")


if __name__ == "__main__":
    main()