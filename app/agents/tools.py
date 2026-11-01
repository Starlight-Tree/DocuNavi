"""LLM に渡す検索ツール `search_documents`。検索の入口は `app/search/pipeline.py`（DB の `chunks` へのベクトル検索に、
ハイブリッド・リランカー・GraphRAG を環境変数で重ねる。3 つとも off なら `app/search/vector.py` のベクトル検索そのまま）。

検索はひとつの関数 `search_chunks_async` に閉じてある。テストはここを差し替えて（偽の検索）、DB も埋め込みの API も使わずに動かす。
"""

import logging
from collections.abc import Sequence
from typing import Any

from langchain_core.runnables import RunnableConfig
from langchain_core.tools import StructuredTool

from app.scope import ACCESS_SCOPE_KEY, Scope
from app.search import pipeline as search_pipeline
from app.search import vector as search_vector
from app.search.types import Hit

logger = logging.getLogger(__name__)

TOP_K = 2
# 画面の出典カードを絞る基準: 同じ検索の 1 位との類似度の差がこれより大きい 2 件目以降は「弱い」とし、カードに出さない。
# LLM に渡す件数・文面は変えない。値は、最終確認の 52 問（答えのある 38 問）の 1 位と 2 位の差から決めた。2 件目が答えに要った 4 問
# の差は 0〜0.04 で、0.05 はそれを隠さない最小の丸めた値。標本が小さい（資料 6 冊・80 チャンク）ので、資料が増えたら測り直す
WEAK_GAP = 0.05

# グラフの tools ノードが、会話の検索対象を `config["configurable"]` に入れて渡すキー（LLM が渡す引数ではない）
COLLECTION_KEY = "collection"

_NOT_FOUND_TEXT = "該当する設定資料は見つかりませんでした。別のキーワードで再検索してください。"
# 検索が使えなかったときに、呼び出し側（API）へ出す固定文。例外の文字列には接続先・URL・本文が入りうるので載せない
_SEARCH_FAILED_TEXT = "資料の検索に失敗しました。"
# LLM に見せるツールの説明。変えると、モデルが見るツールの定義が変わる（tests/test_search_vector.py が固定している）
_SEARCH_DESCRIPTION = (
    "登録された資料（設定資料・店舗案内など）を検索する。\n\n"
    "質問に答える前に必ず使う。query には質問の要点となるキーワードや短い文を渡す。"
)


async def search_chunks_async(
    query: str, top_k: int = TOP_K, collection: str | None = None, *, scope: Scope
) -> list[Hit]:
    """クエリに近い資料の箇所を、`Hit`（ファイル名・本文・ページ）で最大 top_k 件返す（DB のベクトル検索が土台。しきい値未満は返さない）。
    `SEARCH_GRAPH` が on のときは、参照先のチャンクが `origin="graph"` で最後に加わることがある（top_k の外）。

    `scope` は必須（検索できるグループの範囲。全グループは `Scope.ALL` を明示する）。`collection` を渡すと、その範囲の中の
    そのグループだけを探す（None は範囲の全資料）。

    失敗しても BM25 などには戻らない（どちらで答えたかが曖昧になるため）。`VectorSearchError` を投げ、API が 503 の固定文にする。
    """
    try:
        return await search_pipeline.search(query, top_k, collection, scope=scope)
    except search_vector.VectorSearchError:
        raise
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・URL・本文が入りうる。型の名前だけを残す
        logger.warning("ベクトル検索に失敗しました (%s)", type(exc).__name__)
        raise search_vector.VectorSearchError(_SEARCH_FAILED_TEXT) from None


def _format_hits(hits: Sequence[Hit]) -> str:
    """LLM に見せる文面。ページは載せない（載せると回答が変わりうる。ページは `_sources` で画面にだけ渡す）。"""
    if not hits:
        return _NOT_FOUND_TEXT
    return "\n\n".join(f"[{hit.source}]\n{hit.text}" for hit in hits)


def _sources(hits: Sequence[Hit]) -> list[dict[str, Any]]:
    """画面に渡す出典の一覧（`_format_hits` の見出しと同じ順）。ツールの artifact で、LLM には渡らない。

    `page` は 1 以上の整数だけ、`page_end` は `page` より大きいときだけ入れる（不正な値は None にして、画面が表示しない）。
    `collection` は資料のグループ（未設定なら None）。画面の出典に、どのグループの資料かを添えるためのもの。
    `weak` は、1 位との類似度の差が `WEAK_GAP` を超える 2 件目以降と、GraphRAG で加えた参照先（`origin="graph"`）。画面の出典カードに出さない。
    類似度が無ければ（キーワードだけで拾った箇所など）差では判断せず False。
    """
    sources: list[dict[str, Any]] = []
    top = hits[0].similarity if hits else None
    for index, hit in enumerate(hits):
        page = hit.page if isinstance(hit.page, int) and hit.page >= 1 else None
        page_end = hit.page_end if page is not None and isinstance(hit.page_end, int) and hit.page_end > page else None
        weak = hit.origin == "graph" or (
            index > 0 and top is not None and hit.similarity is not None and round(top - hit.similarity, 4) > WEAK_GAP
        )
        sources.append(
            {"name": hit.source, "page": page, "page_end": page_end, "weak": weak, "collection": hit.collection}
        )
    return sources


async def _asearch_documents(query: str, config: RunnableConfig) -> tuple[str, list[dict[str, Any]]]:
    # 検索対象は LLM の引数ではなく、グラフの状態（会話ごとに固定）から tools ノードが config で渡す。LLM が変えられるのは query だけ
    # （`config` は LangChain が注入する引数で、LLM に見せるツールの定義には載らない）
    configurable = config.get("configurable") or {}
    collection = configurable.get(COLLECTION_KEY)
    # 検索できる範囲も、LLM の引数ではなく config で渡る（API が利用者ごとに決める）。渡されていなければ何も見えない範囲にする
    scope = configurable.get(ACCESS_SCOPE_KEY)
    if not isinstance(scope, Scope):
        logger.warning("検索の範囲が渡されませんでした。何も検索しません")
        scope = Scope.NONE
    hits = await search_chunks_async(query, collection=collection, scope=scope)
    return _format_hits(hits), _sources(hits)


# 非同期だけ（グラフは .ainvoke で呼ぶ）。同期の .invoke は用意しない（DB とネットワークを同期で呼ばないため）
search_documents = StructuredTool.from_function(
    coroutine=_asearch_documents,
    name="search_documents",
    description=_SEARCH_DESCRIPTION,
    # 戻り値は (LLM に見せる文面, 画面にだけ渡す出典の一覧)。ToolMessage.content が前者、.artifact が後者になる
    response_format="content_and_artifact",
)

TOOLS = [search_documents]
