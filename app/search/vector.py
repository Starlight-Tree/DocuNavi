"""資料のベクトル検索（Cloudflare Vectorize + D1）。`search_documents` ツールの検索の実体で、アプリの検索はこれだけ。

検索の経路は、質問（LLM が渡した検索クエリ）の埋め込みを 1 回作り（`gemini-embedding-2`、768 次元）、Vectorize のベクトルとの
コサイン類似度が高い順に上位を取る（ベクトルの検索は Vectorize、本文とメタデータは D1。どちらもデータ用 Worker 経由）。DB の `documents`（`status = 'ready'`）の全チャンクが対象で、ローカルの PDF は読まない。
保存側の入力の形・モデル・次元は `app/ingestion/embedding.py` が唯一の定義で、ここはそれに揃える。
DB は既存の接続（`app/history.py` の `get_pool()`、`app/d1.py` の `D1Pool`）を借りる。読むだけで、書かない。

- **しきい値（`MIN_SIMILARITY`）は粗い下限で、「資料にない」の判断は LLM に任せる。** 類似度では、資料にある話題の質問と
  資料にない質問を分けられない（答えのある質問の 1 位は 0.62〜0.89、資料にない話題だが関連する質問は 0.67〜0.78、無関係な質問は 0.66 以下）。
  そこで下限は低めにして、明らかな無関係だけを切り、残りは LLM がプロンプトの指示で断る
  （評価: evals/README.md の「ベクトル検索の比較」「回答の評価」）。
- **失敗したときは、この関数が例外（`VectorSearchError`）を投げる。** 別の経路に戻らない（どちらで答えたかが曖昧になるため）。
  例外の文字列には接続先・URL・本文が入りうるので、ログには型の名前だけを残す。
- 埋め込みの呼び出しは 1 回だけ（SDK の再試行は切る）で、上限は `EMBED_TIMEOUT_SECONDS`。質問ごとに API を 1 回呼ぶ。
  `GEMINI_API_KEY_PAID` があるときだけ、無料キーが 429 だったときに有料キーで 1 回やり直す。
- `EMBEDDING_USE_VERTEXAI=true` なら、埋め込みを Vertex AI（ADC 認証・同じモデルと 768 次元）で呼ぶ。API キーも有料キーへの切り替えも使わない。
"""

import asyncio
import logging
import os
from collections.abc import Mapping, Sequence
from typing import Any

from app.agents.resilience import KeySwitch, call_with_key_switch, key_switch_hold_seconds, paid_api_key
from app.history import get_pool
from app.ingestion import embedding
from app.scope import ALLOWED_FILTER_SQL, DOCUMENTS_FILTER_SQL, TARGET_FILTER_SQL, Scope, require_scope
from app.search.types import Hit, VectorSearchError

# ログの絞り込み（ロガー名）を、移動前の app.search_vector のまま保つ
logger = logging.getLogger("app.search_vector")

# この値未満のチャンクは返さない。evals/README.md の「回答の評価（ベクトル検索）」で、14 問を LLM まで通して確かめた値
# （資料にない話題の質問 11 問を全て断り、答えのある 3 問は断らなかった。小さい標本なので、動く値と見る）
MIN_SIMILARITY = 0.65
EMBED_TIMEOUT_SECONDS = 10.0

_PLACEHOLDER_KEYS = {"", "your_api_key_here"}

# Vectorize が返したチャンク ID を、本文・メタデータに引き直す（範囲・対象・資料の条件は、Vectorize のフィルターに加えてここでも確かめる）
_CHUNKS_SQL = (
    "SELECT c.id AS chunk_id, d.id AS document_id, d.source AS source, d.collection AS collection, d.target AS target, "
    "c.heading AS heading, c.content AS content, c.page AS page, c.page_end AS page_end, c.chunk_index AS chunk_index "
    "FROM chunks c JOIN documents d ON d.id = c.document_id "
    "WHERE c.id IN (SELECT value FROM json_each(%(ids)s)) AND d.status = 'ready' "
    "AND (%(collection)s IS NULL OR d.collection = %(collection)s) "
    f"AND {ALLOWED_FILTER_SQL} "
    f"AND {TARGET_FILTER_SQL} "
    f"AND {DOCUMENTS_FILTER_SQL} "
    "AND c.embedding_model = %(model)s AND c.embedding_dimensions = %(dims)s"
)

