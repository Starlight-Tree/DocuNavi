"""資料グループの一覧（読み取り専用）。画面が、グループの表示名・説明・資料の数を出すために読む。

- 登録済みの情報は `document_collections`（表示名・説明・並び順。supabase/migrations/20261025205258）にある。検索の絞り込みに使う値は
  `documents.collection` の名前で、この表は検索に関係しない
- 表に無い名前が `documents.collection` にあれば、未登録のグループとして返す（`registered: false`、表示名は名前そのもの、説明は空）。
  フォルダを置いて取り込むだけで、画面から選べるようにするため
- 表が無い（マイグレーション 0006 の前）ときは 503 の固定メッセージ。表が空なのは異常ではなく、`documents` にある名前だけを返す
- `AUTH_MODE=required` のときは `/chat` と同じ検証を通った人だけが読める。DB のエラーの文字列は返さない
- `ACCESS_MODE=enforce` のときは、利用者が見えるグループ（`app/access.py`）だけを返す。見えるグループが 0 件なら 403
"""

import logging
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Response
from pydantic import BaseModel

from app.access import Access, request_access
from app.history import get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/collections")

# 暴走した件数を返さないための上限（いまのグループは 3 つ）
_MAX_COLLECTIONS = 200

_DETAIL_NOT_READY = "資料グループの情報がまだ用意されていません。"
_DETAIL_UNAVAILABLE = "資料グループを取得できませんでした。しばらく待ってから再試行してください。"
# HTTPException の応答には、引数の Response に付けたヘッダーが載らない。エラーにも付けるため、こちらで渡す
_NO_STORE = {"Cache-Control": "no-store"}

# 登録済み（document_collections）と、資料にだけある名前（documents.collection）を、名前でつないで 1 行ずつにする。
# document_count は全ての文書、ready_count は検索の対象になる文書（status = 'ready'）の数
_LIST_SQL = (
    "SELECT coalesce(c.name, d.collection) AS name, c.label AS label, c.description AS description, "
    "c.sort_order AS sort_order, (c.name IS NOT NULL) AS registered, "
    "count(d.id) AS document_count, count(d.id) FILTER (WHERE d.status = 'ready') AS ready_count "
    "FROM document_collections c FULL OUTER JOIN documents d ON d.collection = c.name "
    "WHERE coalesce(c.name, d.collection) IS NOT NULL "
    "AND (%(allowed)s::text[] IS NULL OR coalesce(c.name, d.collection) = ANY(%(allowed)s::text[])) "
    "GROUP BY c.name, c.label, c.description, c.sort_order, d.collection "
    "ORDER BY c.sort_order NULLS LAST, coalesce(c.name, d.collection) LIMIT %(limit)s"
)


class CollectionSummary(BaseModel):
    # 検索の絞り込みに渡す値（`POST /chat` の `collection`）。documents.collection と同じ
    name: str
    # 画面に出す名前。未登録のグループは name と同じ
    label: str
    description: str = ""
    # 登録した並び順（小さいほど先）。未登録は null で、登録済みの後ろに名前順で並ぶ
    sort_order: int | None = None
    # document_collections に登録されているか
    registered: bool
    document_count: int = 0
    # 検索の対象になる文書の数。0 のグループは「資料はまだありません」と出せる
    ready_count: int = 0


class CollectionList(BaseModel):
    # False = DATABASE_URL が空で、資料の DB を使っていない（エラーではない）。グループが 0 件のときの collections == [] とは別
    configured: bool
    collections: list[CollectionSummary]


def _summary(row: dict[str, Any]) -> CollectionSummary:
    label = row.get("label")
    return CollectionSummary(
        name=row["name"],
        label=label.strip() if isinstance(label, str) and label.strip() else row["name"],
        description=row.get("description") or "",
        sort_order=row.get("sort_order"),
        registered=bool(row.get("registered")),
        document_count=int(row.get("document_count") or 0),
        ready_count=int(row.get("ready_count") or 0),
    )


@router.get("", response_model=CollectionList)
async def list_collections(
    response: Response,
    access: Annotated[Access, Depends(request_access)],
) -> CollectionList:
    response.headers["Cache-Control"] = "no-store"
    access.require_any()
    pool = get_pool()
    if pool is None:
        return CollectionList(configured=False, collections=[])
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_LIST_SQL, {"limit": _MAX_COLLECTIONS, "allowed": access.scope.sql_names()})
            rows = await cursor.fetchall()
    except psycopg.errors.UndefinedTable:
        # マイグレーション 0006 の前。ほかの失敗（接続など）とは、メッセージを分ける
        logger.warning("資料グループの表がありません (UndefinedTable)")
        raise HTTPException(status_code=503, detail=_DETAIL_NOT_READY, headers=_NO_STORE) from None
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
        logger.warning("資料グループを取得できませんでした (%s)", type(exc).__name__)
        raise HTTPException(status_code=503, detail=_DETAIL_UNAVAILABLE, headers=_NO_STORE) from None
    access.scope.log_shadow("資料グループの一覧", (row["name"] for row in rows))
    # SQL で絞っているが、返す前にもう一度確かめる（範囲の外のグループは返さない）
    return CollectionList(configured=True, collections=[_summary(row) for row in rows if access.allows(row["name"])])
