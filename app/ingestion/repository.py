"""取り込んだ文書とチャンクの保存先。

`MemoryRepository` はプロセス内（D1_API_URL が無いときの確認用）、`D1Repository` は D1。
チャンクの再利用は `chunk_index` ではなく、文書内の `content_hash` で判定する（段落を 1 つ挿入しても、
後ろのチャンクが全部「変わった」ことにならない）。
"""

import json
from collections import Counter
from collections.abc import Sequence
from typing import Any, Protocol

from app.d1 import MAX_BATCH
from app.ingestion.chunker import Chunk


class _KeepTitle:
    """`save()` に題名を渡さなかった印。保存済みの題名をそのまま残す（None を渡すと「題名なし」として消す）。"""

    def __repr__(self) -> str:
        return "KEEP_TITLE"


KEEP_TITLE = _KeepTitle()
Title = str | None | _KeepTitle


class Repository(Protocol):
    def stored_hashes(self, source: str) -> list[str] | None:
        """保存済みのチャンクのハッシュ（chunk_index の順）。文書が無ければ None。"""

    def save(
        self,
        source: str,
        document_hash: str,
        pages: int,
        chunks: Sequence[Chunk],
        *,
        title: Title = KEEP_TITLE,
        collection: str | None = None,
    ) -> None:
        """文書を ready にし、チャンクを入れ替える（ハッシュが同じチャンクの行は残す）。

        題名: 渡さない（KEEP_TITLE）なら保存済みのまま、文字列ならそれに、None なら題名なしにする。
        資料群（collection）: 文字列ならそれに上書きし、None（渡さない）なら保存済みのまま（消さない）。
        """

    def mark_failed(self, source: str, document_hash: str, reason: str) -> None:
        """取り込みの失敗を記録する。以前のチャンクは消さない（黙って検索対象から外さない）。"""


class MemoryRepository:
    def __init__(self) -> None:
        self.documents: dict[str, dict[str, Any]] = {}

    def stored_hashes(self, source: str) -> list[str] | None:
        doc = self.documents.get(source)
        return None if doc is None else [c.content_hash for c in doc["chunks"]]

    def save(
        self,
        source: str,
        document_hash: str,
        pages: int,
        chunks: Sequence[Chunk],
        *,
        title: Title = KEEP_TITLE,
        collection: str | None = None,
    ) -> None:
        previous = self.documents.get(source, {}).get("title")
        previous_collection = self.documents.get(source, {}).get("collection")
        self.documents[source] = {
            "content_hash": document_hash,
            "status": "ready",
            "failure_reason": None,
            "pages": pages,
            "title": previous if isinstance(title, _KeepTitle) else title,
            "collection": collection if collection is not None else previous_collection,
            "chunks": list(chunks),
        }

    def mark_failed(self, source: str, document_hash: str, reason: str) -> None:
        doc = self.documents.setdefault(source, {"chunks": [], "pages": 0})
        doc.update(content_hash=document_hash, status="failed", failure_reason=reason)


