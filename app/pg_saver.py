"""LangGraph の checkpointer（会話履歴）を Postgres に置く実装。`D1Saver`（`app/d1_saver.py`）と同じ組み立てで、
バイナリの列（checkpoint・書き込みの値）は bytea、metadata は jsonb で持つ（D1 では base64 の文字列）。

表は `db/migrations/0001_schema.sql` の `checkpoints` と `checkpoint_writes`（LangGraph 標準の PostgresSaver の表とは別物。名前が衝突するので使わない）。
SQL は D1Saver と同じ文で、`app/pg.py` の `translate()` が Postgres の書き方に直す。
"""

from typing import Any, cast

from langgraph.checkpoint.base import CheckpointMetadata

from app.d1_saver import D1Saver


class PgSaver(D1Saver):
    def _enc(self, data: bytes) -> Any:
        return data

    def _dec(self, value: Any) -> bytes:
        return bytes(value) if value is not None else b""

    def _meta_load(self, value: Any) -> CheckpointMetadata:
        if isinstance(value, dict):  # jsonb は dict で返る
            return cast(CheckpointMetadata, value)
        return super()._meta_load(value)
