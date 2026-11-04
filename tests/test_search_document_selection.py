"""資料単位の選択（`document_ids`。画面でチェックを付けた資料）。実 API・実 DB は使わない。

確かめるのは、リクエストの検証（空・不正な ID・target / collection との同時指定は 422）、検索への受け渡し（会話の状態ではなく
ターンごとの config で渡り、会話に固定されない）、選んだターンは会話の対象に代わって選んだ資料だけを探すこと、
検索の SQL の引数と返った行の再確認（選ばれていない資料は返さない）、空の並びは何も探さないこと。
Postgres の本物の SQL は tests/test_classification_postgres.py の側に置く。
"""

import asyncio
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest

from app.agents import tools
from app.ingestion import embedding
from app.scope import Scope
from app.search import vector as search_vector
from app.search.types import Hit


class SelectingSearch:
    """検索の偽物。資料を ID で持ち、渡された document_ids で絞る。呼び出しを記録する。"""

    DOCS = (
        (1, "zahard", "zahard", "ザーハード1.pdf", "ザーハードの大気は薄い。"),
        (2, "cafe", "minato-coffee", "メニュー.pdf", "大気の話ではなく、コーヒーのメニューがある。"),
        (3, "guides", "aoba-aw820", "AW-820.pdf", "大気ではなく、洗濯機の取扱説明書。"),
    )

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        query: str,
        top_k: int = 3,
        collection: str | None = None,
        *,
        scope: Scope | None = None,
        target: str | None = None,
        document_ids: tuple[int, ...] | None = None,
    ) -> list[Hit]:
        self.calls.append({"collection": collection, "target": target, "document_ids": document_ids})
        hits = []
        for doc_id, group, doc_target, name, text in self.DOCS:
            if "大気" not in query:
                continue
            if document_ids is not None and doc_id not in document_ids:
                continue
            if collection is not None and group != collection:
                continue
            if target is not None and doc_target != target:
                continue
            hits.append(Hit(name, text, 1, None, 0.8, group, None, doc_target))
        return hits[:top_k]


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch, searching_llm) -> SelectingSearch:
    fake = SelectingSearch()
    monkeypatch.setattr(tools, "search_chunks_async", fake)
    return fake


def chat(client, **body: Any):
    return client.post("/chat", json={"message": "大気について教えて", **body})


# ---- リクエストの検証 ----


@pytest.mark.parametrize(
    "extra",
    [
        {"document_ids": []},
        {"document_ids": [0]},
        {"document_ids": [-1]},
        {"document_ids": ["a"]},
        {"document_ids": [1.5]},
        {"document_ids": [9_223_372_036_854_775_808]},
        {"document_ids": list(range(1, 502))},
        {"document_ids": [1], "target": "aoba-aw820"},
        {"document_ids": [1], "collection": "guides"},
    ],
)
def test_an_invalid_selection_is_a_422_and_never_searches(client, search, extra):
    res = chat(client, **extra)

    assert res.status_code == 422
    assert search.calls == []


def test_the_stream_validates_the_selection_too(client, search):
    res = client.post("/chat/stream", json={"message": "大気について教えて", "document_ids": []})

    assert res.status_code == 422
    assert search.calls == []


# ---- 検索への受け渡し ----


def test_the_selected_documents_reach_the_search_sorted_and_without_duplicates(client, search):
    res = chat(client, document_ids=[3, 1, 3])

    assert res.status_code == 200
    assert search.calls == [{"collection": None, "target": None, "document_ids": (1, 3)}]
    assert "ザーハード1.pdf" in res.json()["reply"] and "AW-820.pdf" in res.json()["reply"]
    assert "メニュー.pdf" not in res.json()["reply"]


def test_the_stream_passes_the_selection_too(client, search):
    res = client.post("/chat/stream", json={"message": "大気について教えて", "document_ids": [2]})

    assert res.status_code == 200
    assert search.calls == [{"collection": None, "target": None, "document_ids": (2,)}]
    assert "メニュー.pdf" in res.text


def test_without_a_selection_the_search_is_called_as_before(client, search):
    res = chat(client)

    assert res.status_code == 200
    assert search.calls == [{"collection": None, "target": None, "document_ids": None}]


def test_the_selection_is_per_turn_and_does_not_fix_the_conversation(client, search):
    thread = str(uuid.uuid4())

    assert chat(client, thread_id=thread, document_ids=[1]).status_code == 200
    # 次のターンで選び直しても 409 にならず、選ばなければ絞らない
    assert chat(client, thread_id=thread, document_ids=[2, 3]).status_code == 200
    assert chat(client, thread_id=thread).status_code == 200

    assert [c["document_ids"] for c in search.calls] == [(1,), (2, 3), None]


