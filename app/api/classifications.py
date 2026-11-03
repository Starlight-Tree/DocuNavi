"""資料の分類の一覧（読み取り専用）。画面が、対象（`/targets`）と資料種別（`/doc-types`）の表示名・説明・資料の数を出すために読む。

- 登録済みの語彙は `document_targets` / `document_types`（表示名・説明・並び順。supabase/migrations/20261103150000）。
  資料の分類は `documents.target` / `documents.doc_type`（外部キー。未分類は NULL）。
- **見える資料のある分類だけを返す**（資料が 1 件も見えない対象・種別は、存在しないのと同じに、一覧に出さない）。
  件数も、利用者が見える資料（`ACCESS_MODE=enforce` なら、見えるグループの資料）だけを数える。
  見えない資料の件数・対象の名前が、一覧から漏れないようにするため（閲覧権限は `collection` が決め、ここでは変えない）。
- `document_count` は見える資料の数、`ready_count` は検索の対象になる資料（`status = 'ready'`）の数。0 の対象は「準備中」と出せる。
- 表が無い（M1 の前）ときは 503 の固定メッセージ。`AUTH_MODE=required` のときは `/chat` と同じ検証を通った人だけが読める。
  DB のエラーの文字列は返さない。見えるグループが 0 件なら 403。
"""

import logging
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.access import Access, request_access
from app.history import get_pool
from app.scope import ALLOWED_FILTER_SQL

logger = logging.getLogger(__name__)

router = APIRouter()

# 暴走した件数を返さないための上限（いまは対象 4・種別 4）
_MAX_ITEMS = 200

_DETAIL_NOT_READY = "資料の分類の情報がまだ用意されていません。"
_DETAIL_UNAVAILABLE = "資料の分類を取得できませんでした。しばらく待ってから再試行してください。"
# HTTPException の応答には、引数の Response に付けたヘッダーが載らない。エラーにも付けるため、こちらで渡す
_NO_STORE = {"Cache-Control": "no-store"}


def _list_sql(vocabulary: str, column: str) -> str:
    # 内側結合: 見える資料が 1 件も無い分類は、行ごと出ない。件数は、見える資料（ALLOWED_FILTER_SQL）だけ
    return (
        "SELECT v.name AS name, v.label AS label, v.description AS description, v.sort_order AS sort_order, "
        "count(d.id) AS document_count, count(d.id) FILTER (WHERE d.status = 'ready') AS ready_count "
        f"FROM {vocabulary} v JOIN documents d ON d.{column} = v.name "
        f"WHERE {ALLOWED_FILTER_SQL} "
        "GROUP BY v.name, v.label, v.description, v.sort_order "
        "ORDER BY v.sort_order, v.name LIMIT %(limit)s"
    )


_TARGETS_SQL = _list_sql("document_targets", "target")
_TYPES_SQL = _list_sql("document_types", "doc_type")


class ClassificationSummary(BaseModel):
    # `POST /chat` の `target`・資料の `target` / `doc_type` と同じ識別名。変わらない
    name: str
    # 画面に出す名前
    label: str
    description: str = ""
    # 登録した並び順（小さいほど先）
    sort_order: int = 0
    # 見える資料の数
    document_count: int = 0
    # 検索の対象になる資料の数。0 は「準備中」（選べるが、見つかる資料はまだ無い）
    ready_count: int = 0


class TargetList(BaseModel):
    # False = DATABASE_URL が空で、資料の DB を使っていない（エラーではない）。対象が 0 件のときの targets == [] とは別
    configured: bool
    targets: list[ClassificationSummary]


class DocTypeList(BaseModel):
    configured: bool
    doc_types: list[ClassificationSummary]


def _summary(row: dict[str, Any]) -> ClassificationSummary:
    label = row.get("label")
    return ClassificationSummary(
        name=row["name"],
        label=label.strip() if isinstance(label, str) and label.strip() else row["name"],
        description=row.get("description") or "",
        sort_order=int(row.get("sort_order") or 0),
        document_count=int(row.get("document_count") or 0),
        ready_count=int(row.get("ready_count") or 0),
    )


async def _read(sql: str, access: Access) -> list[ClassificationSummary] | None:
    """見える資料のある分類。DATABASE_URL が空なら None。"""
    access.require_any()
    pool = get_pool()
    if pool is None:
        return None
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(sql, {"limit": _MAX_ITEMS, "allowed": access.scope.sql_names()})
            rows = await cursor.fetchall()
    except psycopg.errors.UndefinedTable:
        # M1 の前（表か列が無い）。ほかの失敗（接続など）とは、メッセージを分ける
        logger.warning("資料の分類の表がありません (UndefinedTable)")
        raise HTTPException(status_code=503, detail=_DETAIL_NOT_READY, headers=_NO_STORE) from None
    except psycopg.errors.UndefinedColumn:
        logger.warning("資料の分類の列がありません (UndefinedColumn)")
        raise HTTPException(status_code=503, detail=_DETAIL_NOT_READY, headers=_NO_STORE) from None
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
        logger.warning("資料の分類を取得できませんでした (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail=_DETAIL_UNAVAILABLE, headers=_NO_STORE) from None
    return [_summary(row) for row in rows]


@router.get("/targets", response_model=TargetList)
async def list_targets(response: Response, access: Annotated[Access, Depends(request_access)]) -> TargetList:
    response.headers["Cache-Control"] = "no-store"
    items = await _read(_TARGETS_SQL, access)
    return TargetList(configured=items is not None, targets=items or [])


@router.get("/doc-types", response_model=DocTypeList)
async def list_doc_types(response: Response, access: Annotated[Access, Depends(request_access)]) -> DocTypeList:
    response.headers["Cache-Control"] = "no-store"
    items = await _read(_TYPES_SQL, access)
    return DocTypeList(configured=items is not None, doc_types=items or [])
