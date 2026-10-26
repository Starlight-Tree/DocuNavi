"""資料のベクトル検索（Supabase / pgvector）。`search_documents` ツールの検索の実体で、アプリの検索はこれだけ。

検索の経路は、質問（LLM が渡した検索クエリ）の埋め込みを 1 回作り（`gemini-embedding-2`、768 次元）、`chunks.embedding` との
コサイン類似度が高い順に上位を取る。DB の `documents`（`status = 'ready'`）の全チャンクが対象で、ローカルの PDF は読まない。
保存側の入力の形・モデル・次元は `app/ingestion/embedding.py` が唯一の定義で、ここはそれに揃える。
DB は既存の接続プール（`app/history.py` の `get_pool()`）を借りる。読むだけで、書かない。

- **しきい値（`MIN_SIMILARITY`）は粗い下限で、「資料にない」の判断は LLM に任せる。** 類似度では、資料にある話題の質問と
  資料にない質問を分けられない（答えのある質問の 1 位は 0.62〜0.89、資料にない話題だが関連する質問は 0.67〜0.78、無関係な質問は 0.66 以下）。
  そこで下限は低めにして、明らかな無関係だけを切り、残りは LLM がプロンプトの指示で断る
  （評価: evals/README.md の「ベクトル検索の比較」「回答の評価」）。
- **失敗したときは、この関数が例外（`VectorSearchError`）を投げる。** 別の経路に戻らない（どちらで答えたかが曖昧になるため）。
  例外の文字列には接続先・URL・本文が入りうるので、ログには型の名前だけを残す。
- 埋め込みの呼び出しは 1 回だけ（SDK の再試行は切る）で、上限は `EMBED_TIMEOUT_SECONDS`。質問ごとに API を 1 回呼ぶ。
  `GEMINI_API_KEY_PAID` があるときだけ、無料キーが 429 だったときに有料キーで 1 回やり直す。
"""

import asyncio
import logging
import os
from collections.abc import Mapping
from typing import Any, NamedTuple

from app.agents.resilience import KeySwitch, call_with_key_switch, key_switch_hold_seconds, paid_api_key
from app.history import get_pool
from app.ingestion import embedding
from app.scope import ALLOWED_FILTER_SQL, Scope, require_scope

logger = logging.getLogger(__name__)

# この値未満のチャンクは返さない。evals/README.md の「回答の評価（ベクトル検索）」で、14 問を LLM まで通して確かめた値
# （資料にない話題の質問 11 問を全て断り、答えのある 3 問は断らなかった。小さい標本なので、動く値と見る）
MIN_SIMILARITY = 0.65
EMBED_TIMEOUT_SECONDS = 10.0

_PLACEHOLDER_KEYS = {"", "your_api_key_here"}

_SEARCH_SQL = (
    "SELECT d.source AS source, d.collection AS collection, c.heading AS heading, c.content AS content, "
    "c.page AS page, c.page_end AS page_end, "
    "1 - (c.embedding OPERATOR(extensions.<=>) %(q)s::extensions.vector(768)) AS sim "
    "FROM chunks c JOIN documents d ON d.id = c.document_id "
    "WHERE d.status = 'ready' AND c.embedding IS NOT NULL "
    "AND (%(collection)s::text IS NULL OR d.collection = %(collection)s::text) "
    f"AND {ALLOWED_FILTER_SQL} "
    "AND c.embedding_model = %(model)s AND c.embedding_dimensions = %(dims)s "
    "ORDER BY c.embedding OPERATOR(extensions.<=>) %(q)s::extensions.vector(768), d.source, c.chunk_index "
    "LIMIT %(k)s"
)

_VERIFY_SQL = (
    "SELECT d.id AS id, d.source AS source, d.status AS status, c.heading AS heading, "
    "c.content AS content, c.embedding IS NOT NULL AS has_vector, c.embedding_model AS model, "
    "c.embedding_dimensions AS dims, c.embedding_input_hash AS input_hash "
    "FROM documents d LEFT JOIN chunks c ON c.document_id = d.id"
)


