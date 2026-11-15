"""D1 / Vectorize の HTTP クライアント（app/d1.py）と、それに載せた保存先の検証。

データ用 Worker の偽物（tests/d1_fakes.py。本物の SQLite に migration を適用）を相手にするので、SQL の書き方（SQLite の方言）まで確かめる。
Cloudflare には何も送らない。
"""

import asyncio

import pytest
from langchain_core.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph

from app import d1, history
from app.d1 import D1Error, D1Pool, SyncD1
from app.d1_saver import D1Saver
from app.ingestion import embedding
from app.ingestion.chunker import Chunk, content_hash
from app.ingestion.embedding import D1EmbeddingStore, PendingChunk
from app.ingestion.repository import D1Repository
from tests.d1_fakes import FakeWorker


def pool_for(worker: FakeWorker) -> D1Pool:
    return D1Pool("https://worker.test", transport=worker.transport())


def sync_for(worker: FakeWorker) -> SyncD1:
    return SyncD1("https://worker.test", transport=worker.transport())


# ---- 引数の変換 ----


def test_named_parameters_become_positional_in_order_of_appearance():
    sql, values = d1.convert("SELECT %(a)s, %(b)s, %(a)s", {"a": 1, "b": "x"})
    assert sql == "SELECT ?, ?, ?" and values == [1, "x", 1]


def test_lists_and_sets_become_json_and_none_stays_null():
    _, values = d1.convert("%(l)s %(s)s %(n)s %(t)s", {"l": [3, 1], "s": {"b", "a"}, "n": None, "t": True})
    assert values == ["[3, 1]", '["a", "b"]', None, 1]


def test_positional_parameters_pass_through():
    assert d1.convert("SELECT ?", (5,)) == ("SELECT ?", [5])
    assert d1.convert("SELECT 1", None) == ("SELECT 1", [])


# ---- 接続 ----


def test_batch_runs_in_one_transaction_and_a_failure_rolls_everything_back(d1_worker):
    pool = pool_for(d1_worker)

    async def run():
        await pool.batch([("INSERT INTO roles (name, label) VALUES (?, ?)", ("a", "A"))])
        with pytest.raises(D1Error):
            await pool.batch(
                [
                    ("INSERT INTO roles (name, label) VALUES (?, ?)", ("b", "B")),
                    ("INSERT INTO roles (name, label) VALUES (?, ?)", ("a", "dup")),  # 主キーの重複
                ]
            )
        return await pool.batch([("SELECT name FROM roles ORDER BY name", None)])

    assert asyncio.run(run()) == [[{"name": "a"}]]


def test_a_wrong_token_is_a_fixed_error_that_never_contains_the_token(d1_worker, monkeypatch):
    monkeypatch.setenv("D1_API_TOKEN", "wrong-SECRET-token")
    with pytest.raises(D1Error) as caught:
        asyncio.run(pool_for(d1_worker).batch([("SELECT 1", None)]))
    assert "401" in str(caught.value) and "SECRET" not in str(caught.value)


def test_a_missing_token_is_an_error(d1_worker, monkeypatch):
    monkeypatch.setenv("D1_API_TOKEN", "")
    with pytest.raises(D1Error, match="D1_API_TOKEN"):
        asyncio.run(pool_for(d1_worker).batch([("SELECT 1", None)]))


def test_history_store_opens_the_worker_and_closes_it(d1_worker, monkeypatch):
    monkeypatch.setattr(history.d1, "D1Pool", lambda url: pool_for(d1_worker))

    async def run():
        async with history.history_store() as saver:
            assert isinstance(saver, D1Saver) and history.get_checkpointer() is saver
            assert history.history_backend() == "d1" and history.get_pool() is not None
        assert history.get_checkpointer() is None and history.get_pool() is None

    asyncio.run(run())


def test_history_store_without_a_url_is_memory(monkeypatch):
    monkeypatch.setenv("D1_API_URL", "")

    async def run():
        async with history.history_store() as saver:
            assert saver is None and history.history_backend() == "memory"

    asyncio.run(run())


# ---- 会話履歴（checkpointer） ----


def _graph(saver):
    def reply(state: MessagesState):
        return {"messages": [AIMessage(content=f"answer {len(state['messages'])}")]}

    builder = StateGraph(MessagesState)
    builder.add_node("reply", reply)
    builder.add_edge(START, "reply")
    builder.add_edge("reply", END)
    return builder.compile(checkpointer=saver)


