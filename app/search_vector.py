"""PDF の資料のベクトル検索（Supabase / pgvector）。`SEARCH_BACKEND=vector` のときだけ、`search_documents` が使う。

検索の経路は、質問（LLM が渡した検索クエリ）の埋め込みを 1 回作り（`gemini-embedding-2`、768 次元）、`chunks.embedding` との
コサイン類似度が高い順に上位を取る。保存側の入力の形・モデル・次元は `app/ingestion/embedding.py` が唯一の定義で、ここはそれに揃える。
DB は既存の接続プール（`app/history.py` の `get_pool()`）を借りる。読むだけで、書かない。

- **しきい値（`MIN_SIMILARITY`）未満の結果は返さない。** 弱い一致の注記をつけて返す案は、入れていない。評価（evals/README.md の
  「ベクトル検索の比較」）では、このしきい値で答えのある質問が 22 問中 2 問（t005・t001）、答えなしになった。既知の制約。
- **失敗したときは、この関数が例外を投げる。** BM25 に戻すかどうかは呼び出し側（`app/agents/tools.py`）が決める。例外の文字列には
  接続先・URL・本文が入りうるので、ログには型の名前だけを残す。
- 埋め込みの呼び出しは 1 回だけ（SDK の再試行は切る）で、上限は `EMBED_TIMEOUT_SECONDS`。質問ごとに API を 1 回呼ぶ。
"""

import asyncio
import hashlib
import logging
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.history import get_pool
from app.ingestion import embedding

logger = logging.getLogger(__name__)

BACKEND_ENV = "SEARCH_BACKEND"
BACKENDS = ("bm25", "vector")
DEFAULT_BACKEND = "bm25"

# 1 位の類似度がこれ未満の結果は返さない。evals/README.md の「ベクトル検索の比較」で、dev の 19 問から決めた値
# （答えのある質問の 1 位は 0.68〜0.89、資料外は 0.57〜0.77 で重なる。小さい標本なので、動く値と見る）
MIN_SIMILARITY = 0.729
EMBED_TIMEOUT_SECONDS = 10.0

_PLACEHOLDER_KEYS = {"", "your_api_key_here"}

_SEARCH_SQL = (
    "SELECT d.source AS source, c.heading AS heading, c.content AS content, "
    "1 - (c.embedding OPERATOR(extensions.<=>) %(q)s::extensions.vector(768)) AS sim "
    "FROM chunks c JOIN documents d ON d.id = c.document_id "
    "WHERE d.source = ANY(%(sources)s) AND d.status = 'ready' AND c.embedding IS NOT NULL "
    "AND c.embedding_model = %(model)s AND c.embedding_dimensions = %(dims)s "
    "ORDER BY c.embedding OPERATOR(extensions.<=>) %(q)s::extensions.vector(768), d.source, c.chunk_index "
    "LIMIT %(k)s"
)

_VERIFY_SQL = (
    "SELECT d.source AS source, d.status AS status, d.content_hash AS document_hash, c.heading AS heading, "
    "c.content AS content, c.embedding IS NOT NULL AS has_vector, c.embedding_model AS model, "
    "c.embedding_dimensions AS dims, c.embedding_input_hash AS input_hash "
    "FROM documents d LEFT JOIN chunks c ON c.document_id = d.id WHERE d.source = ANY(%(sources)s)"
)


class VectorSearchError(RuntimeError):
    """ベクトル検索を使えない（設定・DB の状態）。起動の検査では、このメッセージ（固定文 + ファイル名）で起動を止める。"""


def search_backend() -> str:
    """環境変数 SEARCH_BACKEND（bm25 / vector。未設定・空は bm25）。知らない値は ValueError。"""
    value = os.environ.get(BACKEND_ENV, "").strip().lower() or DEFAULT_BACKEND
    if value not in BACKENDS:
        raise ValueError(f"{BACKEND_ENV} は {' / '.join(BACKENDS)} のどれかにしてください")
    return value


_client: Any | None = None


def _api_key() -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if key in _PLACEHOLDER_KEYS:
        raise VectorSearchError("質問の埋め込みには GEMINI_API_KEY が要ります")
    return key