class Hit(NamedTuple):
    """検索で拾った 1 チャンク。`page` は PDF の 1 始まりのページ、`page_end` はページをまたぐ表の最終ページ（またがなければ None）。

    `similarity` はコサイン類似度。画面の出典カードを絞る判断（`app/agents/tools.py` の `WEAK_GAP`）だけに使い、LLM には渡さない。
    `collection` は資料のグループ（`documents.collection`。未設定なら None）。画面の出典に添えるだけで、LLM には渡さない。
    """

    source: str
    text: str
    page: int | None = None
    page_end: int | None = None
    similarity: float | None = None
    collection: str | None = None


class VectorSearchError(RuntimeError):
    """資料の検索を使えない（設定・DB の状態・実行時の失敗）。メッセージは固定文（ファイル名まで）で、API はこの型を 503 の固定文にする。"""


_client: Any | None = None
_paid_client: Any | None = None
_key_switch: KeySwitch | None = None


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key in _PLACEHOLDER_KEYS:
        raise VectorSearchError(
            "質問の埋め込みには GEMINI_API_KEY が要ります（LLM を Vertex AI で使う場合も、埋め込みは API キーで呼びます）"
        )
    return key


def _new_client(api_key: str) -> Any:
    from google import genai
    from google.genai import types

    return genai.Client(
        # Vertex AI の環境変数（GOOGLE_GENAI_USE_VERTEXAI）が真でも、埋め込みは API キーで呼ぶ（キーを Vertex に送らない）
        vertexai=False,
        api_key=api_key,
        # 再試行しない（attempts=1）。timeout はミリ秒
        http_options=types.HttpOptions(
            timeout=int(EMBED_TIMEOUT_SECONDS * 1000), retry_options=types.HttpRetryOptions(attempts=1)
        ),
    )


def _get_client() -> Any:
    global _client
    if _client is None:
        _client = _new_client(_api_key())
    return _client


def _get_paid_client() -> Any:
    """GEMINI_API_KEY_PAID のクライアント（無料キーが 429 のときだけ使う）。"""
    global _paid_client
    if _paid_client is None:
        _paid_client = _new_client(paid_api_key())
    return _paid_client


def _get_key_switch() -> KeySwitch:
    global _key_switch
    if _key_switch is None:
        _key_switch = KeySwitch(key_switch_hold_seconds(), name="埋め込み")
    return _key_switch


async def _embed(client: Any, query: str) -> list[float]:
    from google.genai import types

    contents = [types.Content(parts=[types.Part(text=embedding.query_text(query))])]
    result = await asyncio.wait_for(
        client.aio.models.embed_content(
            model=embedding.MODEL,
            contents=contents,
            config=types.EmbedContentConfig(output_dimensionality=embedding.DIMENSIONS),
        ),
        timeout=EMBED_TIMEOUT_SECONDS + 1.0,
    )
    return embedding.check_vectors([e.values or [] for e in (result.embeddings or [])], 1)[0]


async def embed_query(query: str) -> list[float]:
    """検索クエリの埋め込み（768 次元）。API を 1 回呼ぶ。失敗は例外（文字列に URL などが入りうるので、呼び出し側は型だけをログに残す）。

    GEMINI_API_KEY_PAID があるときだけ、無料キーが 429（レート制限）だったときに有料キーで 1 回やり直す
    （LLM と同じ方針。ここは再試行しないので、429 はすぐ切り替わる）。400/401/403 や一時的な失敗では切り替えない。
    """
    if not paid_api_key():
        return await _embed(_get_client(), query)
    return await call_with_key_switch(
        lambda: _embed(_get_client(), query),
        lambda: _embed(_get_paid_client(), query),
        switch=_get_key_switch(),
        name="埋め込み",
    )


def _chunk_text(heading: str | None, content: str) -> str:
    return f"{heading}\n{content}" if heading else content


