"""LangGraph の checkpointer（会話履歴）を D1 に置く実装。`langgraph-checkpoint-sqlite` の `AsyncSqliteSaver` と同じ組み立てで、
接続を HTTP（`app/d1.py`）に替えたもの。バイナリの列（checkpoint・書き込みの値）は base64 の文字列で持つ。

表は `workers/api/migrations/0001_schema.sql` の `checkpoints` と `checkpoint_writes`。同期のメソッドは使わない（非同期のグラフ専用）。
"""

import base64
import json
from collections.abc import AsyncIterator, Sequence
from typing import Any, cast

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    WRITES_IDX_MAP,
    BaseCheckpointSaver,
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.serde.base import SerializerProtocol

from app.d1 import D1Pool


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _unb64(text: str | None) -> bytes:
    return base64.b64decode(text or "")


class D1Saver(BaseCheckpointSaver[str]):
    # バイナリ列と metadata の入出力。`app/pg_saver.py` の PgSaver が（bytea / jsonb 用に）置き換える
    def _enc(self, data: bytes) -> Any:
        return _b64(data)

    def _dec(self, value: Any) -> bytes:
        return _unb64(value)

    def _meta_dump(self, meta: Any) -> Any:
        return json.dumps(meta, ensure_ascii=False)

    def _meta_load(self, value: Any) -> CheckpointMetadata:
        return cast(CheckpointMetadata, json.loads(value) if value else {})

    def __init__(self, pool: D1Pool, *, serde: SerializerProtocol | None = None) -> None:
        super().__init__(serde=serde)
        self.pool = pool

    def _cfg(self, thread_id: str, ns: str, checkpoint_id: str) -> RunnableConfig:
        return {"configurable": {"thread_id": thread_id, "checkpoint_ns": ns, "checkpoint_id": checkpoint_id}}

    async def _writes(self, thread_id: str, ns: str, checkpoint_id: str) -> list[tuple[str, str, Any]]:
        rows = (
            await self.pool.batch(
                [
                    (
                        "SELECT task_id, channel, type, value FROM checkpoint_writes "
                        "WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ? ORDER BY task_id, idx",
                        (thread_id, ns, checkpoint_id),
                    )
                ]
            )
        )[0]
        return [(r["task_id"], r["channel"], self.serde.loads_typed((r["type"], self._dec(r["value"])))) for r in rows]

    def load_checkpoint(self, row: dict[str, Any]) -> Checkpoint:
        """`checkpoints` の 1 行（type と checkpoint の列）から、チェックポイント本体を復号する。壊れていれば例外。"""
        return cast(Checkpoint, self.serde.loads_typed((row["type"], self._dec(row["checkpoint"]))))

    async def _tuple(self, row: dict[str, Any], config: RunnableConfig | None = None) -> CheckpointTuple:
        thread_id, ns, checkpoint_id = row["thread_id"], row["checkpoint_ns"], row["checkpoint_id"]
        parent = row["parent_checkpoint_id"]
        return CheckpointTuple(
            config or self._cfg(thread_id, ns, checkpoint_id),
            self.load_checkpoint(row),
            self._meta_load(row["metadata"]),
            self._cfg(thread_id, ns, parent) if parent else None,
            await self._writes(thread_id, ns, checkpoint_id),
        )

    async def aget_tuple(self, config: RunnableConfig) -> CheckpointTuple | None:
        thread_id = str(config["configurable"]["thread_id"])
        ns = config["configurable"].get("checkpoint_ns", "")
        cols = "thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata"
        if checkpoint_id := get_checkpoint_id(config):
            sql = f"SELECT {cols} FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? AND checkpoint_id = ?"
            params: tuple[Any, ...] = (thread_id, ns, checkpoint_id)
        else:
            sql = f"SELECT {cols} FROM checkpoints WHERE thread_id = ? AND checkpoint_ns = ? ORDER BY checkpoint_id DESC LIMIT 1"
            params = (thread_id, ns)
        rows = (await self.pool.batch([(sql, params)]))[0]
        if not rows:
            return None
        return await self._tuple(rows[0], None if not get_checkpoint_id(config) else config)

    async def alist(
        self,
        config: RunnableConfig | None,
        *,
        filter: dict[str, Any] | None = None,
        before: RunnableConfig | None = None,
        limit: int | None = None,
    ) -> AsyncIterator[CheckpointTuple]:
        wheres: list[str] = []
        params: list[Any] = []
        if config is not None:
            wheres.append("thread_id = ?")
            params.append(str(config["configurable"]["thread_id"]))
            if (ns := config["configurable"].get("checkpoint_ns")) is not None:
                wheres.append("checkpoint_ns = ?")
                params.append(ns)
            if checkpoint_id := get_checkpoint_id(config):
                wheres.append("checkpoint_id = ?")
                params.append(checkpoint_id)
        for key, value in (filter or {}).items():
            wheres.append(
                f"json_extract(metadata, '$.{key}') IS ?" if value is None else f"json_extract(metadata, '$.{key}') = ?"
            )
            params.append(value if not isinstance(value, bool) else int(value))
        if before is not None:
            wheres.append("checkpoint_id < ?")
            params.append(get_checkpoint_id(before))
        sql = (
            "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata FROM checkpoints "
            + ("WHERE " + " AND ".join(wheres) + " " if wheres else "")
            + "ORDER BY checkpoint_id DESC"
        )
        if limit:
            sql += f" LIMIT {int(limit)}"
        for row in (await self.pool.batch([(sql, params)]))[0]:
            yield await self._tuple(row)

    async def aput(
        self,
        config: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        thread_id = str(config["configurable"]["thread_id"])
        ns = config["configurable"]["checkpoint_ns"]
        type_, blob = self.serde.dumps_typed(checkpoint)
        meta = self._meta_dump(get_checkpoint_metadata(config, metadata))
        await self.pool.batch(
            [
                (
                    "INSERT OR REPLACE INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, parent_checkpoint_id, type, checkpoint, metadata) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (
                        thread_id,
                        ns,
                        checkpoint["id"],
                        config["configurable"].get("checkpoint_id"),
                        type_,
                        self._enc(blob),
                        meta,
                    ),
                )
            ]
        )
        return self._cfg(thread_id, ns, checkpoint["id"])

    async def aput_writes(
        self, config: RunnableConfig, writes: Sequence[tuple[str, Any]], task_id: str, task_path: str = ""
    ) -> None:
        if not writes:
            return
        verb = "INSERT OR REPLACE" if all(w[0] in WRITES_IDX_MAP for w in writes) else "INSERT OR IGNORE"
        sql = (
            f"{verb} INTO checkpoint_writes (thread_id, checkpoint_ns, checkpoint_id, task_id, idx, channel, type, value) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
        )
        statements = []
        for idx, (channel, value) in enumerate(writes):
            type_, blob = self.serde.dumps_typed(value)
            statements.append(
                (
                    sql,
                    (
                        str(config["configurable"]["thread_id"]),
                        str(config["configurable"]["checkpoint_ns"]),
                        str(config["configurable"]["checkpoint_id"]),
                        task_id,
                        WRITES_IDX_MAP.get(channel, idx),
                        channel,
                        type_,
                        self._enc(blob),
                    ),
                )
            )
        await self.pool.batch(statements)

    async def adelete_thread(self, thread_id: str) -> None:
        await self.pool.batch(
            [
                ("DELETE FROM checkpoints WHERE thread_id = ?", (str(thread_id),)),
                ("DELETE FROM checkpoint_writes WHERE thread_id = ?", (str(thread_id),)),
            ]
        )

    def get_next_version(self, current: str | None, channel: None) -> str:
        import random

        current_v = 0 if current is None else (current if isinstance(current, int) else int(current.split(".")[0]))
        return f"{current_v + 1:032}.{random.random():016}"
