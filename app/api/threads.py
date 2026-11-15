"""会話の一覧と、本人による削除。

- `GET /threads`: ログイン中の利用者の会話を、新しい順に返す。`AUTH_MODE=required` のときだけ使える。匿名（off）の会話には
  持ち主がなく、一覧にすると他人の会話が見えてしまうので、404 を返す
- `GET /threads/{thread_id}/messages`: 会話の発言を古い順に返す（ユーザーと最終回答だけ。ツールの結果は含めないが、最終回答には、
  その質問の最後の検索から取り出した出典〔`context` と `sources`〕を付ける）。
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
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from pydantic import BaseModel, Field, ValidationError

from app.access import Access, request_access, target_is_visible
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

# 一覧の元になる、会話ごとの最初と最後のチェックポイントの時刻と、最新のチェックポイント本体（題名の元）を、1 回で取る。
# checkpoint は bytea で持つので、時刻は checkpoints.created_at（timestamptz。`app/pg.py` が UNIX ミリ秒にして返す）を使う。
# prefix は `<利用者 ID>:`（UUID で検証済み）。LIKE のワイルドカードを含まないので、先頭一致に使える。
# 最新 = checkpoint_id の最大（`aget_tuple` の `ORDER BY checkpoint_id DESC LIMIT 1` と同じ列・同じ照合順序。主キーなので 1 行に決まる）。
# 集計で最大の ID を取り、主キー（thread_id, checkpoint_ns, checkpoint_id）で本体を引く（LATERAL は使わない）。
# 外側の ORDER BY は必須（CTE の並びは JOIN のあとに保証されない）。同時刻のときは thread_id の昇順（従来と同じ）。
_LIST_SQL = (
    "WITH recent AS ("
    " SELECT thread_id, min(created_at) AS created_at, max(created_at) AS updated_at, max(checkpoint_id) AS latest_id"
    " FROM checkpoints WHERE substr(thread_id, 1, length(%(prefix)s)) = %(prefix)s AND checkpoint_ns = ''"
    " GROUP BY thread_id ORDER BY max(created_at) DESC, thread_id LIMIT %(limit)s) "
    "SELECT r.thread_id, r.created_at, r.updated_at, c.type, c.checkpoint "
    "FROM recent r JOIN checkpoints c "
    "ON c.thread_id = r.thread_id AND c.checkpoint_ns = '' AND c.checkpoint_id = r.latest_id "
    "ORDER BY r.updated_at DESC, r.thread_id"
)


class ThreadSummary(BaseModel):
    thread_id: str = Field(description="会話ID。`/chat` に送ると会話が続く（利用者 ID の接頭辞は付かない）")
    title: str = Field(description="最初のユーザー発言の先頭")
    created_at: datetime | None = Field(description="最初のチェックポイントの時刻。無ければ null")
    updated_at: datetime | None = Field(description="最後のチェックポイントの時刻。無ければ null")


class ThreadList(BaseModel):
    threads: list[ThreadSummary]


class ThreadSource(BaseModel):
    """回答の出典 1 件。`/chat/stream` の `message-metadata.sources` の項目から、`target` を除いたもの（保存済みの検索結果の artifact から取り出す。画面は `target` を使わず、見えない対象の名前を返さないため）。"""

    name: str
    page: int | None = None
    page_end: int | None = None
    page_label: str | None = None  # PDF のページラベル（表紙・i・1 など）。保存されていない古い会話は null
    page_end_label: str | None = None
    weak: bool = False
    collection: str | None = None
    document_id: int | None = None
    title: str | None = None  # 資料の題名（画面の表示用）。保存されていない古い会話・題名が無い資料は null


class ThreadMessage(BaseModel):
    role: Literal["user", "assistant"]
    text: str
    context: str | None = Field(
        default=None,
        description="assistant のみ。その回答の検索結果（`/chat/stream` の `message-metadata.context` と同じ形）。出典が無い回答・伏せた回答では項目が無い",
    )
    sources: list[ThreadSource] | None = Field(
        default=None,
        description="assistant のみ。`context` の出典と同じ順のページなど。出典が無い回答・伏せた回答・保存されていない古い会話では項目が無い",
    )


class MessageList(BaseModel):
    messages: list[ThreadMessage]
    collection: str | None = Field(
        default=None,
        description=(
            "この会話の検索対象の資料のグループ（旧方式）。null は絞らない（この項目が無い古い会話も null）。"
            "いま見えないグループなら null（名前は返さない。`scope_unavailable` が true になる）"
        ),
    )
    target: str | None = Field(
        default=None,
        description=(
            "この会話の質問の対象（documents.target）。null は絞らない（この項目が無い古い会話も null）。"
            "いま見える資料が無い対象なら null（名前は返さない。`scope_unavailable` が true になる）"
        ),
    )
    scope_unavailable: bool = Field(
        default=False,
        description=(
            "保存された検索範囲（collection か target）が、いま利用できない（見えない・資料が無い）。true のとき、collection と target は"
            "null だが「すべて」ではない。続きを送ると、サーバーは保存された範囲のまま探す（資料が無ければ 0 件）"
        ),
    )


def _turn_references(group: list[ToolMessage]) -> tuple[str, list[ThreadSource]] | None:
    """検索結果（ToolMessage の連なり 1 組）から、回答の出典（context と sources）を取り出す。出典が無ければ None。

    `graph.py` の `tools` ノードが、1 回の検索（連なる ToolMessage）の本文を空行で連ねて context にし、artifact の並びを sources にしている。
    ここでは、それと同じ作り方で組み直す。伏せた検索結果（artifact が空）は出典にならない。
    """
    items: list[Any] = []
    for message in group:
        artifact = getattr(message, "artifact", None)
        if isinstance(artifact, list):
            items.extend(artifact)
    if not items or not all(isinstance(item, dict) for item in items):
        return None
    try:
        sources = [ThreadSource.model_validate(item) for item in items]
    except ValidationError:
        return None
    return "\n\n".join(str(message.content) for message in group), sources


def visible_messages(messages: list[Any], *, with_sources: bool = True) -> list[ThreadMessage]:
    """画面に出す発言だけ（古い順）。ユーザーの発言と、ツール呼び出しの無い（最終回答の）AI の発言で、本文が空のものは除く。

    ツールの結果（検索で引いた資料の本文）と、ツールを呼ぶだけの発話は、画面に出さない。ただし、最終回答には、その質問の最後の検索
    （直前に連なる ToolMessage の 1 組。画面の「参照資料」と同じ範囲）から取り出した出典（`context` と `sources`）を付ける。
    `with_sources=False`（保存された検索範囲がいま見えないとき）は、出典の資料名・グループを返さないため、付けない。
    """
    shown: list[ThreadMessage] = []
    group: list[ToolMessage] = []  # この質問の、最後の検索結果の連なり
    after_tool = False
    for message in messages:
        if isinstance(message, ToolMessage):
            group = group + [message] if after_tool else [message]
            after_tool = True
            continue
        after_tool = False
        if isinstance(message, HumanMessage):
            group = []
            role = "user"
        elif isinstance(message, AIMessage) and not message.tool_calls:
            role = "assistant"
        else:
            continue
        text = message.text.strip()
        if not text:
            continue
        references = _turn_references(group) if role == "assistant" and with_sources else None
        if references is None:
            shown.append(ThreadMessage(role=role, text=text))
        else:
            shown.append(ThreadMessage(role=role, text=text, context=references[0], sources=references[1]))
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


def _from_ms(value: Any) -> datetime | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=UTC)
    return None


async def _recent_threads_postgres(
    prefix: str, saver: Any | None = None
) -> list[tuple[str, datetime | None, datetime | None, list[Any]]]:
    """新しい順の会話（最大 MAX_THREADS 件）と、各会話の最新のチェックポイントの発言。DB への問い合わせは、会話の数によらず 1 回。

    saver（`load_checkpoint` を持つ PgSaver / D1Saver）を渡すと、最新のチェックポイントを復号して発言を返す（復号できなければ例外）。
    渡さなければ復号せず、発言は空（時刻だけ読む呼び出し用）。
    """
    async with get_pool().connection() as conn:
        cursor = await conn.execute(_LIST_SQL, {"prefix": prefix, "limit": MAX_THREADS})
        rows = await cursor.fetchall()
    return [
        (
            row["thread_id"],
            _from_ms(row["created_at"]),
            _from_ms(row["updated_at"]),
            [] if saver is None else saver.load_checkpoint(row).get("channel_values", {}).get("messages", []),
        )
        for row in rows
    ]


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
        if get_pool() is not None:
            # Postgres: 題名の元は一覧と同じ 1 回の問い合わせで届く（会話ごとの aget_tuple は呼ばない）
            found = await _recent_threads_postgres(prefix, saver)
            threads = [
                ThreadSummary(
                    thread_id=key[len(prefix) :], title=make_title(messages), created_at=created, updated_at=updated
                )
                for key, created, updated, messages in found
            ]
        else:
            rows = await _recent_threads_memory(saver, prefix)
            threads = list(await asyncio.gather(*(summarize(*row) for row in rows)))
    except Exception as exc:  # noqa: BLE001 - 固定メッセージにする
        raise _unavailable(exc) from None
    return ThreadList(threads=threads)


@router.get("/{thread_id}/messages", response_model=MessageList, response_model_exclude_unset=True)
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
    saved_target = values.get("target")
    collection = saved if access.allows(saved) else None
    target = saved_target
    if isinstance(saved_target, str):
        try:
            target = saved_target if await target_is_visible(access.scope, saved_target) else None
        except Exception as exc:  # noqa: BLE001 - 固定メッセージにする
            raise _unavailable(exc) from None
    unavailable = (saved is not None and collection is None) or (saved_target is not None and target is None)
    return MessageList(
        messages=visible_messages(shown, with_sources=not unavailable),
        collection=collection,
        target=target,
        scope_unavailable=unavailable,
    )


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