def test_a_conversation_with_a_target_searches_the_selection_that_turn_and_returns_to_the_target_after(client, search):
    thread = str(uuid.uuid4())

    assert chat(client, thread_id=thread, target="aoba-aw820").status_code == 200
    res = chat(client, thread_id=thread, document_ids=[1, 2])
    assert res.status_code == 200
    assert "ザーハード1.pdf" in res.json()["reply"] and "AW-820.pdf" not in res.json()["reply"]
    assert chat(client, thread_id=thread).status_code == 200

    # 選んだターンは対象を使わない。選ばないターンは、会話に固定された対象に戻る
    assert search.calls[1] == {"collection": None, "target": None, "document_ids": (1, 2)}
    assert search.calls[2] == {"collection": None, "target": "aoba-aw820", "document_ids": None}


def test_a_selection_of_unknown_documents_is_an_empty_result_not_an_error(client, search):
    res = chat(client, document_ids=[999])

    assert res.status_code == 200
    assert search.calls == [{"collection": None, "target": None, "document_ids": (999,)}]


# ---- 検索の SQL ----


VECTOR = [0.001 * i for i in range(1, embedding.DIMENSIONS + 1)]


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self._rows = rows

    async def fetchall(self) -> list[dict[str, Any]]:
        return self._rows


class _Pool:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any]) -> _Cursor:
                pool.calls.append((sql, params))
                return _Cursor(pool.rows)

        yield Conn()


def _row(document_id: int, source: str) -> dict[str, Any]:
    return {
        "document_id": document_id,
        "source": source,
        "collection": "guides",
        "target": None,
        "heading": None,
        "content": "本文",
        "page": 1,
        "page_end": None,
        "sim": 0.8,
    }


@pytest.fixture
def vector_pool(monkeypatch: pytest.MonkeyPatch) -> _Pool:
    pool = _Pool([_row(1, "a.pdf"), _row(2, "b.pdf")])
    embedded: list[str] = []

    async def embed(query: str) -> list[float]:
        embedded.append(query)
        return VECTOR

    monkeypatch.setattr(search_vector, "get_pool", lambda: pool)
    monkeypatch.setattr(search_vector, "embed_query", embed)
    pool.embedded = embedded  # type: ignore[attr-defined]
    return pool


@pytest.mark.real_search
def test_the_selected_ids_go_to_the_sql_and_rows_outside_the_selection_are_dropped(vector_pool):
    hits = asyncio.run(search_vector.search("質問", 5, scope=Scope.ALL, document_ids=[2, 1, 2]))

    sql, params = vector_pool.calls[0]
    assert "d.id = ANY(%(doc_ids)s::bigint[])" in sql
    assert params["doc_ids"] == [1, 2]
    assert [h.source for h in hits] == ["a.pdf", "b.pdf"]

    # SQL の条件を誤って、選ばれていない資料の行が返っても、返さない
    hits = asyncio.run(search_vector.search("質問", 5, scope=Scope.ALL, document_ids=[1]))
    assert [h.source for h in hits] == ["a.pdf"]


@pytest.mark.real_search
def test_without_a_selection_the_sql_does_not_filter_by_document(vector_pool):
    hits = asyncio.run(search_vector.search("質問", 5, scope=Scope.ALL))

    assert vector_pool.calls[0][1]["doc_ids"] is None
    assert [h.source for h in hits] == ["a.pdf", "b.pdf"]


@pytest.mark.real_search
def test_an_empty_selection_searches_nothing_and_does_not_call_the_embedding_or_the_database(vector_pool):
    hits = asyncio.run(search_vector.search("質問", 5, scope=Scope.ALL, document_ids=[]))

    assert hits == []
    assert vector_pool.calls == [] and vector_pool.embedded == []  # type: ignore[attr-defined]


# ---- 検索の重ね（pipeline） ----


@pytest.mark.real_search
def test_the_pipeline_passes_the_selection_to_the_vector_and_keyword_sides_and_caches_per_selection(monkeypatch):
    from app.search import pipeline as search_pipeline
    from app.search.pipeline import Chunk

    seen: list[Any] = []
    fetched: list[Any] = []

    async def vector_search(query, top_k, collection=None, *, scope, target=None, document_ids=None):
        seen.append(document_ids)
        return []

    async def fetch(scope, collection, target=None, document_ids=None):
        fetched.append(document_ids)
        return [Chunk("a.pdf", "本文", 1, None, "guides", None, 1)]

    search_pipeline.clear_cache()
    monkeypatch.setattr(search_vector, "search", vector_search)
    monkeypatch.setattr(search_pipeline, "_fetch_chunks", fetch)
    monkeypatch.setenv("SEARCH_HYBRID", "on")
    monkeypatch.setenv("SEARCH_RERANK", "off")
    monkeypatch.setenv("SEARCH_GRAPH", "off")

    for ids in ([2, 1], [1, 2], [3]):
        asyncio.run(search_pipeline.search("質問", 3, scope=Scope.ALL, document_ids=ids))
    # 空の選択は何も探さない（ベクトル側も資料の写しも呼ばない）
    assert asyncio.run(search_pipeline.search("質問", 3, scope=Scope.ALL, document_ids=[])) == []

    assert seen == [(1, 2), (1, 2), (3,)]
    assert fetched == [(1, 2), (3,)]  # 同じ選び方（並びが違うだけ）は写しを使い回す
    search_pipeline.clear_cache()