def test_the_saver_keeps_a_conversation_across_calls_and_deletes_it(d1_worker):
    saver = D1Saver(pool_for(d1_worker))
    graph = _graph(saver)
    config = {"configurable": {"thread_id": "u1:t1"}}

    async def run():
        await graph.ainvoke({"messages": [HumanMessage("hello")]}, config)
        await graph.ainvoke({"messages": [HumanMessage("again")]}, config)
        latest = await saver.aget_tuple(config)
        history_ids = [t.config["configurable"]["checkpoint_id"] async for t in saver.alist(config)]
        await saver.adelete_thread("u1:t1")
        return latest, history_ids, await saver.aget_tuple(config)

    latest, ids, after = asyncio.run(run())
    texts = [m.content for m in latest.checkpoint["channel_values"]["messages"]]
    assert texts == ["hello", "answer 1", "again", "answer 3"]
    assert len(ids) >= 4 and ids == sorted(ids, reverse=True)  # 新しい順
    assert latest.parent_config is not None
    assert after is None
    assert d1_worker.rows("SELECT count(*) AS n FROM checkpoints")[0]["n"] == 0
    assert d1_worker.rows("SELECT count(*) AS n FROM checkpoint_writes")[0]["n"] == 0


def test_threads_are_isolated_by_thread_id(d1_worker):
    saver = D1Saver(pool_for(d1_worker))
    graph = _graph(saver)

    async def run():
        await graph.ainvoke({"messages": [HumanMessage("one")]}, {"configurable": {"thread_id": "a"}})
        await graph.ainvoke({"messages": [HumanMessage("two")]}, {"configurable": {"thread_id": "b"}})
        return (
            await saver.aget_tuple({"configurable": {"thread_id": "a"}}),
            await saver.aget_tuple({"configurable": {"thread_id": "b"}}),
        )

    a, b = asyncio.run(run())
    assert [m.content for m in a.checkpoint["channel_values"]["messages"]][0] == "one"
    assert [m.content for m in b.checkpoint["channel_values"]["messages"]][0] == "two"


# ---- 取り込み（repository） ----


def _chunk(index: int, text: str, heading: str | None = None, page: int = 1) -> Chunk:
    return Chunk(index=index, page=page, heading=heading, text=text, content_hash=content_hash(text))


def test_the_repository_saves_reuses_chunks_by_hash_and_drops_the_rest(d1_worker):
    db = sync_for(d1_worker)
    repo = D1Repository(db)
    repo.save(
        "a.pdf", "h1", 2, [_chunk(0, "alpha"), _chunk(1, "beta"), _chunk(2, "gamma")], title="題名", collection="guides"
    )
    first = {r["content"]: r["id"] for r in d1_worker.rows("SELECT id, content FROM chunks")}
    assert repo.stored_hashes("a.pdf") == [
        c.content_hash for c in [_chunk(0, "alpha"), _chunk(1, "beta"), _chunk(2, "gamma")]
    ]
    assert repo.stored_hashes("missing.pdf") is None

    # 段落を 1 つ挿入し、gamma は消える。alpha と beta は同じ行を使い回し、番号だけ変わる
    # （Vectorize のベクトルも、消えた行の分だけ消える）
    d1_worker.vectors[str(first["gamma"])] = {"values": [1.0], "metadata": {}}
    repo.save("a.pdf", "h2", 2, [_chunk(0, "alpha"), _chunk(1, "new"), _chunk(2, "beta")])
    rows = {r["content"]: r for r in d1_worker.rows("SELECT id, content, chunk_index FROM chunks")}
    assert set(rows) == {"alpha", "new", "beta"}
    assert rows["alpha"]["id"] == first["alpha"] and rows["beta"]["id"] == first["beta"]
    assert [rows[t]["chunk_index"] for t in ("alpha", "new", "beta")] == [0, 1, 2]
    assert str(first["gamma"]) not in d1_worker.vectors

    doc = d1_worker.rows("SELECT title, collection, status, content_hash FROM documents")[0]
    assert doc == {
        "title": "題名",
        "collection": "guides",
        "status": "ready",
        "content_hash": "h2",
    }  # 題名・グループは保存済みのまま


