from typing import Annotated, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """LangGraph エージェントの状態。"""

    # 会話履歴。add_messages により、ノードが返したメッセージは追記される
    messages: Annotated[list, add_messages]
    # 直近の RAG 検索で取得した設定テキスト（検索していなければ空文字）
    context: str
    # 入力ガードが今ターンの発言を拒否した理由コード（通過なら None）。毎ターン guard_input が上書きする
    guard_reason: str | None
