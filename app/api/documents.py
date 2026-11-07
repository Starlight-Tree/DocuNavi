"""資料の一覧（読み取り専用）。DB（`documents` / `chunks`）に保存された内容を、そのまま見せる。

- チャットの検索（ベクトル検索）が読むのと同じ DB。ここに出る本文が、検索の対象になる本文そのもの（`status = 'ready'` の文書）
- `GET /documents/{id}/file` は取り込んだ元の PDF を返す（名前と内容のハッシュが DB と合うものだけ。`app/pdf_originals.py`）
- `AUTH_MODE=required` のときは、`/chat` と同じ検証（`current_user`）を通った人だけが読める
- `ACCESS_MODE=enforce` のときは、利用者が見えるグループ（`app/access.py`）の資料だけが一覧に出て、見えない資料の本文・PDF・関係図は
  存在しないのと同じ 404（`off` と `shadow` は、ログインした人が全グループの全文を読める）。見えるグループが 0 件なら 403
- 埋め込みのベクトルは返さない（あるかどうかと、モデル名だけ）。DB のエラーの文字列も返さない（固定メッセージ）
"""

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Annotated, Any
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from pydantic import BaseModel

from app.access import Access, request_access
from app.history import get_pool
from app.pdf_originals import find_original
from app.r2_store import default_store
from app.scope import ALLOWED_FILTER_SQL

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents")

# bigint の上限。これを超える id は、DB に渡す前に 422 にする（桁あふれの例外を 503 にしないため）
# 資料群の名前の形。取り込み（app/ingestion/service.py）と DB の制約（workers/api/migrations/0001_schema.sql）と同じ
_COLLECTION_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,40}")
# 対象・資料種別の名前の形。DB の制約（workers/api/migrations/0001_schema.sql）と同じ
_CLASSIFICATION_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
_MAX_ID = 9_223_372_036_854_775_807
# 暴走した件数を返さないための上限（いまの資料は 3 文書・68 チャンク）
_MAX_DOCUMENTS = 500
_MAX_CHUNKS = 2000

_DETAIL_NOT_CONFIGURED = "資料のデータベースが設定されていません。"
_DETAIL_UNAVAILABLE = "資料を取得できませんでした。しばらく待ってから再試行してください。"
_DETAIL_NOT_FOUND = "指定された資料が見つかりません。"
_DETAIL_NO_ORIGINAL = "元の PDF は利用できません。"
_DETAIL_ORIGINAL_MISMATCH = "元の PDF が、DB に保存された資料と一致しません。"
_DETAIL_ORIGINAL_UNAVAILABLE = "元の PDF を取得できませんでした。しばらく待ってから再試行してください。"

_LIST_SQL = (
    "SELECT d.id AS id, d.source AS source, d.title AS title, d.collection AS collection, "
    "d.target AS target, d.doc_type AS doc_type, d.status AS status, "
    "d.failure_reason AS failure_reason, d.pages AS pages, d.updated_at AS updated_at, "
    "count(c.id) AS chunk_count, count(c.embedding_model) AS embedded_count, "
    "EXISTS (SELECT 1 FROM document_images i WHERE i.document_id = d.id AND i.kind = 'hero' AND i.status = 'approved') "
    "AS hero_image "
    "FROM documents d LEFT JOIN chunks c ON c.document_id = d.id "
    "WHERE " + ALLOWED_FILTER_SQL + " "
    "GROUP BY d.id ORDER BY d.source, d.id LIMIT %(limit)s"
)
_DOCUMENT_SQL = (
    "SELECT d.id AS id, d.source AS source, d.title AS title, d.collection AS collection, "
    "d.target AS target, d.doc_type AS doc_type, d.status AS status, "
    "d.failure_reason AS failure_reason, d.pages AS pages, d.updated_at AS updated_at, "
    "d.content_hash AS content_hash "
    "FROM documents d WHERE d.id = %(id)s"
)
# 承認済みのヒーロー画像。資料と同じ行から引く（資料の可視性の判定を、PDF の配信と同じ行に対して行うため）
_HERO_SQL = (
    "SELECT d.collection AS collection, i.mime_type AS mime_type, i.r2_key AS r2_key "
    "FROM documents d JOIN document_images i ON i.document_id = d.id "
    "WHERE d.id = %(id)s AND i.kind = 'hero' AND i.status = 'approved'"
)
_CHUNKS_SQL = (
    "SELECT c.chunk_index AS chunk_index, c.page AS page, c.page_end AS page_end, c.kind AS kind, "
    "c.heading AS heading, c.content AS content, c.embedding_model IS NOT NULL AS embedded, "
    "c.embedding_model AS embedding_model "
    "FROM chunks c WHERE c.document_id = %(id)s ORDER BY c.chunk_index LIMIT %(limit)s"
)


class DocumentSummary(BaseModel):
    id: int
    source: str
    # PDF の題名（/Title）。無ければ null で、画面は source（ファイル名）で代える
    title: str | None = None
    # 資料群の名前（storage/pdf/<名前>/ のディレクトリ名。cafe / guides / zahard など）。未設定なら null。表示名は画面が決める
    collection: str | None = None
    # 対象（document_targets.name。例: zahard / minato-coffee）と資料種別（document_types.name。例: setting / manual）。未分類は null。
    # 表示名は GET /targets・GET /doc-types が持つ。閲覧権限は変えない（権限は collection だけが決める）
    target: str | None = None
    doc_type: str | None = None
    status: str
    failure_reason: str | None = None
    pages: int | None = None
    updated_at: datetime | None = None
    chunk_count: int = 0
    embedded_count: int = 0
    # 承認済みのヒーロー画像があるか（あれば GET /documents/{id}/hero で取れる）。画像そのものは一覧に入れない
    hero_image: bool = False