async def search(query: str, top_k: int, collection: str | None = None, *, scope: Scope) -> list[Hit]:
    """類似度がしきい値以上のチャンクを、近い順に上位 top_k 件、`Hit`（ファイル名・本文・ページ）で返す。

    `scope` は必須（キーワード専用）。検索できるグループの範囲で、`Scope.ALL` を明示したときだけ全資料を探す。渡し忘れ・`None`・
    `Scope` 以外は TypeError、空の範囲は DB を呼ばずに空の結果（漏れる方向には倒れない）。範囲は SQL で絞り、返ってきた行も
    もう一度確かめる（SQL の条件を誤っても、範囲外のグループの本文は返さない）。
    `collection` を渡すと、さらにそのグループ（`documents.collection`）の資料だけを探す（範囲の外のグループなら空）。None は範囲の全資料。
    存在しないグループは、エラーではなく空の結果になる。
    """
    scope = require_scope(scope)
    if top_k <= 0 or not query.strip():
        return []
    if scope.is_empty or (collection is not None and not scope.allows(collection)):
        return []
    pool = get_pool()
    if pool is None:
        raise VectorSearchError("DB の接続プールが開いていません")
    literal = embedding.vector_literal(await embed_query(query))
    params = {
        "q": literal,
        "model": embedding.MODEL,
        "dims": embedding.DIMENSIONS,
        "k": top_k,
        "collection": collection,
        "allowed": scope.sql_names(),
    }
    async with pool.connection() as conn:
        cursor = await conn.execute(_SEARCH_SQL, params)
        rows = await cursor.fetchall()
    hits = [
        Hit(
            row["source"],
            _chunk_text(row["heading"], row["content"]),
            row.get("page"),
            row.get("page_end"),
            float(row["sim"]),
            row.get("collection"),
        )
        for row in rows
        if float(row["sim"]) >= MIN_SIMILARITY
    ]
    scope.log_shadow("検索", (hit.collection for hit in hits))
    return [hit for hit in hits if scope.allows(hit.collection)]


async def verify() -> None:
    """起動時の検査（本物の LLM のときだけ）。検索を使える状態でなければ VectorSearchError（起動を止める）。

    API キー（質問の埋め込み）と DB の接続プールがあり、DB の検索できる資料（status = 'ready'）の全チャンクに、
    現在のモデル・入力の埋め込みがあることを確かめる。ローカルの PDF とは突き合わせない（検索は DB だけを読む）。
    """
    _api_key()
    pool = get_pool()
    if pool is None:
        raise VectorSearchError("資料の検索には DATABASE_URL が要ります（DB の接続プールが開いていません）")
    async with pool.connection() as conn:
        cursor = await conn.execute(_VERIFY_SQL)
        rows = await cursor.fetchall()
    by_document: dict[Any, list[dict[str, Any]]] = {}
    for row in rows:
        if row["status"] == "ready":
            by_document.setdefault(row["id"], []).append(row)
    if not by_document:
        raise VectorSearchError(
            "検索できる資料が DB にありません（python -m app.ingestion --apply と python -m app.ingestion.embed --apply で取り込む）"
        )
    problems: list[str] = []
    for doc_rows in by_document.values():
        name = doc_rows[0]["source"]
        if any(r["content"] is None for r in doc_rows):
            problems.append(f"{name}: チャンクがありません")
        elif any(not _embedding_is_current(r) for r in doc_rows):
            problems.append(f"{name}: 埋め込みが無い、または古いチャンクがあります")
    if problems:
        raise VectorSearchError(
            "資料の埋め込みが DB と合っていません（python -m app.ingestion.embed --apply で更新）: "
            + "; ".join(sorted(problems))
        )


def _embedding_is_current(row: Mapping[str, Any]) -> bool:
    return bool(
        row["has_vector"]
        and row["model"] == embedding.MODEL
        and row["dims"] == embedding.DIMENSIONS
        and row["input_hash"] == embedding.input_hash(row["heading"], row["content"])
    )


async def close() -> None:
    """埋め込みのクライアントを閉じる（lifespan の終了時）。"""
    global _client
    client, _client = _client, None
    if client is not None:
        await client.aio.aclose()
