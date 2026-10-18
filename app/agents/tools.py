"""LLM に渡す検索ツール `search_documents`。検索の実体は DB の `chunks` へのベクトル検索（`app/search_vector.py`）だけ。

検索はひとつの関数 `search_chunks_async` に閉じてある。テストはここを差し替えて（偽の検索）、DB も埋め込みの API も使わずに動かす。
"""

import logging
from collections.abc import Sequence

from langchain_core.tools import StructuredTool

from app import search_vector

logger = logging.getLogger(__name__)

TOP_K = 2

_NOT_FOUND_TEXT = "該当する設定資料は見つかりませんでした。別のキーワードで再検索してください。"
# 検索が使えなかったときに、呼び出し側（API）へ出す固定文。例外の文字列には接続先・URL・本文が入りうるので載せない
_SEARCH_FAILED_TEXT = "資料の検索に失敗しました。"
# LLM に見せるツールの説明。変えると、モデルが見るツールの定義が変わる（tests/test_search_vector.py が固定している）
_SEARCH_DESCRIPTION = (
    "登録された資料（設定資料・店舗案内など）を検索する。\n\n"
    "質問に答える前に必ず使う。query には質問の要点となるキーワードや短い文を渡す。"
)


async def search_chunks_async(query: str, top_k: int = TOP_K) -> list[tuple[str, str]]:
    """クエリに近い資料の箇所を、(ファイル名, 本文) で最大 top_k 件返す（DB のベクトル検索。しきい値未満は返さない）。

    失敗しても BM25 などには戻らない（どちらで答えたかが曖昧になるため）。`VectorSearchError` を投げ、API が 503 の固定文にする。
    """
    try:
        return await search_vector.search(query, top_k)
    except search_vector.VectorSearchError:
        raise
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・URL・本文が入りうる。型の名前だけを残す
        logger.warning("ベクトル検索に失敗しました (%s)", type(exc).__name__)
        raise search_vector.VectorSearchError(_SEARCH_FAILED_TEXT) from None


def _format_hits(hits: Sequence[tuple[str, str]]) -> str:
    if not hits:
        return _NOT_FOUND_TEXT
    return "\n\n".join(f"[{name}]\n{text}" for name, text in hits)


async def _asearch_documents(query: str) -> str:
    return _format_hits(await search_chunks_async(query))


# 非同期だけ（グラフは .ainvoke で呼ぶ）。同期の .invoke は用意しない（DB とネットワークを同期で呼ばないため）
search_documents = StructuredTool.from_function(
    coroutine=_asearch_documents,
    name="search_documents",
    description=_SEARCH_DESCRIPTION,
)

TOOLS = [search_documents]