class DocumentList(BaseModel):
    # False = D1_API_URL が空で、資料の DB を使っていない（エラーではない）。文書が 0 件のときの documents == [] とは別
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


def _collection(value: object) -> str | None:
    """資料群の名前。文字列で、取り込み側と同じ形（英数字・_・-、40 文字まで）のときだけ返す。それ以外は未設定と同じ。"""
    return value if isinstance(value, str) and _COLLECTION_PATTERN.fullmatch(value) else None


def _classification(value: object) -> str | None:
    """対象・資料種別の名前。文字列で、DB の制約と同じ形のときだけ返す。それ以外は未分類と同じ。"""
    return value if isinstance(value, str) and _CLASSIFICATION_PATTERN.fullmatch(value) else None


def _timestamp(value: object) -> datetime | None:
    """D1 の時刻（UNIX ミリ秒の整数）を datetime（UTC）にする。"""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc)
    return None


def _summary(row: dict[str, Any]) -> DocumentSummary:
    return DocumentSummary(
        id=row["id"],
        source=row["source"],
        title=_title(row.get("title")),
        collection=_collection(row.get("collection")),
        target=_classification(row.get("target")),
        doc_type=_classification(row.get("doc_type")),
        status=row["status"],
        failure_reason=row.get("failure_reason"),
        pages=row.get("pages"),
        updated_at=_timestamp(row.get("updated_at")),
        chunk_count=int(row.get("chunk_count") or 0),
        embedded_count=int(row.get("embedded_count") or 0),
        hero_image=bool(row.get("hero_image")),
    )


@router.get("", response_model=DocumentList)
async def list_documents(
    response: Response,
    access: Annotated[Access, Depends(request_access)],
) -> DocumentList:
    _no_store(response)
    access.require_any()
    pool = get_pool()
    if pool is None:
        return DocumentList(configured=False, documents=[])
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_LIST_SQL, {"limit": _MAX_DOCUMENTS, "allowed": access.scope.sql_names()})
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
        raise _unavailable(exc) from None
    access.scope.log_shadow("資料の一覧", (row.get("collection") for row in rows))
    # SQL で絞っているが、返す前にもう一度確かめる（範囲の外のグループの資料は返さない）
    return DocumentList(
        configured=True, documents=[_summary(row) for row in rows if access.allows(row.get("collection"))]
    )


@router.get("/{document_id}/chunks", response_model=DocumentDetail)
async def document_chunks(
    document_id: Annotated[int, Path(ge=1, le=_MAX_ID)],
    response: Response,
    access: Annotated[Access, Depends(request_access)],
) -> DocumentDetail:
    _no_store(response)
    access.require_any()
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
    # 見えないグループの資料は、存在しないのと同じ 404（本文は返さない）
    access.require_visible(documents[0].get("collection"), "資料の本文", _DETAIL_NOT_FOUND)
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
    access: Annotated[Access, Depends(request_access)],
) -> Response:
    """取り込んだ元の PDF。名前とハッシュが DB と合うものだけを、確かめたバイト列のまま返す（app/pdf_originals.py）。"""
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    access.require_any()
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
    access.require_visible(rows[0].get("collection"), "元の PDF", _DETAIL_NOT_FOUND)
    source = rows[0]["source"]
    original = await asyncio.to_thread(find_original, source, rows[0].get("content_hash"))
    if not original.ok or original.data is None:
        if original.reason == "mismatch":
            raise HTTPException(status_code=409, detail=_DETAIL_ORIGINAL_MISMATCH)
        if original.reason == "unavailable":
            raise HTTPException(status_code=503, detail=_DETAIL_ORIGINAL_UNAVAILABLE)
        raise HTTPException(status_code=404, detail=_DETAIL_NO_ORIGINAL)
    headers["Content-Disposition"] = _content_disposition(document_id, source)
    return Response(content=original.data, media_type="application/pdf", headers=headers)


_DETAIL_NO_HERO = "この資料の画像はありません。"
MAX_HERO_BYTES = 5 * 1024 * 1024
_HERO_MIME_TYPES = {"image/png", "image/jpeg", "image/webp"}


@router.get("/{document_id}/hero")
async def document_hero(
    document_id: Annotated[int, Path(ge=1, le=_MAX_ID)],
    access: Annotated[Access, Depends(request_access)],
) -> Response:
    """承認済みのヒーロー画像（資料ごとに 1 枚）。承認済みが無い・見えないグループの資料は、同じ 404（画面は灰色の代替表示のまま）。"""
    headers = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}
    access.require_any()
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail=_DETAIL_NOT_CONFIGURED)
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_HERO_SQL, {"id": document_id})
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 上と同じ
        raise _unavailable(exc) from None
    if not rows:
        raise HTTPException(status_code=404, detail=_DETAIL_NO_HERO)
    access.require_visible(rows[0].get("collection"), "ヒーロー画像", _DETAIL_NO_HERO)
    mime_type, r2_key = rows[0].get("mime_type"), rows[0].get("r2_key")
    if mime_type not in _HERO_MIME_TYPES or not isinstance(r2_key, str):
        raise HTTPException(status_code=404, detail=_DETAIL_NO_HERO)
    try:
        data = await asyncio.to_thread(lambda: default_store().get(r2_key, MAX_HERO_BYTES))
    except Exception as exc:  # noqa: BLE001 - 上と同じ
        raise _unavailable(exc) from None
    if data is None:
        raise HTTPException(status_code=404, detail=_DETAIL_NO_HERO)
    return Response(content=data, media_type=mime_type, headers=headers)
