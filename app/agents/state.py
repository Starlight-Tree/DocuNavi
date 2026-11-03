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
    # この会話の検索対象（資料のグループ。`documents.collection`）。None は「すべて」。会話の最初の発言で決まり、チェックポイントに
    # 残る（API が違う値を 409 で断る）ので、既存の会話を開いても履歴から復元される。この項目が無い古い会話は「すべて」
    collection: str | None
    # この会話の質問対象（資料の「対象」。`documents.target`）。None は「対象を絞らない」。`collection` と同じく最初の発言で決まり、
    # チェックポイントに残る。固定するのは対象の識別子だけで、資料の集合ではない（続きの会話は、そのときの分類で探す）。
    # この項目が無い古い会話は None。`collection` と同時には持たない（API が両方の指定を断る）
    target: str | None
    # 今ターンの検索で、しきい値を超えて拾ったチャンクの数（検索していなければ None）。画面が「該当する資料がありません」を、
    # 検索対象の指定があって 0 件のときだけ出すための値で、検索の失敗（エラー）とは別。毎ターン API が None に戻す
    hit_count: int | None
    # 入力ガードが今ターンの発言を拒否した理由コード（通過なら None）。毎ターン guard_input が上書きする
    guard_reason: str | None
    # 今ターンの先の LLM 呼び出しが、フォールバックのモデルで答えたか。以降の呼び出しも、主モデルの失敗を
    # また待たずにフォールバックから始めるための記憶。毎ターン guard_input が False に戻す
    fallback_used: bool
