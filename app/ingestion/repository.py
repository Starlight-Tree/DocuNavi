"""取り込んだ文書とチャンクの保存先。

`MemoryRepository` はプロセス内（DATABASE_URL が無いときの確認用）、`PostgresRepository` は Postgres。
チャンクの再利用は `chunk_index` ではなく、文書内の `content_hash` で判定する（段落を 1 つ挿入しても、
後ろのチャンクが全部「変わった」ことにならない）。
"""

from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

from app.ingestion.chunker import Chunk


class Repository(Protocol):
    def stored_hashes(self, source: str) -> list[str] | None:
        """保存済みのチャンクのハッシュ（chunk_index の順）。文書が無ければ None。"""

    def save(self, source: str, document_hash: str, pages: int, chunks: Sequence[Chunk]) -> None:
        """文書を ready にし、チャンクを入れ替える（ハッシュが同じチャンクの行は残す）。"""

    def mark_failed(self, source: str, document_hash: str, reason: str) -> None:
        """取り込みの失敗を記録する。以前のチャンクは消さない（黙って検索対象から外さない）。"""


class MemoryRepository:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def stored_hashes(self, source: str) -> list[str] | None:
        doc = self.documents.get(source)
        return None if doc is None else [c.content_hash for c in doc["chunks"]]

    def save(self, source: str, document_hash: str, pages: int, chunks: Sequence[Chunk]) -> None:
        self.documents[source] = {
            "content_hash": document_hash,
            "status": "ready",
            "failure_reason": None,
            "pages": pages,
            "chunks": list(chunks),
        }

    def mark_failed(self, source: str, document_hash: str, reason: str) -> None:
        doc = self.documents.setdefault(source, {"chunks": [], "pages": 0})
        doc.update(content_hash=document_hash, status="failed", failure_reason=reason)


_SETUP_SQL = """
CREATE TABLE IF NOT EXISTS documents (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    source         text NOT NULL UNIQUE,
    content_hash   text NOT NULL,
    status         text NOT NULL CHECK (status IN ('ready', 'failed')),
    failure_reason text,
    pages          integer NOT NULL DEFAULT 0,
    created_at     timestamptz NOT NULL DEFAULT now(),
    updated_at     timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS chunks (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    document_id  bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
    chunk_index  integer NOT NULL,
    page         integer NOT NULL,
    heading      text,
    content      text NOT NULL,
    content_hash text NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    UNIQUE (document_id, chunk_index) DEFERRABLE INITIALLY DEFERRED
);
CREATE INDEX IF NOT EXISTS chunks_document_hash_idx ON chunks (document_id, content_hash);
"""


class PostgresRepository:
    """psycopg の同期接続で、文書とチャンクを保存する。接続は呼び出し側が開閉する（autocommit なし）。"""

    def __init__(self, conn: Any, *, create_tables: bool) -> None:
        self._conn = conn
        if create_tables:
            self._setup()

    def _setup(self) -> None:
        with self._conn.transaction():
            # 複数のコマンドが同時に初めて動いても、DDL が競合しないようにする
            self._conn.execute("SELECT pg_advisory_xact_lock(hashtext('app.ingestion.setup'))")
            self._conn.execute(_SETUP_SQL)

    def stored_hashes(self, source: str) -> list[str] | None:
        import psycopg

        try:
            with self._conn.transaction():
                doc = self._conn.execute("SELECT id FROM documents WHERE source = %s", (source,)).fetchone()
                if doc is None:
                    return None
                rows = self._conn.execute(
                    "SELECT content_hash FROM chunks WHERE document_id = %s ORDER BY chunk_index", (doc[0],)
                ).fetchall()
        except psycopg.errors.UndefinedTable:
            return None  # まだ取り込んだことが無い（確認だけのときは、テーブルを作らない）
        return [row[0] for row in rows]

    def save(self, source: str, document_hash: str, pages: int, chunks: Sequence[Chunk]) -> None:
        with self._conn.transaction():
            doc_id = self._upsert_document(source, document_hash, "ready", None, pages)
            existing = self._conn.execute(
                "SELECT id, content_hash FROM chunks WHERE document_id = %s ORDER BY chunk_index", (doc_id,)
            ).fetchall()
            available: dict[str, list[int]] = {}
            for row_id, digest in existing:
                available.setdefault(digest, []).append(row_id)

            keep: list[tuple[int, Chunk]] = []
            insert: list[Chunk] = []
            for chunk in chunks:
                ids = available.get(chunk.content_hash)
                if ids:
                    keep.append((ids.pop(0), chunk))
                else:
                    insert.append(chunk)
            leftover = [row_id for ids in available.values() for row_id in ids]

            if leftover:
                self._conn.execute("DELETE FROM chunks WHERE id = ANY(%s)", (leftover,))
            for row_id, chunk in keep:
                # (document_id, chunk_index) の一意制約は、コミット時に検査される（入れ替えで一時的に重なってよい）
                self._conn.execute(
                    "UPDATE chunks SET chunk_index = %s, page = %s, heading = %s WHERE id = %s",
                    (chunk.index, chunk.page, chunk.heading, row_id),
                )
            for chunk in insert:
                self._conn.execute(
                    "INSERT INTO chunks (document_id, chunk_index, page, heading, content, content_hash) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (doc_id, chunk.index, chunk.page, chunk.heading, chunk.text, chunk.content_hash),
                )

    def mark_failed(self, source: str, document_hash: str, reason: str) -> None:
        with self._conn.transaction():
            self._upsert_document(source, document_hash, "failed", reason, None)

    def _upsert_document(
        self, source: str, document_hash: str, status: str, reason: str | None, pages: int | None
    ) -> int:
        row = self._conn.execute(
            """
            INSERT INTO documents (source, content_hash, status, failure_reason, pages)
            VALUES (%s, %s, %s, %s, COALESCE(%s, 0))
            ON CONFLICT (source) DO UPDATE SET
                content_hash = EXCLUDED.content_hash,
                status = EXCLUDED.status,
                failure_reason = EXCLUDED.failure_reason,
                pages = COALESCE(%s, documents.pages),
                updated_at = now()
            RETURNING id
            """,
            (source, document_hash, status, reason, pages, pages),
        ).fetchone()
        return row[0]


def diff_counts(stored: list[str] | None, new: list[str]) -> tuple[int, int, int]:
    """(新しいチャンク数, そのまま使えるチャンク数, 消えるチャンク数)。同じ内容が複数あっても数で数える。"""
    old_count, new_count = Counter(stored or []), Counter(new)
    unchanged = sum((old_count & new_count).values())
    return len(new) - unchanged, unchanged, len(stored or []) - unchanged
