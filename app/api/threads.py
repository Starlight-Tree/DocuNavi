"""会話の一覧と、本人による削除。

- `GET /threads`: ログイン中の利用者の会話を、新しい順に返す。`AUTH_MODE=required` のときだけ使える。匿名（off）の会話には
  持ち主がなく、一覧にすると他人の会話が見えてしまうので、404 を返す
- `GET /threads/{thread_id}/messages`: 会話の発言を古い順に返す（ユーザーと最終回答だけ。ツールの結果は含めない）。
  required では本人の会話だけ（他人の ID は存在しないのと同じ 404）。off では `thread_id` を知っていることが権限
- `DELETE /threads/{thread_id}`: 会話を 1 件消す。required では本人の会話だけ（他人の ID は存在しないのと同じ 404）。
  off では `/chat` と同じく、`thread_id` を知っていることが権限
- `ACCESS_MODE=enforce` のとき、会話の発言は本人のものでも、いま見えないグループの検索結果に基づく回答は固定文に置き換える
  （`app/access.py` の `mask_hidden_turns`）。会話に保存された検索対象がいま見えないグループなら、`collection` は null で返す
- 題名は、会話の最初のユーザー発言の先頭（新しい列も LLM 呼び出しも使わない）。履歴のチェックポイントから読む
- 履歴の保存先は Postgres かメモリ（`app/history.py`）。どちらも LangGraph の checkpointer を通して読み書きする
- 例外の文字列は応答に含めない（固定メッセージ）
"""

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Path, Response
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, Field

from app.access import Access, request_access
from app.api import routes
from app.auth import conversation_key, current_user
from app.history import get_checkpointer, get_pool

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/threads")

# 一覧に出す会話の上限（新しい順）。題名のために会話ごとに履歴を読むので、件数を抑える
MAX_THREADS = 50
# 題名を読む同時数。接続プールの借り出しを占有しない（チャットが待たされない）ための上限
_TITLE_CONCURRENCY = 3
MAX_TITLE_LENGTH = 40

_DETAIL_NOT_AVAILABLE = "会話の一覧は、ログイン（AUTH_MODE=required）のときだけ使えます。"
_DETAIL_NOT_FOUND = "指定された会話が見つかりません。"
_DETAIL_UNAVAILABLE = "会話履歴を操作できませんでした。しばらく待ってから再試行してください。"

# 一覧の元になる、会話ごとの最初と最後のチェックポイントの時刻。prefix は `<利用者 ID>:`（UUID で検証済み）
_LIST_SQL = (
    "SELECT thread_id, min((checkpoint->>'ts')::timestamptz) AS created_at, "
    "max((checkpoint->>'ts')::timestamptz) AS updated_at "
    "FROM checkpoints WHERE starts_with(thread_id, %(prefix)s) AND checkpoint_ns = '' "
    "GROUP BY thread_id ORDER BY max((checkpoint->>'ts')::timestamptz) DESC NULLS LAST, thread_id LIMIT %(limit)s"
)


class ThreadSummary(BaseModel):
    thread_id: str = Field(description="会話ID。`/chat` に送ると会話が続く（利用者 ID の接頭辞は付かない）")
    title: str = Field(description="最初のユーザー発言の先頭")
    created_at: datetime | None = Field(description="最初のチェックポイントの時刻。無ければ null")
    updated_at: datetime | None = Field(description="最後のチェックポイントの時刻。無ければ null")


class ThreadList(BaseModel):
    threads: list[ThreadSummary]


class ThreadMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str


class MessageList(BaseModel):
    messages: list[ThreadMessage]
    collection: str | None = Field(
        default=None,
        description="この会話の検索対象の資料のグループ。null は「すべて」（この項目が無い古い会話も null）",
    )


def visible_messages(messages: list[Any]) -> list[ThreadMessage]:
    """画面に出す発言だけ（古い順）。ユーザーの発言と、ツール呼び出しの無い（最終回答の）AI の発言で、本文が空のものは除く。

    ツールの結果（検索で引いた資料の本文）と、ツールを呼ぶだけの発話は、画面に出さない。
    """
    shown: list[ThreadMessage] = []
    for message in messages:
        if isinstance(message, HumanMessage):
            role = "user"
        elif isinstance(message, AIMessage) and not message.tool_calls:
            role = "assistant"
        else:
            continue
        text = message.text.strip()
        if text:
            shown.append(ThreadMessage(role=role, text=text))
    return shown


def _saver() -> BaseCheckpointSaver:
    # DB 履歴なら開いている checkpointer、メモリなら /chat が使うグラフが持つ MemorySaver
    return get_checkpointer() or routes.get_graph(routes.current_mode() == "mock").checkpointer