def test_the_repository_saves_page_labels_and_fills_them_in_for_chunks_it_reuses(d1_worker):
    repo = D1Repository(sync_for(d1_worker))
    plain = _chunk(0, "alpha")
    repo.save("a.pdf", "h1", 3, [plain, _chunk(1, "beta", page=2)])
    assert [r["page_label"] for r in d1_worker.rows("SELECT page_label FROM chunks ORDER BY chunk_index")] == [
        None,
        None,
    ]
    first = {r["content"]: r["id"] for r in d1_worker.rows("SELECT id, content FROM chunks")}

    # 同じ本文のまま PDF にページラベルが付いた: 同じ行を使い回し（再埋め込みなし）、ラベルだけが入る。ページ（通し番号）は変わらない
    labelled = [
        Chunk(0, 1, None, "alpha", content_hash("alpha"), page_label="表紙"),
        Chunk(1, 2, None, "beta", content_hash("beta"), page_end=3, page_label="i", page_end_label="1", kind="table"),
    ]
    repo.save("a.pdf", "h2", 3, labelled)
    rows = {
        r["content"]: r
        for r in d1_worker.rows("SELECT id, content, page, page_end, page_label, page_end_label FROM chunks")
    }
    assert {t: rows[t]["id"] for t in rows} == first
    assert (rows["alpha"]["page"], rows["alpha"]["page_label"], rows["alpha"]["page_end_label"]) == (1, "表紙", None)
    assert (rows["beta"]["page"], rows["beta"]["page_end"]) == (2, 3)
    assert (rows["beta"]["page_label"], rows["beta"]["page_end_label"]) == ("i", "1")


def test_the_repository_can_overwrite_the_title_and_records_failures_without_dropping_chunks(d1_worker):
    repo = D1Repository(sync_for(d1_worker))
    repo.save("a.pdf", "h1", 1, [_chunk(0, "alpha")], title="古い題名")
    repo.save("a.pdf", "h2", 1, [_chunk(0, "alpha")], title=None)
    assert d1_worker.rows("SELECT title FROM documents")[0]["title"] is None
    repo.mark_failed("a.pdf", "h3", "garbled")
    doc = d1_worker.rows("SELECT status, failure_reason, content_hash FROM documents")[0]
    assert doc == {"status": "failed", "failure_reason": "garbled", "content_hash": "h3"}
    assert d1_worker.rows("SELECT count(*) AS n FROM chunks")[0]["n"] == 1  # 以前のチャンクは消さない


# ---- 埋め込みの保存 ----


def _store_with_chunks(worker: FakeWorker, collection: str | None = "guides") -> D1EmbeddingStore:
    repo = D1Repository(sync_for(worker))
    repo.save("a.pdf", "h1", 1, [_chunk(0, "alpha", "見出し"), _chunk(1, "beta")], collection=collection)
    return D1EmbeddingStore(sync_for(worker))


def _vec(x: float) -> list[float]:
    return [x] + [0.0] * (embedding.DIMENSIONS - 1)


def test_saving_embeddings_marks_chunks_and_writes_vectors_with_metadata(d1_worker):
    store = _store_with_chunks(d1_worker)
    pending = store.pending_embeddings(10)
    assert len(pending) == 2 and store.count_pending() == 2
    assert store.save_embeddings([(p, _vec(1.0)) for p in pending]) == 2
    assert store.count_pending() == 0
    assert set(d1_worker.vectors) == {str(p.id) for p in pending}
    meta = next(iter(d1_worker.vectors.values()))["metadata"]
    assert meta["collection"] == "guides" and "target" not in meta and isinstance(meta["document_id"], int)


def test_a_chunk_replaced_while_embedding_gets_no_vector(d1_worker):
    store = _store_with_chunks(d1_worker)
    pending = store.pending_embeddings(10)
    stale = PendingChunk(pending[0].id, "different-hash", pending[0].heading, pending[0].content)
    saved = store.save_embeddings([(stale, _vec(1.0)), (pending[1], _vec(1.0))])
    assert saved == 1 and set(d1_worker.vectors) == {str(pending[1].id)}


def test_a_document_without_a_collection_has_no_collection_metadata(d1_worker):
    store = _store_with_chunks(d1_worker, collection=None)
    store.save_embeddings([(p, _vec(1.0)) for p in store.pending_embeddings(10)])
    assert all("collection" not in v["metadata"] for v in d1_worker.vectors.values())


# ---- 検索（Vectorize + D1） ----