class D1Repository:
    """D1（データ用 Worker 経由）に、文書とチャンクを保存する。表は `workers/api/migrations/` が作る。

    対話的なトランザクションは HTTP 越しにできないので、読んでから 1 回の `batch`（1 トランザクション）で書く。
    消えるチャンクのベクトルは Vectorize からも消す（検索の結果に古い行が残らないように）。
    """

    def __init__(self, client: Any) -> None:
        self._db = client

    def stored_hashes(self, source: str) -> list[str] | None:
        docs = self._db.execute("SELECT id FROM documents WHERE source = ?", (source,))
        if not docs:
            return None
        rows = self._db.execute(
            "SELECT content_hash FROM chunks WHERE document_id = ? ORDER BY chunk_index", (docs[0]["id"],)
        )
        return [row["content_hash"] for row in rows]

    def save(
        self,
        source: str,
        document_hash: str,
        pages: int,
        chunks: Sequence[Chunk],
        *,
        title: Title = KEEP_TITLE,
        collection: str | None = None,
    ) -> None:
        doc_id = self._upsert_document(source, document_hash, "ready", None, pages, title, collection)
        existing = self._db.execute(
            "SELECT id, content_hash FROM chunks WHERE document_id = ? ORDER BY chunk_index", (doc_id,)
        )
        available: dict[str, list[int]] = {}
        for row in existing:
            available.setdefault(row["content_hash"], []).append(row["id"])

        keep: list[tuple[int, Chunk]] = []
        insert: list[Chunk] = []
        for chunk in chunks:
            ids = available.get(chunk.content_hash)
            if ids:
                keep.append((ids.pop(0), chunk))
            else:
                insert.append(chunk)
        leftover = [row_id for ids in available.values() for row_id in ids]

        statements: list[tuple[str, Sequence[Any]]] = []
        # (document_id, chunk_index) の一意制約は文ごとに検査される。入れ替えで重ならないよう、残す行は先に退避の番号へ動かす
        statements.append(("UPDATE chunks SET chunk_index = -id WHERE document_id = ?", (doc_id,)))
        if leftover:
            statements.append(
                ("DELETE FROM chunks WHERE id IN (SELECT value FROM json_each(?))", (json.dumps(leftover),))
            )
        for row_id, chunk in keep:
            statements.append(
                (
                    "UPDATE chunks SET chunk_index = ?, page = ?, page_end = ?, kind = ?, heading = ? WHERE id = ?",
                    (chunk.index, chunk.page, chunk.page_end, chunk.kind, chunk.heading, row_id),
                )
            )
        for chunk in insert:
            statements.append(
                (
                    "INSERT INTO chunks (document_id, chunk_index, page, page_end, kind, heading, content, content_hash) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        doc_id,
                        chunk.index,
                        chunk.page,
                        chunk.page_end,
                        chunk.kind,
                        chunk.heading,
                        chunk.text,
                        chunk.content_hash,
                    ),
                )
            )
        for i in range(0, len(statements), MAX_BATCH):
            self._db.batch(statements[i : i + MAX_BATCH])
        if leftover:
            self._db.vectors_delete([str(row_id) for row_id in leftover])

    def mark_failed(self, source: str, document_hash: str, reason: str) -> None:
        self._upsert_document(source, document_hash, "failed", reason, None, KEEP_TITLE)

    def _upsert_document(
        self,
        source: str,
        document_hash: str,
        status: str,
        reason: str | None,
        pages: int | None,
        title: Title,
        collection: str | None = None,
    ) -> int:
        # 題名は、渡されたとき（文字列・None）だけ上書きする。渡されない（KEEP_TITLE）なら、保存済みのまま
        keep = isinstance(title, _KeepTitle)
        new_title = None if keep else title
        rows = self._db.execute(
            """
            INSERT INTO documents (source, content_hash, status, failure_reason, pages, title, collection)
            VALUES (?, ?, ?, ?, COALESCE(?, 0), ?, ?)
            ON CONFLICT (source) DO UPDATE SET
                content_hash = excluded.content_hash,
                status = excluded.status,
                failure_reason = excluded.failure_reason,
                pages = COALESCE(?, documents.pages),
                title = CASE WHEN ? THEN documents.title ELSE excluded.title END,
                collection = COALESCE(excluded.collection, documents.collection),
                updated_at = unixepoch() * 1000
            RETURNING id
            """,
            (source, document_hash, status, reason, pages, new_title, collection, pages, int(keep)),
        )
        return rows[0]["id"]


def diff_counts(stored: list[str] | None, new: list[str]) -> tuple[int, int, int]:
    """(新しいチャンク数, そのまま使えるチャンク数, 消えるチャンク数)。同じ内容が複数あっても数で数える。"""
    old_count, new_count = Counter(stored or []), Counter(new)
    unchanged = sum((old_count & new_count).values())
    return len(new) - unchanged, unchanged, len(stored or []) - unchanged