def _get_client() -> Any:
    global _client
    if _client is None:
        from google import genai
        from google.genai import types

        _client = genai.Client(
            api_key=_api_key(),
            # 再試行しない（attempts=1）。timeout はミリ秒
            http_options=types.HttpOptions(
                timeout=int(EMBED_TIMEOUT_SECONDS * 1000), retry_options=types.HttpRetryOptions(attempts=1)
            ),
        )
    return _client


async def embed_query(query: str) -> list[float]:
    """検索クエリの埋め込み（768 次元）。API を 1 回呼ぶ。失敗は例外（文字列に URL などが入りうるので、呼び出し側は型だけをログに残す）。"""
    from google.genai import types

    client = _get_client()
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


def _chunk_text(heading: str | None, content: str) -> str:
    return f"{heading}\n{content}" if heading else content


async def search(query: str, top_k: int, sources: list[str]) -> list[tuple[str, str]]:
    """sources（documents.source = PDF のファイル名）の中から、類似度がしきい値以上のチャンクを上位 top_k 件、(ファイル名, 本文) で返す。"""
    if not sources or top_k <= 0 or not query.strip():
        return []
    pool = get_pool()
    if pool is None:
        raise VectorSearchError("DB の接続プールが開いていません")
    literal = embedding.vector_literal(await embed_query(query))
    params = {
        "q": literal,
        "sources": list(sources),
        "model": embedding.MODEL,
        "dims": embedding.DIMENSIONS,
        "k": top_k,
    }
    async with pool.connection() as conn:
        cursor = await conn.execute(_SEARCH_SQL, params)
        rows = await cursor.fetchall()
    return [
        (row["source"], _chunk_text(row["heading"], row["content"]))
        for row in rows
        if float(row["sim"]) >= MIN_SIMILARITY
    ]


async def verify(files: Mapping[str, Path], *, mock: bool, vertex: bool) -> None:
    """起動時の検査。ベクトル検索を使える状態でなければ VectorSearchError（起動を止める。BM25 には黙って切り替えない）。

    files: 検索する PDF（ファイル名 -> パス）。DB の文書が、いまのファイルを取り込んだもの（文書のハッシュが一致）で、
    全チャンクに現在のモデル・入力の埋め込みがあることを確かめる。
    """
    if mock:
        raise VectorSearchError("モックモードではベクトル検索を使えません（GEMINI_API_KEY を設定してください）")
    if vertex:
        raise VectorSearchError("Vertex AI モードでは、ベクトル検索の質問の埋め込みに対応していません（API キーで使います）")
    _api_key()
    pool = get_pool()
    if pool is None:
        raise VectorSearchError("ベクトル検索には DATABASE_URL が要ります（DB の接続プールが開いていません）")
    if not files:
        return
    file_hashes = {
        name: await asyncio.to_thread(lambda p=path: hashlib.sha256(p.read_bytes()).hexdigest())
        for name, path in files.items()
    }
    async with pool.connection() as conn:
        cursor = await conn.execute(_VERIFY_SQL, {"sources": list(files)})
        rows = await cursor.fetchall()
    by_source: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        by_source.setdefault(row["source"], []).append(row)
    problems: list[str] = []
    for name in sorted(files):
        doc_rows = by_source.get(name)
        if not doc_rows:
            problems.append(f"{name}: DB に取り込まれていません")
        elif doc_rows[0]["status"] != "ready":
            problems.append(f"{name}: 取り込みに失敗した状態です")
        elif doc_rows[0]["document_hash"] != file_hashes[name]:
            problems.append(f"{name}: 取り込み後にファイルが変わっています")
        elif any(r["content"] is None for r in doc_rows):
            problems.append(f"{name}: チャンクがありません")
        elif any(not _embedding_is_current(r) for r in doc_rows):
            problems.append(f"{name}: 埋め込みが無い、または古いチャンクがあります")
    if problems:
        raise VectorSearchError(
            "ベクトル検索の資料が DB と合っていません（python -m app.ingestion --apply と python -m app.ingestion.embed --apply で更新）: "
            + "; ".join(problems)
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
