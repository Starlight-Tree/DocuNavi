from typing import Annotated, Any, TypedDict

from langgraph.graph.message import add_messages


class AgentState(TypedDict):
    """LangGraph エージェントの状態。"""

    # 会話履歴。add_messages により、ノードが返したメッセージは追記される
    messages: Annotated[list, add_messages]
    # 直近の RAG 検索で取得した設定テキスト（検索していなければ空文字）
    context: str
    # context の見出しと同じ順の出典（{"name", "page", "page_end"}）。ページは LLM には渡さず、画面だけが使う
    sources: list[dict[str, Any]]
    # 入力ガードが今ターンの発言を拒否した理由コード（通過なら None）。毎ターン guard_input が上書きする
    guard_reason: str | None
    # 今ターンの先の LLM 呼び出しが、フォールバックのモデルで答えたか。以降の呼び出しも、主モデルの失敗を
    # また待たずにフォールバックから始めるための記憶。毎ターン guard_input が False に戻す
    fallback_used: bool
