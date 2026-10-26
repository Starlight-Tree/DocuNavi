"""入寮規程の条文の関係図（読み取り専用。実験）。

- `GET /documents/{id}/graph`: 章・条・項・脚注のノードと、関係の辺
- `GET /documents/{id}/impact?node=…&hops=2`: そのノードを変えたとき、依存をたどって影響が及ぶ先
- 対象は入寮規程（`tsubasa_dorm_rules.pdf`）だけ。他の資料は 404
- DB には何も保存しない。元の PDF（名前と内容のハッシュが DB と合うもの。`app/pdf_originals.py`）を、リクエストのたびに読んで計算する
- `AUTH_MODE=required` のときは `/documents` と同じ検証（`current_user`）を通った人だけが読める。`ACCESS_MODE=enforce` で
  見えないグループの資料は、存在しないのと同じ 404（対象外の資料の 404 と文が違うと存在が分かるので、先に見える資料かを確かめる）
- 例外の文字列・サーバーのパスは返さない（固定メッセージ）
"""

import asyncio
import logging
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Response
from pydantic import BaseModel

from app.access import Access, request_access
from app.api.documents import (
    _DETAIL_NO_ORIGINAL,
    _DETAIL_NOT_CONFIGURED,
    _DETAIL_NOT_FOUND,
    _DETAIL_ORIGINAL_MISMATCH,
    _DETAIL_ORIGINAL_UNAVAILABLE,
    _DOCUMENT_SQL,
    _MAX_ID,
    _no_store,
    _unavailable,
)
from app.history import get_pool
from app.pdf_originals import find_original
from app.relations.graph import Analysis, Impact, NotAnImpactSource, UnknownNode, analyze_dorm_rules, impact_of

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/documents")

SUPPORTED_SOURCE = "tsubasa_dorm_rules.pdf"
MAX_HOPS = 10

_DETAIL_UNSUPPORTED = "この資料は、関係図の対象ではありません。"
_DETAIL_UNREADABLE = "条文の関係を読み取れませんでした。"
_DETAIL_UNKNOWN_NODE = "指定されたノードが、この資料にありません。"
_DETAIL_CHAPTER = "章は、影響の起点にできません。項・条・脚注を指定してください。"

# ノードの id（章 chN / 条 aN / 項 aNpM / 脚注 fnN）。形が違うものは、規程を読む前に 422 にする
_NODE_ID = r"^(?:ch\d{1,3}|a\d{1,3}(?:p\d{1,3})?|fn\d{1,3})$"


class GraphNode(BaseModel):
    id: str
    type: str  # chapter / article / paragraph / footnote
    label: str
    text: str | None = None
    parent: str | None = None
    page: int | None = None


class GraphEdge(BaseModel):
    # 向きは「src が dst に依存する」。type は references / exception_of / same_chapter / uses_term
    type: str
    src: str
    dst: str
    evidence: str
    page: int
    extracted_by: str
    updated_at: str


class GraphSource(BaseModel):
    id: int
    source: str
    title: str | None = None
    updated_at: datetime | None = None


class GraphResponse(BaseModel):
    document: GraphSource
    title: str | None = None
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class ImpactedNode(BaseModel):
    id: str
    hops: int


class ImpactResponse(BaseModel):
    document: GraphSource
    node: str
    hops: int
    start: list[str]
    impacted: list[ImpactedNode]
    articles: list[str]
    edges: list[GraphEdge]


def _edge(e) -> GraphEdge:
    return GraphEdge(
        type=e.type,
        src=e.src,
        dst=e.dst,
        evidence=e.evidence,
        page=e.page,
        extracted_by=e.extracted_by,
        updated_at=e.updated_at,
    )


async def _load(document_id: int, access: Access) -> tuple[GraphSource, Analysis]:
    access.require_any()
    pool = get_pool()
    if pool is None:
        raise HTTPException(status_code=503, detail=_DETAIL_NOT_CONFIGURED)
    try:
        async with pool.connection() as conn:
            cursor = await conn.execute(_DOCUMENT_SQL, {"id": document_id})
            rows = await cursor.fetchall()
    except Exception as exc:  # noqa: BLE001 - 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
        raise _unavailable(exc) from None
    if not rows:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    row = rows[0]
    access.require_visible(row.get("collection"), "関係図", _DETAIL_NOT_FOUND)
    if row["source"] != SUPPORTED_SOURCE:
        raise HTTPException(status_code=404, detail=_DETAIL_UNSUPPORTED)
    original = await asyncio.to_thread(find_original, row["source"], row.get("content_hash"))
    if not original.ok or original.data is None:
        if original.reason == "mismatch":
            raise HTTPException(status_code=409, detail=_DETAIL_ORIGINAL_MISMATCH)
        if original.reason == "unavailable":
            raise HTTPException(status_code=503, detail=_DETAIL_ORIGINAL_UNAVAILABLE)
        raise HTTPException(status_code=404, detail=_DETAIL_NO_ORIGINAL)
    try:
        analysis = await asyncio.to_thread(analyze_dorm_rules, original.data)
    except Exception as exc:  # noqa: BLE001 - PDF の読み取りや条文の分割の失敗。型の名前だけを残す
        logger.warning("条文の関係を読み取れませんでした (%s)", type(exc).__name__)
        raise HTTPException(status_code=500, detail=_DETAIL_UNREADABLE) from None
    title = row.get("title")
    source = GraphSource(
        id=row["id"],
        source=row["source"],
        title=title.strip() or None if isinstance(title, str) else None,
        updated_at=row.get("updated_at"),
    )
    return source, analysis


@router.get("/{document_id}/graph", response_model=GraphResponse)
async def document_graph(
    document_id: Annotated[int, Path(ge=1, le=_MAX_ID)],
    response: Response,
    access: Annotated[Access, Depends(request_access)],
) -> GraphResponse:
    _no_store(response)
    source, analysis = await _load(document_id, access)
    return GraphResponse(
        document=source,
        title=analysis.title,
        nodes=[GraphNode(**vars(n)) for n in analysis.nodes],
        edges=[_edge(e) for e in analysis.edges],
    )


@router.get("/{document_id}/impact", response_model=ImpactResponse)
async def document_impact(
    document_id: Annotated[int, Path(ge=1, le=_MAX_ID)],
    response: Response,
    access: Annotated[Access, Depends(request_access)],
    node: Annotated[str, Query(pattern=_NODE_ID)],
    hops: Annotated[int, Query(ge=1, le=MAX_HOPS)] = 2,
) -> ImpactResponse:
    _no_store(response)
    source, analysis = await _load(document_id, access)
    try:
        result: Impact = impact_of(analysis, node, hops)
    except UnknownNode:
        raise HTTPException(status_code=404, detail=_DETAIL_UNKNOWN_NODE) from None
    except NotAnImpactSource:
        raise HTTPException(status_code=422, detail=_DETAIL_CHAPTER) from None
    return ImpactResponse(
        document=source,
        node=node,
        hops=hops,
        start=list(result.start),
        impacted=[ImpactedNode(id=i.id, hops=i.hops) for i in result.impacted],
        articles=list(result.articles),
        edges=[_edge(e) for e in result.edges],
    )