def _search(worker: FakeWorker, monkeypatch, query_vec: list[float], **kwargs):
    from app.scope import Scope
    from app.search import vector as search_vector

    pool = pool_for(worker)
    monkeypatch.setattr(search_vector, "get_pool", lambda: pool)

    async def fake_embed(_: str) -> list[float]:
        return query_vec

    monkeypatch.setattr(search_vector, "embed_query", fake_embed)
    kwargs.setdefault("scope", Scope.ALL)
    return asyncio.run(search_vector.search("質問", kwargs.pop("top_k", 5), **kwargs))


def _seed_two_groups(worker: FakeWorker) -> None:
    repo = D1Repository(sync_for(worker))
    repo.save("zahard.pdf", "h", 1, [_chunk(0, "大気の話")], collection="zahard")
    repo.save("cafe.pdf", "h", 1, [_chunk(0, "営業時間")], collection="cafe")
    store = D1EmbeddingStore(sync_for(worker))
    for p in store.pending_embeddings(10):
        store.save_embeddings(
            [(p, _vec(1.0) if "大気" in p.content else [0.8, 0.6] + [0.0] * (embedding.DIMENSIONS - 2))]
        )


def test_search_returns_the_nearest_chunks_and_respects_the_scope(d1_worker, monkeypatch):
    from app.scope import Scope

    _seed_two_groups(d1_worker)
    hits = _search(d1_worker, monkeypatch, _vec(1.0))
    assert [h.source for h in hits] == ["zahard.pdf", "cafe.pdf"]  # 点の高い順
    only_cafe = _search(d1_worker, monkeypatch, _vec(1.0), scope=Scope.only(["cafe"]))
    assert [h.source for h in only_cafe] == ["cafe.pdf"]
    assert _search(d1_worker, monkeypatch, _vec(1.0), scope=Scope.only(["cafe"]), collection="zahard") == []
    assert _search(d1_worker, monkeypatch, _vec(1.0), scope=Scope.NONE) == []


def test_search_ignores_documents_that_are_not_ready(d1_worker, monkeypatch):
    _seed_two_groups(d1_worker)
    d1_worker.run("UPDATE documents SET status = 'failed' WHERE source = 'zahard.pdf'")
    assert [h.source for h in _search(d1_worker, monkeypatch, _vec(1.0))] == ["cafe.pdf"]


def test_search_by_document_ids_uses_the_vectorize_filter_and_the_database(d1_worker, monkeypatch):
    _seed_two_groups(d1_worker)
    cafe_id = d1_worker.rows("SELECT id FROM documents WHERE source = 'cafe.pdf'")[0]["id"]
    hits = _search(d1_worker, monkeypatch, _vec(1.0), document_ids=[cafe_id])
    assert [h.source for h in hits] == ["cafe.pdf"]


# ---- アクセス制御・一覧の SQL（SQLite で動くこと） ----


def test_visible_collections_follow_roles_with_a_default_role(d1_worker, monkeypatch):
    from app import access

    d1_worker.run("INSERT INTO roles (name, label) VALUES ('default', 'd'), ('staff', 's')")
    d1_worker.run("INSERT INTO role_collection_access VALUES ('default', 'cafe'), ('staff', 'zahard')")
    d1_worker.run("INSERT INTO \"user\" (id, name, email) VALUES ('user-aaaaaaaa', 'a', 'a@example.com')")
    d1_worker.run("INSERT INTO user_roles VALUES ('user-aaaaaaaa', 'staff')")
    pool = pool_for(d1_worker)
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    assert asyncio.run(access.visible_collections("user-aaaaaaaa")) == frozenset({"zahard"})
    assert asyncio.run(access.visible_collections("someone-else")) == frozenset({"cafe"})
    assert asyncio.run(access.visible_collections(None)) == frozenset({"cafe"})