def _unavailable(exc: BaseException) -> HTTPException:
    # 例外の文字列には接続先・SQL が入りうる。型の名前だけを残す
    logger.warning("会話履歴を操作できませんでした (%s)", type(exc).__name__)
    return HTTPException(status_code=503, detail=_DETAIL_UNAVAILABLE)


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content
            if isinstance(part, str) or isinstance(part, dict)
        )
    return ""


def make_title(messages: list[Any]) -> str:
    """最初のユーザー発言を、空白をまとめて MAX_TITLE_LENGTH 文字までにする。"""
    for message in messages:
        if isinstance(message, HumanMessage):
            text = re.sub(r"\s+", " ", _text_of(message.content)).strip()
            if text:
                return text if len(text) <= MAX_TITLE_LENGTH else text[: MAX_TITLE_LENGTH - 1] + "…"
    return "（題名なし）"


def _parse_ts(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


async def _recent_threads_postgres(prefix: str) -> list[tuple[str, datetime | None, datetime | None]]:
    async with get_pool().connection() as conn:
        cursor = await conn.execute(_LIST_SQL, {"prefix": prefix, "limit": MAX_THREADS})
        rows = await cursor.fetchall()
    return [(row["thread_id"], row["created_at"], row["updated_at"]) for row in rows]


async def _recent_threads_memory(saver: BaseCheckpointSaver, prefix: str) -> list[tuple[str, Any, Any]]:
    times: dict[str, list[datetime]] = {}
    async for item in saver.alist(None):
        key = item.config["configurable"]["thread_id"]
        if item.config["configurable"].get("checkpoint_ns", "") or not key.startswith(prefix):
            continue
        ts = _parse_ts(item.checkpoint.get("ts"))
        times.setdefault(key, [])
        if ts is not None:
            times[key].append(ts)
    found = [(key, min(stamps, default=None), max(stamps, default=None)) for key, stamps in times.items()]
    found.sort(key=lambda row: (row[2] or datetime.min.replace(tzinfo=UTC), row[0]), reverse=True)
    return found[:MAX_THREADS]


@router.get("", response_model=ThreadList)
async def list_threads(user_id: Annotated[str | None, Depends(current_user)]) -> ThreadList:
    if user_id is None:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_AVAILABLE)
    prefix = f"{user_id}:"
    saver = _saver()
    gate = asyncio.Semaphore(_TITLE_CONCURRENCY)

    async def summarize(key: str, created: datetime | None, updated: datetime | None) -> ThreadSummary:
        async with gate:
            latest = await saver.aget_tuple({"configurable": {"thread_id": key}})
        messages = latest.checkpoint.get("channel_values", {}).get("messages", []) if latest else []
        return ThreadSummary(
            thread_id=key[len(prefix) :], title=make_title(messages), created_at=created, updated_at=updated
        )

    try:
        rows = (
            await _recent_threads_postgres(prefix)
            if get_pool() is not None
            else await _recent_threads_memory(saver, prefix)
        )
        threads = await asyncio.gather(*(summarize(*row) for row in rows))
    except Exception as exc:  # noqa: BLE001 - 固定メッセージにする
        raise _unavailable(exc) from None
    return ThreadList(threads=list(threads))


@router.get("/{thread_id}/messages", response_model=MessageList)
async def get_thread_messages(
    thread_id: Annotated[str, Path(max_length=routes.MAX_THREAD_ID_LENGTH, pattern=r"^[A-Za-z0-9_-]+$")],
    user_id: Annotated[str | None, Depends(current_user)],
    access: Annotated[Access, Depends(request_access)],
) -> MessageList:
    key = conversation_key(user_id, thread_id)
    try:
        latest = await _saver().aget_tuple({"configurable": {"thread_id": key}})
    except Exception as exc:  # noqa: BLE001 - 固定メッセージにする
        raise _unavailable(exc) from None
    if latest is None:
        raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
    values = latest.checkpoint.get("channel_values", {})
    # 会話は表示するが、いま見えないグループの検索結果に基づく回答は伏せる。保存された検索対象が見えないグループなら、名前も返さない
    shown = access.mask(values.get("messages", []), "会話の表示")
    saved = values.get("collection")
    return MessageList(messages=visible_messages(shown), collection=saved if access.allows(saved) else None)


@router.delete("/{thread_id}", status_code=204)
async def delete_thread(
    thread_id: Annotated[str, Path(max_length=routes.MAX_THREAD_ID_LENGTH, pattern=r"^[A-Za-z0-9_-]+$")],
    user_id: Annotated[str | None, Depends(current_user)],
) -> Response:
    key = conversation_key(user_id, thread_id)
    saver = _saver()
    try:
        if await saver.aget_tuple({"configurable": {"thread_id": key}}) is None:
            raise HTTPException(status_code=404, detail=_DETAIL_NOT_FOUND)
        await saver.adelete_thread(key)
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - 固定メッセージにする
        raise _unavailable(exc) from None
    return Response(status_code=204)
