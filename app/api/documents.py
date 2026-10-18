"""資料の一覧（読み取り専用）。DB（`documents` / `chunks`）に保存された内容を、そのまま見せる。

- BM25 検索は、この DB を読まない（起動時に `storage/pdf/` の PDF を読み直す）。ここに出る本文は「DB に保存された資料」で、
  BM25 が採点する本文と同じとは限らない。ベクトル検索（`SEARCH_BACKEND=vector`）だけがこの DB を読む
- `GET /documents/{id}/file` は取り込んだ元の PDF を返す（名前と内容のハッシュが DB と合うものだけ。`app/pdf_originals.py`）
- `AUTH_MODE=required` のときは、`/chat` と同じ検証（`current_user`）を通った人だけが読める。資料ごとの閲覧権限は
  まだ無く、ログインした人は全員が全文を読める
- 埋め込みのベクトルは返さない（あるかどうかと、モデル名だけ）。DB のエラーの文字列も返さない（固定メッセージ）
"""

import asyncio
import logging
from datetime import datetime
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel

from app.auth import current_user
from app.history import get_pool
from app.pdf_originals import find_original

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents")

# bigint の上限。これを超える id は、DB に渡す前に 422 にする（桁あふれの例外を 503 にしないため）
_MAX_ID = 9_223_372_036_854_775_807
# 暴走した件数を返さないための上限（いまの資料は 3 文書・68 チャンク）
_MAX_DOCUMENTS = 500
_MAX_CHUNKS = 2000

_DETAIL_NOT_CONFIGURED = "資料のデータベースが設定されていません。"
_DETAIL_UNAVAILABLE = "資料を取得できませんでした。しばらく待ってから再試行してください。"
_DETAIL_NOT_FOUND = "指定された資料が見つかりません。"
_DETAIL_NO_ORIGINAL = "元の PDF は利用できません。"
_DETAIL_ORIGINAL_MISMATCH = "元の PDF が、DB に保存された資料と一致しません。"

_LIST_SQL = (
    "SELECT d.id AS id, d.source AS source, d.title AS title, d.status AS status, "
    "d.failure_reason AS failure_reason, d.pages AS pages, d.updated_at AS updated_at, "
    "count(c.id) AS chunk_count, count(c.embedding) AS embedded_count "
    "FROM documents d LEFT JOIN chunks c ON c.document_id = d.id "
    "GROUP BY d.id ORDER BY d.source, d.id LIMIT %(limit)s"
)
_DOCUMENT_SQL = (
    "SELECT d.id AS id, d.source AS source, d.title AS title, d.status AS status, "
    "d.failure_reason AS failure_reason, d.pages AS pages, d.updated_at AS updated_at, "
    "d.content_hash AS content_hash "
    "FROM documents d WHERE d.id = %(id)s"
)
_CHUNKS_SQL = (
    "SELECT c.chunk_index AS chunk_index, c.page AS page, c.page_end AS page_end, c.kind AS kind, "
    "c.heading AS heading, c.content AS content, c.embedding IS NOT NULL AS embedded, "
    "c.embedding_model AS embedding_model "
    "FROM chunks c WHERE c.document_id = %(id)s ORDER BY c.chunk_index LIMIT %(limit)s"
)


class DocumentSummary(BaseModel):
    id: int
    source: str
    # PDF の題名（/Title）。無ければ null で、画面は source（ファイル名）で代える
    title: str | None = None
    status: str
    failure_reason: str | None = None
    pages: int | None = None
    updated_at: datetime | None = None
    chunk_count: int = 0
    embedded_count: int = 0


class DocumentList(BaseModel):
    # False = DATABASE_URL が空で、資料の DB を使っていない（エラーではない）。文書が 0 件のときの documents == [] とは別
    configured: bool
    documents: list[DocumentSummary]


class ChunkItem(BaseModel):
    chunk_index: int
    page: int | None = None
    page_end: int | None = None
    kind: str | None = None
    heading: str | None = None
    content: str
    embedded: bool
    embedding_model: str | None = None


class DocumentDetail(BaseModel):
    document: DocumentSummary
    chunks: list[ChunkItem]
    # 元の PDF を GET /documents/{id}/file で返せるか（名前と内容のハッシュが DB と合うファイルが storage/pdf/ にある）
    original_pdf: bool = False


def _no_store(response: Response) -> None:
    # 資料の本文を、ブラウザーや途中のキャッシュに残さない
    response.headers["Cache-Control"] = "no-store"