_VERIFY_SQL = (
    "SELECT d.id AS id, d.source AS source, d.status AS status, c.heading AS heading, "
    "c.content AS content, c.embedding_model IS NOT NULL AS has_vector, c.embedding_model AS model, "
    "c.embedding_dimensions AS dims, c.embedding_input_hash AS input_hash "
    "FROM documents d LEFT JOIN chunks c ON c.document_id = d.id"
)

# Vectorize の $in は 100 件まで。それを超える絞り込みはフィルターに載せず、D1 側の条件だけで絞る
_VECTORIZE_IN_LIMIT = 100
# 絞り込みで落ちる分を見込んで、top_k より多く取る（Vectorize の上限は 100）
_OVERFETCH = 3
_VECTORIZE_MAX_TOP_K = 100


def _vector_filter(
    collection: str | None, allowed: list[str] | None, target: str | None, doc_ids: list[int] | None
) -> dict[str, Any]:
    """Vectorize のメタデータフィルター（collection / target / document_id）。載せられない条件は D1 側で絞る。"""
    flt: dict[str, Any] = {}
    if collection is not None:
        flt["collection"] = collection
    elif allowed is not None and len(allowed) <= _VECTORIZE_IN_LIMIT:
        flt["collection"] = {"$in": allowed}
    if target is not None:
        flt["target"] = target
    if doc_ids is not None and len(doc_ids) <= _VECTORIZE_IN_LIMIT:
        flt["document_id"] = {"$in": doc_ids}
    return flt


_client: Any | None = None
_paid_client: Any | None = None
_key_switch: KeySwitch | None = None


def _api_key() -> str:
    """質問の埋め込みに使う API キー。Vertex AI（`EMBEDDING_USE_VERTEXAI`）のときは要らず、空文字を返す。"""
    if embedding.use_vertex():
        return ""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key in _PLACEHOLDER_KEYS:
        raise VectorSearchError(
            "質問の埋め込みには GEMINI_API_KEY が要ります（LLM を Vertex AI で使う場合も、埋め込みは API キーで呼びます。"
            f"埋め込みを Vertex AI で呼ぶなら {embedding.VERTEX_ENV}=true と ADC が要ります）"
        )
    return key


def _new_client(api_key: str) -> Any:
    # 経路（API キーか Vertex AI か）は embedding.make_client が決める。GOOGLE_GENAI_USE_VERTEXAI（LLM 用）では変わらない
    return embedding.make_client(api_key or None, int(EMBED_TIMEOUT_SECONDS * 1000))


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
    if embedding.use_vertex() or not paid_api_key():  # Vertex AI は ADC 認証で、有料キーへの切り替えは無い
        return await _embed(_get_client(), query)
    return await call_with_key_switch(
        lambda: _embed(_get_client(), query),
        lambda: _embed(_get_paid_client(), query),
        switch=_get_key_switch(),
        name="埋め込み",
    )


def _chunk_text(heading: str | None, content: str) -> str:
    return f"{heading}\n{content}" if heading else content