def test_the_collection_and_classification_lists_run_on_sqlite(d1_worker, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import classifications, collections
    from app.main import app

    d1_worker.run("INSERT INTO document_collections (name, label, sort_order) VALUES ('cafe', 'カフェ', 1)")
    d1_worker.run("INSERT INTO document_targets (name, label, sort_order) VALUES ('shop', '店舗', 1)")
    d1_worker.run("INSERT INTO document_types (name, label, sort_order) VALUES ('manual', 'マニュアル', 1)")
    repo = D1Repository(sync_for(d1_worker))
    repo.save("cafe.pdf", "h", 1, [_chunk(0, "営業時間")], collection="cafe")
    repo.save("loose.pdf", "h", 1, [_chunk(0, "x")], collection="newfolder")
    d1_worker.run("UPDATE documents SET target = 'shop', doc_type = 'manual' WHERE source = 'cafe.pdf'")
    pool = pool_for(d1_worker)
    monkeypatch.setattr(collections, "get_pool", lambda: pool)
    monkeypatch.setattr(classifications, "get_pool", lambda: pool)

    client = TestClient(app)
    items = client.get("/collections").json()["collections"]
    assert [(c["name"], c["registered"], c["document_count"], c["ready_count"]) for c in items] == [
        ("cafe", True, 1, 1),
        ("newfolder", False, 1, 1),
    ]
    targets = client.get("/targets").json()["targets"]
    assert [(t["name"], t["document_count"], t["ready_count"]) for t in targets] == [("shop", 1, 1)]
    assert [t["name"] for t in client.get("/doc-types").json()["doc_types"]] == ["manual"]


def test_the_document_list_runs_on_sqlite_and_counts_embedded_chunks(d1_worker, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import documents
    from app.main import app

    repo = D1Repository(sync_for(d1_worker))
    repo.save("cafe.pdf", "h", 2, [_chunk(0, "a"), _chunk(1, "b")], title="T", collection="cafe")
    store = D1EmbeddingStore(sync_for(d1_worker))
    store.save_embeddings([(store.pending_embeddings(1)[0], _vec(1.0))])
    pool = pool_for(d1_worker)
    monkeypatch.setattr(documents, "get_pool", lambda: pool)

    body = TestClient(app).get("/documents").json()
    (doc,) = body["documents"]
    assert doc["source"] == "cafe.pdf" and doc["chunk_count"] == 2 and doc["embedded_count"] == 1
    assert doc["hero_image"] is False and doc["updated_at"] is not None

    detail = TestClient(app).get(f"/documents/{doc['id']}/chunks").json()
    assert [c["embedded"] for c in detail["chunks"]] == [True, False]


def test_the_thread_list_reads_the_checkpoint_times(d1_worker, monkeypatch):
    from app.api import threads

    saver = D1Saver(pool_for(d1_worker))
    graph = _graph(saver)

    async def run():
        for key in ("user-aaaaaaaa:t1", "user-aaaaaaaa:t2", "user-bbbbbbbb:t1"):
            await graph.ainvoke({"messages": [HumanMessage("q")]}, {"configurable": {"thread_id": key}})

    asyncio.run(run())
    pool = pool_for(d1_worker)
    monkeypatch.setattr(threads, "get_pool", lambda: pool)
    rows = asyncio.run(threads._recent_threads_postgres("user-aaaaaaaa:"))
    assert sorted(r[0] for r in rows) == ["user-aaaaaaaa:t1", "user-aaaaaaaa:t2"]
    assert all(r[1] is not None and r[2] is not None and r[1] <= r[2] for r in rows)


# ---- キーワード検索用の写し（ページ分けして取る） ----


def test_the_corpus_is_fetched_in_pages_and_membership_runs_on_sqlite(d1_worker, monkeypatch):
    from app.scope import Scope
    from app.search import pipeline
    from app.search import vector as search_vector

    repo = D1Repository(sync_for(d1_worker))
    repo.save("a.pdf", "h", 1, [_chunk(i, f"本文{i}") for i in range(5)], collection="guides")
    store = D1EmbeddingStore(sync_for(d1_worker))
    store.save_embeddings([(p, _vec(1.0)) for p in store.pending_embeddings(3)])  # 5 件のうち 3 件だけ埋め込み済み
    pool = pool_for(d1_worker)
    monkeypatch.setattr(search_vector, "get_pool", lambda: pool)
    monkeypatch.setattr(pipeline, "_CORPUS_PAGE", 2)

    chunks = asyncio.run(pipeline._fetch_chunks(Scope.ALL, None))
    assert [c.text for c in chunks] == [
        "本文0",
        "本文1",
        "本文2",
    ]  # 埋め込みのあるチャンクだけ。2 件ずつ 2 ページで取る
    assert asyncio.run(pipeline._fetch_chunks(Scope.only(["other"]), None)) == []
    doc_id = d1_worker.rows("SELECT id FROM documents")[0]["id"]
    assert asyncio.run(pipeline._fetch_chunks(Scope.ALL, None, None, (doc_id,))) != []
    d1_worker.run("UPDATE documents SET target = NULL")
    assert asyncio.run(pipeline._membership(Scope.ALL, None, "shop")) == ()