def _unavailable(exc: Exception) -> HTTPException:
    # 失敗の種類だけを残す（接続先・SQL・本文は出さない）
    logger.warning("資料を取得できませんでした (%s)", type(exc).__name__)
    return HTTPException(status_code=503, detail=_DETAIL_UNAVAILABLE)


def _title(value: object) -> str | None:
    """空白だけの題名は、題名なしと同じに扱う（画面側がファイル名に代える条件を、1 つにする）。"""
    return value.strip() or None if isinstance(value, str) else None


def _summary(row: dict[str, Any]) -> DocumentSummary:
    return DocumentSummary(
        id=row["id"],
        source=row["source"],
        title=_title(row.get("title")),
        status=row["status"],
        failure_reason=row.get("failure_reason"),
        pages=row.get("pages"),
        updated_at=row.get("updated_at"),
        chunk_count=int(row.get("chunk_count") or 0),
        embedded_count=int(row.get("embedded_count") or 0),
    )


@router.get("", response_model=DocumentList)
async def list_documents(
    response: Response,
    _user: Annotated[str | None, Depends(current_user)],
) -> DocumentList:
    _no_store(response)
    pool = get_pool()
    if pool is None:
        return DocumentList(configured=False, documents=[])
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_LIST_SQL, {"limit": _MAX_DOCUMENTS})
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
        raise _unavailable(exc) from None
    return DocumentList(configured=True, documents=[_summary(row) for row in rows])


@router.get("/{document_id}/chunks", response_model=DocumentDetail)
async def document_chunks(
    document_id: Annotated[int, Path(ge=1, le=_MAX_ID)],
    response: Response,
    _user: Annotated[str | None, Depends(current_user)],
) -> DocumentDetail:
    _no_store(response)
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail=_DETAIL_NOT_CONFIGURED)
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_DOCUMENT_SQL, {"id": document_id})
            documents = await cursor.fetchall()
            if not documents:
                chunks: list[dict[str, Any]] = []
            else:
                cursor = await conn.execute(_CHUNKS_SQL, {"id": document_id, "limit": _MAX_CHUNKS})
                chunks = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 上と同じ
        raise _unavailable(exc) from None
    if not documents:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    summary = _summary(documents[0])
    summary.chunk_count = len(chunks)
    summary.embedded_count = sum(1 for chunk in chunks if chunk["embedded"])
    original = await asyncio.to_thread(find_original, documents[0]["source"], documents[0].get("content_hash"))
    return DocumentDetail(
        original_pdf=original.ok,
        document=summary,
        chunks=[
            ChunkItem(
                chunk_index=chunk["chunk_index"],
                page=chunk.get("page"),
                page_end=chunk.get("page_end"),
                kind=chunk.get("kind"),
                heading=chunk.get("heading"),
                content=chunk["content"],
                embedded=bool(chunk["embedded"]),
                embedding_model=chunk.get("embedding_model"),
            )
            for chunk in chunks
        ],
    )


def _content_disposition(document_id: int, source: str) -> str:
    # filename は ASCII の代わりの名前、filename* が本来の名前（日本語のファイル名は RFC 5987 の形で渡す）
    return f"inline; filename=\"document-{document_id}.pdf\"; filename*=UTF-8''{quote(source, safe='')}"


@router.get("/{document_id}/file")
async def document_file(
    document_id: Annotated[int, Path(ge=1, le=_MAX_ID)],
    _user: Annotated[str | None, Depends(current_user)],
) -> Response:
    """取り込んだ元の PDF。名前とハッシュが DB と合うものだけを、確かめたバイト列のまま返す（app/pdf_originals.py）。"""
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail=_DETAIL_NOT_CONFIGURED)
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_DOCUMENT_SQL, {"id": document_id})
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 上と同じ
        raise _unavailable(exc) from None
    if not rows:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    source = rows[0]["source"]
    original = await asyncio.to_thread(find_original, source, rows[0].get("content_hash"))
    if not original.ok or original.data is None:
        if original.reason == "mismatch":
            raise HTTPException(status_code=409, detail=_DETAIL_ORIGINAL_MISMATCH)
        raise HTTPException(status_code=404, detail=_DETAIL_NO_ORIGINAL)
    headers["Content-Disposition"] = _content_disposition(document_id, source)
    return Response(content=original.data, media_type="application/pdf", headers=headers)
