"""Postgres 版の窓口（`app/pg.py`・`app/pg_saver.py`）のテスト。

- `translate()` の単体テスト（SQLite の書き方 → Postgres）は、いつでも動く。
- 本物の Postgres（pgvector 入り）を使うテストは、環境変数 `PG_TEST_URL`（サーバーに入れる接続文字列。例 `postgresql://test@127.0.0.1:54329/postgres`）が
  あるときだけ動く。テストごとに使い捨てのデータベースを作り、`db/migrations/` の全ファイルを番号順に適用して使う（接続先の既存のデータには触らない）。
  アプリの SQL（`app/` の各モジュールの定数）を、そのまま Postgres で実行して確かめる。
"""

import asyncio
import os
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.pg import PgPool, SyncPg, _filter_sql, translate

MIGRATIONS = sorted((Path(__file__).resolve().parent.parent / "db" / "migrations").glob("*.sql"))  # 番号順に全部
PG_TEST_URL = os.environ.get("PG_TEST_URL", "")

# ---- translate()（Postgres は要らない） ----


def test_positional_placeholders_become_percent_s_but_not_inside_string_literals():
    assert (
        translate("SELECT 1 FROM t WHERE a = ? AND b = '?' AND c = ?")
        == "SELECT 1 FROM t WHERE a = %s AND b = '?' AND c = %s"
    )


def test_json_each_in_becomes_jsonb_elements_for_named_and_positional():
    named = translate("WHERE d.id IN (SELECT value FROM json_each(%(ids)s)) AND x = 1")
    assert named == "WHERE d.id::text IN (SELECT jsonb_array_elements_text(%(ids)s::jsonb)) AND x = 1"
    pos = translate("DELETE FROM chunks WHERE id IN (SELECT value FROM json_each(?))")
    assert pos == "DELETE FROM chunks WHERE id::text IN (SELECT jsonb_array_elements_text(%s::jsonb))"
    fn = translate("AND coalesce(c.name, d.collection) IN (SELECT value FROM json_each(%(a)s))")
    assert "coalesce(c.name, d.collection)::text IN (SELECT jsonb_array_elements_text(%(a)s::jsonb))" in fn


def test_unixepoch_becomes_now():
    assert translate("SET updated_at = unixepoch() * 1000 WHERE id = ?") == "SET updated_at = now() WHERE id = %s"
    age = translate("HAVING max(created_at) < (unixepoch() - ? * 86400) * 1000")
    assert age == "HAVING max(created_at) < (now() - make_interval(secs => %s * 86400))"


def test_insert_or_replace_and_ignore_become_on_conflict():
    replace = translate(
        "INSERT OR REPLACE INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint) VALUES (?, ?, ?, ?, ?)"
    )
    assert replace.startswith(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint) VALUES (%s, %s, %s, %s, %s)"
    )
    assert (
        "ON CONFLICT (thread_id, checkpoint_ns, checkpoint_id) DO UPDATE SET type = EXCLUDED.type, checkpoint = EXCLUDED.checkpoint"
        in replace
    )
    ignore = translate("INSERT OR IGNORE INTO checkpoint_writes (thread_id, idx) VALUES (?, ?)")
    assert ignore.endswith("VALUES (%s, %s) ON CONFLICT DO NOTHING")
    with pytest.raises(ValueError):
        translate("INSERT OR REPLACE INTO other (a) VALUES (?)")


def test_a_parameter_used_only_with_is_null_is_pinned_to_text():
    assert (
        translate("WHERE (%(a)s IS NULL OR x = %(a)s) AND ? IS NOT NULL")
        == "WHERE (%(a)s::text IS NULL OR x = %(a)s) AND %s::text IS NOT NULL"
    )


def test_json_extract_filters_and_literal_percent():
    assert translate("WHERE json_extract(metadata, '$.step') = ?") == "WHERE (metadata ->> 'step') = %s::text"
    assert "IS NOT DISTINCT FROM" in translate("WHERE json_extract(metadata, '$.x') IS ?")
    assert translate("WHERE a LIKE 'x%' AND b = ?") == "WHERE a LIKE 'x%%' AND b = %s"
    assert translate("SELECT '100%'", with_params=False) == "SELECT '100%'"