async def search(
    query: str,
    top_k: int,
    collection: str | None = None,
    *,
    scope: Scope,
    target: str | None = None,
    document_ids: Sequence[int] | None = None,
) -> list[Hit]:
    """類似度がしきい値以上のチャンクを、近い順に上位 top_k 件、`Hit`（ファイル名・本文・ページ）で返す。

    `scope` は必須（キーワード専用）。検索できるグループの範囲で、`Scope.ALL` を明示したときだけ全資料を探す。渡し忘れ・`None`・
    `Scope` 以外は TypeError、空の範囲は DB を呼ばずに空の結果（漏れる方向には倒れない）。範囲は SQL で絞り、返ってきた行も
    もう一度確かめる（SQL の条件を誤っても、範囲外のグループの本文は返さない）。
    `collection` を渡すと、さらにそのグループ（`documents.collection`）の資料だけを探す（範囲の外のグループなら空）。None は範囲の全資料。
    存在しないグループは、エラーではなく空の結果になる。
    `target` を渡すと、さらにその対象（`documents.target`）の資料だけを探す（権限の範囲 ∩ 対象。範囲の外の資料は、対象に入っていても出ない）。
    None は対象で絞らない（従来どおり）。存在しない対象は空の結果。返ってきた行の対象も確かめる。
    `document_ids` を渡すと、さらにその資料（`documents.id`）だけを探す（権限の範囲 ∩ 資料。範囲の外の資料は、選ばれていても出ない）。
    None は資料で絞らない（従来どおり）。空の並びは何も探さない（「選ばない」を「全部」と読み替えない）。
    """
    scope = require_scope(scope)
    if top_k <= 0 or not query.strip():
        return []
    if document_ids is not None and not document_ids:
        return []
    if scope.is_empty or (collection is not None and not scope.allows(collection)):
        return []
    pool = get_pool()
    if pool is None:
        raise VectorSearchError("DB の接続が開いていません")
    vector = await embed_query(query)
    doc_ids = None if document_ids is None else sorted(set(document_ids))
    allowed = scope.sql_names()
    top = min(max(top_k * _OVERFETCH, top_k), _VECTORIZE_MAX_TOP_K)
    matches = await pool.vectors_query(vector, top, _vector_filter(collection, allowed, target, doc_ids))
    if not matches:
        return []
    scores = {int(chunk_id): score for chunk_id, score in matches}
    params = {
        "ids": sorted(scores),
        "model": embedding.MODEL,
        "dims": embedding.DIMENSIONS,
        "collection": collection,
        "target": target,
        "doc_ids": doc_ids,
        "allowed": allowed,
    }
    async with pool.connection() as conn:
        cursor = await conn.execute(_CHUNKS_SQL, params)
        found = await cursor.fetchall()
    for row in found:
        row["sim"] = scores[int(row["chunk_id"])]
    rows = sorted(found, key=lambda r: (-r["sim"], r["source"], r["chunk_index"]))[:top_k]
    wanted = None if document_ids is None else set(document_ids)
    hits = [
        Hit(
            row["source"],
            _chunk_text(row["heading"], row["content"]),
            row.get("page"),
            row.get("page_end"),
            float(row["sim"]),
            row.get("collection"),
            None,
            row.get("target"),
            row.get("document_id"),
        )
        for row in rows
        if float(row["sim"]) >= MIN_SIMILARITY
        # SQL の条件を誤っても、選ばれていない資料は返さない
        and (wanted is None or row.get("document_id") in wanted)
    ]
    scope.log_shadow("検索", (hit.collection for hit in hits))
    # SQL の条件を誤っても、範囲外のグループと、指定した対象以外の資料は返さない
    return [hit for hit in hits if scope.allows(hit.collection) and (target is None or hit.target == target)]


async def verify() -> None:
    """起動時の検査（本物の LLM のときだけ）。検索を使える状態でなければ VectorSearchError（起動を止める）。

    API キー（質問の埋め込み）と DB の接続プールがあり、DB の検索できる資料（status = 'ready'）の全チャンクに、
    現在のモデル・入力の埋め込みがあることを確かめる。ローカルの PDF とは突き合わせない（検索は DB だけを読む）。
    """
    _api_key()
    pool = get_pool()
    if pool is None:
        raise VectorSearchError("資料の検索には D1_API_URL が要ります（DB の接続が開いていません）")
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
