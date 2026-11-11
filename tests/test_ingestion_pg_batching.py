"""Postgres での、まとめ書き・まとめ読みの確認（Prisma Postgres の operations は SQL 1 本が 1。件数に比例させない）。

本物の Postgres（`PG_TEST_URL`）があるときだけ動く。使い捨てのデータベースを作る（tests/test_history_pg_backend.py の `pg_url`）。
"""

import asyncio

from app.ingestion import embedding
from app.ingestion.chunker import Chunk
from app.ingestion.repository import INSERT_ROWS, D1Repository
from app.pg import PgPool, SyncPg
from app.pg_saver import PgSaver
from tests.test_history_pg_backend import _seed, needs_pg, pg_url  # noqa: F401 - pg_url は fixture


def _chunk(i: int, text: str, heading: str | None = None) -> Chunk:
    return Chunk(index=i, page=1, page_end=None, kind="text", heading=heading, text=text, content_hash=f"hash-{text}")


@needs_pg
def test_prefetch_reads_documents_and_chunk_hashes_in_one_statement(pg_url):  # noqa: F811
    with SyncPg(pg_url) as db:
        repo = D1Repository(db)
        repo.save("a.pdf", "da", 1, [_chunk(0, "one"), _chunk(1, "two")], title="A", collection="g")
        repo.save("b.pdf", "db", 1, [_chunk(0, "three")], title="B", collection="g")
        db.execute(
            "INSERT INTO documents (source, content_hash, status, pages) VALUES ('empty.pdf', 'de', 'failed', 0)"
        )  # チャンクの無い文書
        before = db.statements
        repo.prefetch(["a.pdf", "b.pdf", "empty.pdf", "missing.pdf"])
        assert db.statements - before == 1
        assert repo.stored_hashes("a.pdf") == ["hash-one", "hash-two"]
        assert repo.stored_hashes("b.pdf") == ["hash-three"]
        assert repo.stored_hashes("empty.pdf") == []
        assert repo.stored_hashes("missing.pdf") is None
        state = repo.document_state("a.pdf")
        assert state is not None and (state.content_hash, state.status, state.title, state.collection) == (
            "da",
            "ready",
            "A",
            "g",
        )
        assert repo.document_state("missing.pdf") is None
        assert db.statements - before == 1  # prefetch した文書は、読み直さない


@needs_pg
def test_saving_many_new_chunks_uses_a_few_statements(pg_url):  # noqa: F811
    n = INSERT_ROWS * 2 + 5
    with SyncPg(pg_url) as db:
        repo = D1Repository(db)
        before = db.statements
        repo.save("big.pdf", "dh", 3, [_chunk(i, f"text-{i}", heading=f"H{i}" if i % 2 else None) for i in range(n)])
        used = db.statements - before
        rows = db.execute("SELECT chunk_index, heading, content FROM chunks ORDER BY chunk_index")
        assert [r["chunk_index"] for r in rows] == list(range(n))
        assert rows[1]["heading"] == "H1" and rows[0]["heading"] is None and rows[7]["content"] == "text-7"
        assert used <= 8  # 文書の upsert、既存の読み取り、退避の UPDATE、複数行 INSERT 3 本（件数 205 に比例しない）


@needs_pg
def test_checkpoint_writes_are_one_insert_and_survive_repeated_special_channels(pg_url):  # noqa: F811
    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            saver = PgSaver(pool)
            cfg = {"configurable": {"thread_id": "t1", "checkpoint_ns": "", "checkpoint_id": "c1"}}
            await saver.aput_writes(cfg, [("alpha", {"x": 1}), ("beta", "two"), ("alpha", 3)], "task-a")  # IGNORE 経路
            # すべて特別な通道（固定の idx）。同じ通道が 1 本の文に 2 回入っても、後の値が残り、失敗しない
            await saver.aput_writes(cfg, [("__error__", "first"), ("__error__", "second")], "task-b")
            await saver.aput_writes(cfg, [("__error__", "third")], "task-b")  # 既存の行を置き換える
            rows = (
                await pool.batch(
                    [("SELECT task_id, idx, channel FROM checkpoint_writes ORDER BY task_id, idx", None)],
                )
            )[0]
            tuple_ = await saver._writes("t1", "", "c1")
            return rows, tuple_
        finally:
            await pool.close()

    rows, writes = asyncio.run(run())
    assert [(r["task_id"], r["channel"]) for r in rows if r["task_id"] == "task-a"] == [
        ("task-a", "alpha"),
        ("task-a", "beta"),
        ("task-a", "alpha"),
    ]
    assert len([r for r in rows if r["task_id"] == "task-b"]) == 1
    assert [w[2] for w in writes if w[0] == "task-b"] == ["third"]


@needs_pg
def test_embeddings_are_written_in_one_statement_per_hundred_and_skip_stale_rows(pg_url):  # noqa: F811
    _seed(pg_url)
    with SyncPg(pg_url) as db:
        db.execute("UPDATE chunks SET embedding_model = NULL, embedding_dimensions = NULL, embedding_input_hash = NULL")
        db.execute("UPDATE chunks SET embedding = NULL")
        store = embedding.D1EmbeddingStore(db)
        pending = store.pending_embeddings(10)
        assert len(pending) == 3
        vector = [0.5] + [0.0] * (embedding.DIMENSIONS - 1)
        rows = [(chunk, vector) for chunk in pending]
        # 1 件は、取り込みで見出しが替わった（埋め込みを作ったときの入力と食い違う）ので、更新されない
        db.execute("UPDATE chunks SET heading = 'changed' WHERE id = ?", (pending[0].id,))
        before = db.statements
        saved = store.save_embeddings(rows)
        assert db.statements - before == 1
        assert saved == 2
        done = {
            r["id"]: r
            for r in db.execute(
                "SELECT id, embedding IS NOT NULL AS has_vector, embedding_model, embedding_dimensions, embedding_input_hash FROM chunks"
            )
        }
        assert done[pending[0].id]["has_vector"] is False and done[pending[0].id]["embedding_model"] is None
        for chunk in pending[1:]:
            row = done[chunk.id]
            assert row["has_vector"] is True
            assert (row["embedding_model"], row["embedding_dimensions"]) == (embedding.MODEL, embedding.DIMENSIONS)
            assert row["embedding_input_hash"] == chunk.input_hash
        assert store.count_pending() == 1  # 見出しが替わった 1 件だけが、次の回の対象