def test_vector_filter_becomes_sql_and_rejects_unknown_keys():
    where, params = _filter_sql({"collection": {"$in": ["a", "b"]}, "target": "t", "document_id": {"$in": [1, 2]}})
    assert where == " AND d.collection = ANY(%s::text[]) AND d.target = %s AND d.id = ANY(%s::bigint[])"
    assert params == [["a", "b"], "t", [1, 2]]
    assert _filter_sql(None) == ("", [])
    with pytest.raises(ValueError):
        _filter_sql({"other": 1})
    with pytest.raises(ValueError):
        _filter_sql({"collection": {"$nin": ["a"]}})


# ---- 本物の Postgres ----

needs_pg = pytest.mark.skipif(not PG_TEST_URL, reason="PG_TEST_URL が未設定（本物の Postgres を使うテスト）")


@pytest.fixture
def pg_url():
    import psycopg
    from psycopg import sql

    name = "pgt_" + uuid.uuid4().hex[:12]
    with psycopg.connect(PG_TEST_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    base = PG_TEST_URL.rsplit("/", 1)[0]
    url = f"{base}/{name}"
    with psycopg.connect(url, autocommit=True) as c:
        for migration in MIGRATIONS:
            c.execute(migration.read_text(encoding="utf-8"))
    yield url
    with psycopg.connect(PG_TEST_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


def _vec(*head: float) -> list[float]:
    return [*head] + [0.0] * (768 - len(head))


def _seed(url: str) -> None:
    """資料 3 件（グループ a / a / b、対象 x / y / x）とチャンク各 1 件、アクセス制御と分類。"""
    from app.ingestion import embedding

    with SyncPg(url) as db:
        db.batch(
            [
                ("INSERT INTO document_collections (name, label) VALUES ('a', 'A'), ('b', 'B')", None),
                ("INSERT INTO document_targets (name, label, sort_order) VALUES ('x', 'X', 1), ('y', 'Y', 2)", None),
                ("INSERT INTO document_types (name, label) VALUES ('t', 'T')", None),
                (
                    "INSERT INTO documents (source, content_hash, status, collection, target, doc_type, pages) VALUES "
                    "('d1.pdf', 'h1', 'ready', 'a', 'x', 't', 1), ('d2.pdf', 'h2', 'ready', 'a', 'y', 't', 1), "
                    "('d3.pdf', 'h3', 'ready', 'b', 'x', NULL, 1)",
                    None,
                ),
                ("INSERT INTO roles (name, label) VALUES ('default', 'D'), ('staff', 'S')", None),
                ("INSERT INTO role_collection_access (role, collection) VALUES ('default', 'a'), ('staff', 'b')", None),
            ]
        )
        for n, source in enumerate(["d1.pdf", "d2.pdf", "d3.pdf"], start=1):
            doc = db.execute("SELECT id FROM documents WHERE source = ?", (source,))[0]["id"]
            db.execute(
                "INSERT INTO chunks (document_id, chunk_index, page, kind, heading, content, content_hash, "
                "embedding_model, embedding_dimensions, embedding_input_hash) VALUES (?, 0, 1, 'text', ?, ?, ?, ?, ?, ?)",
                (
                    doc,
                    f"h{n}",
                    f"body {n}",
                    f"c{n}",
                    embedding.MODEL,
                    embedding.DIMENSIONS,
                    embedding.input_hash(f"h{n}", f"body {n}"),
                ),
            )
        ids = {r["content_hash"]: r["id"] for r in db.execute("SELECT id, content_hash FROM chunks")}
        db.vectors_upsert(
            [
                {"id": str(ids["c1"]), "values": _vec(1.0, 0.0)},
                {"id": str(ids["c2"]), "values": _vec(0.9, 0.1)},
                {"id": str(ids["c3"]), "values": _vec(0.0, 1.0)},
            ]
        )


@needs_pg
def test_pool_batch_is_one_transaction_and_rows_have_the_d1_shape(pg_url):
    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            await pool.batch([("INSERT INTO roles (name, label) VALUES (?, ?)", ("r1", "R1"))])
            with pytest.raises(Exception, match="DB の呼び出しに失敗しました"):
                await pool.batch(
                    [
                        ("INSERT INTO roles (name, label) VALUES (?, ?)", ("r2", "R2")),
                        ("INSERT INTO roles (name, label) VALUES (?, ?)", ("r1", "dup")),  # 主キーの重複
                    ]
                )
            rows = (await pool.batch([("SELECT name, label FROM roles ORDER BY name", None)]))[0]
            stamped = (
                await pool.batch(
                    [("INSERT INTO document_collections (name, label) VALUES ('c', 'C') RETURNING created_at", None)]
                )
            )[0]
            return rows, stamped
        finally:
            await pool.close()

    rows, stamped = asyncio.run(run())
    assert rows == [{"name": "r1", "label": "R1"}]  # 失敗した束は丸ごと戻る
    assert isinstance(stamped[0]["created_at"], int) and stamped[0]["created_at"] > 1_700_000_000_000  # unix ミリ秒


@needs_pg
def test_vectors_query_orders_by_cosine_similarity_and_applies_the_filter(pg_url):
    _seed(pg_url)

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            everything = await pool.vectors_query(_vec(1.0, 0.0), 10)
            only_a = await pool.vectors_query(_vec(1.0, 0.0), 10, {"collection": {"$in": ["a"]}})
            target_y = await pool.vectors_query(_vec(1.0, 0.0), 10, {"target": "y"})
            one_doc = await pool.vectors_query(_vec(1.0, 0.0), 10, {"document_id": {"$in": [1]}})
            return everything, only_a, target_y, one_doc
        finally:
            await pool.close()

    everything, only_a, target_y, one_doc = asyncio.run(run())
    assert [round(s, 3) for _, s in everything] == [1.0, 0.994, 0.0]  # c1, c2, c3
    assert len(only_a) == 2 and len(target_y) == 1 and len(one_doc) == 1
    assert all(isinstance(i, str) for i, _ in everything)


@needs_pg
def test_search_runs_the_real_sql_with_scope_target_and_document_filters(pg_url, monkeypatch):
    from app import history
    from app.scope import Scope
    from app.search import vector as search_vector

    _seed(pg_url)

    async def fake_embed(query: str) -> list[float]:
        return _vec(1.0, 0.0)

    monkeypatch.setattr(search_vector, "embed_query", fake_embed)

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        history._pool = pool
        try:
            all_hits = await search_vector.search("q", 10, scope=Scope.ALL)
            a_only = await search_vector.search("q", 10, scope=Scope.only({"a"}))
            nothing = await search_vector.search("q", 10, scope=Scope.only({"zzz"}))
            with_target = await search_vector.search("q", 10, scope=Scope.ALL, target="y")
            with_doc = await search_vector.search("q", 10, scope=Scope.ALL, document_ids=[3])
            collection = await search_vector.search("q", 10, scope=Scope.ALL, collection="b")
            return all_hits, a_only, nothing, with_target, with_doc, collection
        finally:
            history._pool = None
            await pool.close()

    all_hits, a_only, nothing, with_target, with_doc, collection = asyncio.run(run())
    assert [h.source for h in all_hits] == ["d1.pdf", "d2.pdf"]  # d3 は直交（類似度 0 < 0.65）
    assert [h.source for h in a_only] == ["d1.pdf", "d2.pdf"]
    assert nothing == []
    assert [h.source for h in with_target] == ["d2.pdf"]
    assert with_doc == []  # d3 は類似度が足りない
    assert collection == []


@needs_pg
def test_every_application_query_runs_on_postgres(pg_url, monkeypatch):
    """アプリの各モジュールの SQL を、そのまま Postgres で実行する（構文・型の誤りがあれば例外になる）。"""
    from app import access, history_admin, startup_checks
    from app.api import classifications, collections, documents, graph, threads
    from app.scope import ALLOWED_FILTER_SQL
    from app.search import pipeline
    from app.search import vector as search_vector

    _seed(pg_url)

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        out: dict[str, Any] = {}
        try:
            async with pool.connection() as conn:

                async def q(name: str, sql: str, params: Any = None) -> list[dict[str, Any]]:
                    out[name] = await (await conn.execute(sql, params)).fetchall()
                    return out[name]

                await q("visible", access._VISIBLE_SQL, {"uid": "u1", "default": "default"})
                await q("known", access._KNOWN_SQL)
                await q("probe", access._PROBE_SQL, {"default": "default"})
                await q("target_visible", access._TARGET_VISIBLE_SQL, {"target": "x", "allowed": ["a"]})
                await q("collections", collections._LIST_SQL, {"limit": 50, "allowed": ["a", "b"]})
                await q("collections_all", collections._LIST_SQL, {"limit": 50, "allowed": None})
                await q("targets", classifications._TARGETS_SQL, {"limit": 50, "allowed": ["a"]})
                await q("types", classifications._TYPES_SQL, {"limit": 50, "allowed": None})
                await q("documents", documents._LIST_SQL, {"limit": 50, "allowed": ["a"]})
                await q("document", documents._DOCUMENT_SQL, {"id": 1})
                await q("doc_chunks", documents._CHUNKS_SQL, {"id": 1, "limit": 10})
                await q("hero", documents._HERO_SQL, {"id": 1})
                await q("graph_doc", graph._DOCUMENT_SQL, {"id": 1})
                await q("verify", search_vector._VERIFY_SQL)
                await q("startup", startup_checks.COMBINED_SQL, {"default": "default"})
                await q("threads", threads._LIST_SQL, {"prefix": "u1:", "limit": 10})
                params = {"collection": None, "target": None, "doc_ids": None, "allowed": None}
                await q("corpus", pipeline._CORPUS_SQL, {**params, "limit": 10, "offset": 0})
                await q("membership", pipeline._MEMBERSHIP_SQL, {**params, "target": "x"})
                await q(
                    "corpus_filtered",
                    pipeline._CORPUS_SQL,
                    {**params, "doc_ids": [1, 2], "allowed": ["a"], "limit": 10, "offset": 0},
                )
                await q(
                    "allowed_filter", f"SELECT d.id FROM documents d WHERE {ALLOWED_FILTER_SQL}", {"allowed": ["b"]}
                )
        finally:
            await pool.close()
        return out

    out = asyncio.run(run())
    assert [r["collection"] for r in out["visible"]] == ["a"]  # u1 にロールが無い → default の範囲
    assert {r["name"] for r in out["collections"]} == {"a", "b"}
    assert [r["name"] for r in out["targets"]] == ["x", "y"] and out["targets"][0]["document_count"] == 1
    assert len(out["documents"]) == 2
    assert len(out["corpus_filtered"]) == 2 and len(out["corpus"]) == 3
    assert [r["document_id"] for r in out["membership"]] == [1, 3]
    assert [r["id"] for r in out["allowed_filter"]] == [3]
    assert isinstance(out["threads"], list)
    assert history_admin._STALE_THREADS_SQL  # 下で SyncPg が実行する


@needs_pg
def test_repository_saves_replaces_and_marks_failed_on_postgres(pg_url):
    from app.ingestion.chunker import Chunk
    from app.ingestion.repository import D1Repository, diff_counts

    def chunk(i: int, text: str) -> Chunk:
        return Chunk(index=i, page=1, page_end=None, kind="text", heading=None, text=text, content_hash=f"hash-{text}")

    with SyncPg(pg_url) as db:
        repo = D1Repository(db)
        repo.save(
            "a.pdf", "dh1", 2, [chunk(0, "one"), chunk(1, "two"), chunk(2, "three")], title="Title", collection=None
        )
        assert repo.stored_hashes("a.pdf") == ["hash-one", "hash-two", "hash-three"]
        before = {r["content_hash"]: r["id"] for r in db.execute("SELECT id, content_hash FROM chunks")}
        # 段落を 1 つ挿入し、末尾を消す: 残るチャンクの行（ID）は再利用される
        repo.save("a.pdf", "dh2", 2, [chunk(0, "one"), chunk(1, "new"), chunk(2, "two")])
        after = {r["content_hash"]: r["id"] for r in db.execute("SELECT id, content_hash FROM chunks")}
        assert repo.stored_hashes("a.pdf") == ["hash-one", "hash-new", "hash-two"]
        assert (
            after["hash-one"] == before["hash-one"]
            and after["hash-two"] == before["hash-two"]
            and "hash-three" not in after
        )
        doc = db.execute("SELECT status, title, content_hash, updated_at FROM documents WHERE source = ?", ("a.pdf",))[
            0
        ]
        assert (
            doc["status"] == "ready" and doc["title"] == "Title" and doc["content_hash"] == "dh2"
        )  # 題名は渡さなければそのまま
        repo.save("a.pdf", "dh3", 2, [chunk(0, "one")], title=None)
        assert db.execute("SELECT title FROM documents WHERE source = ?", ("a.pdf",))[0]["title"] is None
        repo.mark_failed("a.pdf", "dh4", "boom")
        failed = db.execute("SELECT status, failure_reason FROM documents WHERE source = ?", ("a.pdf",))[0]
        assert failed == {"status": "failed", "failure_reason": "boom"}
        assert repo.stored_hashes("a.pdf") == ["hash-one"]  # 失敗でもチャンクは消さない
        assert diff_counts(["a"], ["a", "b"]) == (1, 1, 0)
        # 履歴の掃除の SQL（unixepoch の置き換え）
        from app import history_admin

        assert db.execute(history_admin._STALE_THREADS_SQL, (1,)) == []


@needs_pg
def test_hero_image_statements_run_on_postgres(pg_url):
    from app import hero_images as hi

    _seed(pg_url)
    with SyncPg(pg_url) as db:
        doc = db.execute(hi.SELECT_DOCUMENT_SQL, (1,))[0]
        assert doc["content_hash"] == "h1"
        new_id = db.execute(hi.INSERT_SQL, (1, "hero", "image/png", "k1", 10, 1, 1, "h1", "a" * 64, "m"))[0]["id"]
        db.execute(hi.APPROVE_SQL, (new_id,))
        approved = db.execute(hi.SELECT_IMAGE_SQL, (new_id,))[0]
        assert approved["status"] == "approved"
        other = db.execute(hi.INSERT_SQL, (1, "hero", "image/png", "k2", 10, 1, 1, "h1", "b" * 64, "m"))[0]["id"]
        db.execute(hi.DEMOTE_SQL, (1, "hero", other))
        db.execute(hi.APPROVE_SQL, (other,))
        statuses = {r["id"]: r["status"] for r in db.execute(hi.LIST_SQL)}
        assert statuses == {new_id: "rejected", other: "approved"}
        row = db.execute(hi.LIST_SQL)[0]
        assert isinstance(row["created_at"], int)


@needs_pg
def test_embedding_sync_statements_run_on_postgres(pg_url):
    from app.ingestion import embedding

    _seed(pg_url)
    with SyncPg(pg_url) as db:
        rows = db.execute("SELECT c.id AS id FROM chunks c WHERE c.id IN (SELECT value FROM json_each(?))", ("[1, 2]",))
        assert [r["id"] for r in rows] == [1, 2]
    assert embedding  # モジュールの SQL（?）は上と同じ構文


@needs_pg
def test_saved_embeddings_survive_closing_the_connection(pg_url):
    """`write_embeddings` が commit しないと、行数は返るのに、接続を閉じたあと保存が消える（2026-11-15 に本番で発生）。別の接続から読み直す。"""
    from app.ingestion import embedding

    _seed(pg_url)
    with SyncPg(pg_url) as db:
        db.execute(
            "INSERT INTO chunks (document_id, chunk_index, page, kind, heading, content, content_hash) "
            "VALUES ((SELECT id FROM documents WHERE source = 'd1.pdf'), 1, 1, 'table', 'toc', 'contents', 'c-new')"
        )
        new_id = db.execute("SELECT id FROM chunks WHERE content_hash = 'c-new'")[0]["id"]
        saved = db.write_embeddings(
            [(new_id, "c-new", "toc", embedding.input_hash("toc", "contents"), _vec(0.5, 0.5))],
            embedding.MODEL,
            embedding.DIMENSIONS,
        )
    assert saved == 1
    with SyncPg(pg_url) as db:
        row = db.execute(
            "SELECT embedding_model, embedding_input_hash, embedding IS NOT NULL AS has FROM chunks WHERE id = ?",
            (new_id,),
        )[0]
    assert row["embedding_model"] == embedding.MODEL
    assert row["embedding_input_hash"] == embedding.input_hash("toc", "contents")
    assert row["has"] is True


@needs_pg
def test_pg_saver_keeps_a_conversation_and_deletes_it(pg_url):
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    from app.pg_saver import PgSaver

    def reply(state: MessagesState):
        return {"messages": [AIMessage(content=f"answer {len(state['messages'])}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("reply", reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            saver = PgSaver(pool)
            graph = builder.compile(checkpointer=saver)
            config = {"configurable": {"thread_id": "u1:t1"}}
            await graph.ainvoke({"messages": [HumanMessage("hello")]}, config)
            await graph.ainvoke({"messages": [HumanMessage("again")]}, config)
            await graph.ainvoke({"messages": [HumanMessage("other")]}, {"configurable": {"thread_id": "u1:t2"}})
            latest = await saver.aget_tuple(config)
            ids = [t.config["configurable"]["checkpoint_id"] async for t in saver.alist(config)]
            listed = (
                await pool.batch(
                    [
                        (
                            "SELECT thread_id, min(created_at) AS c, max(created_at) AS u FROM checkpoints GROUP BY thread_id ORDER BY thread_id",
                            None,
                        )
                    ]
                )
            )[0]
            await saver.adelete_thread("u1:t1")
            return (
                latest,
                ids,
                listed,
                await saver.aget_tuple(config),
                await saver.aget_tuple({"configurable": {"thread_id": "u1:t2"}}),
            )
        finally:
            await pool.close()

    latest, ids, listed, after, other = asyncio.run(run())
    assert [m.content for m in latest.checkpoint["channel_values"]["messages"]] == [
        "hello",
        "answer 1",
        "again",
        "answer 3",
    ]
    assert len(ids) >= 4 and ids == sorted(ids, reverse=True)
    assert latest.parent_config is not None and isinstance(latest.metadata, dict)
    assert [r["thread_id"] for r in listed] == ["u1:t1", "u1:t2"] and all(isinstance(r["u"], int) for r in listed)
    assert after is None and other is not None


@needs_pg
def test_thread_list_is_one_query_and_matches_aget_tuple_on_postgres(pg_url, monkeypatch):
    """GET /threads の Postgres の経路。最新のチェックポイントは aget_tuple と同じ（checkpoint_id の最大）。問い合わせは 1 回。"""
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, MessagesState, StateGraph

    from app.api import threads
    from app.pg_saver import PgSaver

    def reply(state: MessagesState):
        return {"messages": [AIMessage(content=f"answer {len(state['messages'])}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("reply", reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)

    def cfg(thread_id: str) -> dict:
        return {"configurable": {"thread_id": thread_id}}

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            saver = PgSaver(pool)
            graph = builder.compile(checkpointer=saver)
            # 作った順: t1（1 回目）→ t2 → t3 → t1（2 回目）。新しい順は t1, t3, t2
            await graph.ainvoke({"messages": [HumanMessage("first of t1")]}, cfg("u1:t1"))
            await graph.ainvoke({"messages": [HumanMessage("first of t2")]}, cfg("u1:t2"))
            await graph.ainvoke({"messages": [HumanMessage("first of t3")]}, cfg("u1:t3"))
            await graph.ainvoke({"messages": [HumanMessage("second of t1")]}, cfg("u1:t1"))
            await graph.ainvoke({"messages": [HumanMessage("other user")]}, cfg("u2:t1"))
            await graph.ainvoke(
                {"messages": [HumanMessage("prefix lookalike")]}, cfg("u10:t9")
            )  # "u1" で始まるが "u1:" ではない

            calls: list[Any] = []
            real_batch = pool.batch

            async def counting_batch(statements):
                calls.append(statements)
                return await real_batch(statements)

            monkeypatch.setattr(pool, "batch", counting_batch)
            monkeypatch.setattr(threads, "get_pool", lambda: pool)
            listed = await threads._recent_threads_postgres("u1:", saver)
            queries = len(calls)
            monkeypatch.setattr(threads, "MAX_THREADS", 2)
            capped = await threads._recent_threads_postgres("u1:", saver)
            monkeypatch.undo()

            latest = {key: await saver.aget_tuple(cfg(key)) for key, *_ in listed}
            return listed, queries, capped, latest
        finally:
            await pool.close()

    listed, queries, capped, latest = asyncio.run(run())

    assert queries == 1  # 会話が 3 件でも 1 回
    assert [key for key, *_ in listed] == ["u1:t1", "u1:t3", "u1:t2"]  # 新しい順。他の利用者・接頭辞が似た会話は出ない
    assert [key for key, *_ in capped] == ["u1:t1", "u1:t3"]  # MAX_THREADS
    for key, created, updated, title, messages in listed:
        expected = latest[key].checkpoint["channel_values"]["messages"]  # aget_tuple が返す最新のチェックポイント
        assert title == threads.make_title(expected)  # 保存時に metadata へ入れた題名（aput）
        assert latest[key].metadata["title"] == title
        assert messages == []  # 題名があれば、本体は返さず復号もしない
        assert created is not None and updated is not None and created <= updated
    latest_t1 = latest["u1:t1"].checkpoint["channel_values"]["messages"]
    assert [m.content for m in latest_t1] == [
        "first of t1",
        "answer 1",
        "second of t1",
        "answer 3",
    ]  # 最新の状態（2 回目のあと）
    assert listed[0][3] == "first of t1"


@needs_pg
def test_thread_list_skips_the_checkpoint_body_when_a_title_is_stored_and_falls_back_when_not(pg_url, monkeypatch):
    """題名が metadata にある行は、本体（checkpoint）を SQL が返さない。無い行（この機能より前の行）だけ、本体を返して復号する。"""
    import psycopg
    from langchain_core.messages import AIMessage, HumanMessage
    from langgraph.graph import END, START, MessagesState, StateGraph
    from psycopg.rows import dict_row

    from app.api import threads
    from app.pg_saver import PgSaver

    def reply(state: MessagesState):
        return {"messages": [AIMessage(content="answer")]}

    builder = StateGraph(MessagesState)
    builder.add_node("reply", reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            saver = PgSaver(pool)
            graph = builder.compile(checkpointer=saver)
            await graph.ainvoke(
                {"messages": [HumanMessage("  新しい\n質問   です ")]}, {"configurable": {"thread_id": "u1:new"}}
            )
            await graph.ainvoke({"messages": [HumanMessage("古い質問です")]}, {"configurable": {"thread_id": "u1:old"}})
            monkeypatch.setattr(threads, "get_pool", lambda: pool)
            with psycopg.connect(pg_url, autocommit=True, row_factory=dict_row) as c:
                # 古い会話の題名を、この機能より前の状態（metadata に title が無い）に戻す
                c.execute("UPDATE checkpoints SET metadata = metadata - 'title' WHERE thread_id = 'u1:old'")
                rows = {
                    r["thread_id"]: r for r in c.execute(threads._LIST_SQL, {"prefix": "u1:", "limit": 10}).fetchall()
                }
            listed = {
                key: (title, messages)
                for key, _, _, title, messages in await threads._recent_threads_postgres("u1:", saver)
            }
            times_only = {
                key: (title, messages) for key, _, _, title, messages in await threads._recent_threads_postgres("u1:")
            }
            return rows, listed, times_only
        finally:
            await pool.close()

    rows, listed, times_only = asyncio.run(run())

    assert rows["u1:new"]["title"] == "新しい 質問 です" and rows["u1:new"]["checkpoint"] is None  # 本体は返らない
    assert rows["u1:old"]["title"] is None and rows["u1:old"]["checkpoint"] is not None  # 古い行は本体を返す
    assert listed["u1:new"] == ("新しい 質問 です", [])
    old_title, old_messages = listed["u1:old"]
    assert old_title is None and threads.make_title(old_messages) == "古い質問です"  # 復号して、同じ題名になる
    assert times_only["u1:old"] == (None, [])  # saver が無ければ復号しない


@needs_pg
def test_the_checkpoint_metadata_filter_runs_on_postgres(pg_url):
    from langgraph.checkpoint.base import empty_checkpoint

    from app.pg_saver import PgSaver

    async def run():
        pool = PgPool(pg_url)
        await pool.open()
        try:
            saver = PgSaver(pool)
            cfg = {"configurable": {"thread_id": "t", "checkpoint_ns": ""}}
            await saver.aput(cfg, empty_checkpoint(), {"source": "input", "step": 1}, {})
            hit = [t async for t in saver.alist(cfg, filter={"source": "input"})]
            miss = [t async for t in saver.alist(cfg, filter={"source": "loop"})]
            return len(hit), len(miss)
        finally:
            await pool.close()

    assert asyncio.run(run()) == (1, 0)
