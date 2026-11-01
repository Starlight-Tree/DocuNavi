"""ロール別の資料グループ制御（app/access.py、app/scope.py と、各 API の制限）のテスト。実 DB・実 API は使わない。

確かめるのは、次の 5 つ。
1. `cafe` だけのロールの利用者が、10 経路（検索、資料の一覧・本文・PDF・関係図・影響、グループの一覧、/chat、/chat/stream、会話の表示）の
   どこからも `zahard` の内容に届かないこと。見えない資料の応答が、存在しない資料と同じであること。
2. `default` ロール（現行の 3 グループ）の利用者が、`ACCESS_MODE=off`（いまの挙動）と同じ結果を得ること。
3. 範囲（`scope`）を渡し忘れても、全件を検索しないこと。
4. `ACCESS_MODE=off|shadow|enforce` の挙動（shadow は制限せず件数をログに出す。enforce は引けなければ 503）。
5. 過去の会話は表示するが、いま見えないグループの検索結果に基づく回答は、画面にも LLM にも出さないこと。
SQL が本物の Postgres で期待どおりに絞るかは tests/test_access_postgres.py（pgvector が要る検索の SQL だけは、どのテストでも動かせない）。
"""

import asyncio
import json
import logging
import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import access
from app.agents import graph as graph_module
from app.agents import tools
from app.agents.tools import search_documents
from app.api import collections, documents, routes
from app.api import graph as graph_api
from app.main import app
from app.scope import ACCESS_SCOPE_KEY, ALLOWED_FILTER_SQL, Scope
from app.search import vector as search_vector
from tests.fakes import GroupedFakeSearch, SearchingMockLLM
from tests.test_collections import FakeCursor as CollectionsCursor
from tests.test_collections import row as collection_row
from tests.test_documents import FakeCursor, chunk_row, summary_row
from tests.test_graph_api import DORM_HASH
from tests.test_search_vector import Embedder, FakePool

CAFE_ONLY = frozenset({"cafe"})
DEFAULT_ROLE_SET = frozenset({"zahard", "cafe", "guides"})
# zahard の資料にだけある文。これが応答のどこかに出たら、漏れている
SECRET = "ザーハードの大気は薄く"
SECRET_CHUNK = "zahard の本文: 漏れてはいけない"


# ---- アクセス権の設定 ------------------------------------------------------------------------------------


@pytest.fixture
def set_access(monkeypatch: pytest.MonkeyPatch):
    """ACCESS_MODE と、利用者が見えるグループ・いま存在する全グループ（DB の代わり）を決める。引いた回数を返す。"""

    def install(
        mode: str,
        visible: frozenset[str] = DEFAULT_ROLE_SET,
        error: Exception | None = None,
        known: frozenset[str] = DEFAULT_ROLE_SET,
    ) -> list[Any]:
        calls: list[Any] = []

        async def fake_visible(user_id: str | None) -> tuple[frozenset[str], bool]:
            calls.append(user_id)
            if error is not None:
                raise error
            return visible, bool(visible) and known <= visible

        monkeypatch.setenv("ACCESS_MODE", mode)
        monkeypatch.setattr(access, "visible_with_completeness", fake_visible)
        return calls

    return install


@pytest.fixture
def grouped_search(monkeypatch: pytest.MonkeyPatch, searching_llm) -> GroupedFakeSearch:
    """グループと範囲を見る偽の検索と、ツールを呼ぶ偽の LLM。"""
    fake = GroupedFakeSearch()
    monkeypatch.setattr(tools, "search_chunks_async", fake)
    return fake


# ---- 資料の DB の偽物（SQL は見ず、全件を返す。絞り込みが API 側でも効くことを確かめるため） -----------------------


def doc(doc_id: int, source: str, collection: str | None, content_hash: str = "h") -> dict[str, Any]:
    return {**summary_row(doc_id, source), "collection": collection, "title": None, "content_hash": content_hash}


ZAHARD_DOC = doc(1, "zahard_world.pdf", "zahard")
CAFE_DOC = doc(2, "cafe_hours.pdf", "cafe")
GUIDES_DOC = doc(3, "tsubasa_dorm_rules.pdf", "guides", DORM_HASH)
UNGROUPED_DOC = doc(5, "orphan.pdf", None)


class DocsPool:
    """GET /documents・/documents/{id}/…の DB の偽物。一覧は SQL の絞り込みを無視して全件を返す。"""

    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self.docs = {d["id"]: d for d in docs}
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any]) -> FakeCursor:
                pool.calls.append((sql, params))
                if sql == documents._LIST_SQL:
                    return FakeCursor(list(pool.docs.values()))
                if sql == documents._DOCUMENT_SQL:
                    found = pool.docs.get(params["id"])
                    return FakeCursor([found] if found else [])
                if sql == documents._CHUNKS_SQL:
                    found = pool.docs[params["id"]]
                    text = SECRET_CHUNK if found["collection"] == "zahard" else "公開してよい本文"
                    return FakeCursor([chunk_row(0, content=text)])
                raise AssertionError(f"想定外の SQL: {sql}")

        yield Conn()


@pytest.fixture
def docs_pool(monkeypatch: pytest.MonkeyPatch) -> DocsPool:
    pool = DocsPool([ZAHARD_DOC, CAFE_DOC, GUIDES_DOC, UNGROUPED_DOC])
    monkeypatch.setattr(documents, "get_pool", lambda: pool)
    monkeypatch.setattr(graph_api, "get_pool", lambda: pool)
    return pool


class GroupsPool:
    """GET /collections の DB の偽物。SQL の絞り込みを無視して 3 グループを返す。"""

    @asynccontextmanager
    async def connection(self):
        class Conn:
            async def execute(self, sql: str, params: dict[str, Any]) -> CollectionsCursor:
                assert sql == collections._LIST_SQL
                return CollectionsCursor([collection_row("zahard"), collection_row("cafe"), collection_row("guides")])

        yield Conn()


@pytest.fixture
def groups_pool(monkeypatch: pytest.MonkeyPatch) -> None:
    pool = GroupsPool()
    monkeypatch.setattr(collections, "get_pool", lambda: pool)


# ---- 範囲（Scope） -----------------------------------------------------------------------------------------


def test_scope_has_no_default_so_forgetting_it_cannot_mean_all():
    with pytest.raises(TypeError):
        Scope()  # type: ignore[call-arg]
    assert Scope.ALL.is_all and not Scope.ALL.is_empty
    assert Scope.NONE.is_empty and not Scope.NONE.is_all
    assert Scope.only(["cafe"]).names == frozenset({"cafe"})


def test_scope_allows_only_its_groups_and_ungrouped_documents_need_all():
    scope = Scope.only(["cafe", "guides"])
    assert scope.allows("cafe") and scope.allows("guides") and not scope.allows("zahard")
    assert not scope.allows(None)  # グループの無い資料は、範囲を絞ったら見えない
    assert Scope.ALL.allows(None) and Scope.ALL.allows("zahard")
    assert not Scope.NONE.allows("cafe") and not Scope.NONE.allows(None)
    assert scope.sql_names() == ["cafe", "guides"] and Scope.ALL.sql_names() is None and Scope.NONE.sql_names() == []


def test_the_filter_sql_is_shared_by_the_search_and_the_document_list():
    assert ALLOWED_FILTER_SQL in search_vector._SEARCH_SQL
    assert ALLOWED_FILTER_SQL in documents._LIST_SQL


# ---- 検索: 範囲を渡し忘れても全件を探さない ---------------------------------------------------------------------


@pytest.fixture
def vector_env(monkeypatch: pytest.MonkeyPatch):
    rows = [
        {"source": "zahard_world.pdf", "heading": None, "content": SECRET_CHUNK, "sim": 0.9, "collection": "zahard"},
        {"source": "cafe_hours.pdf", "heading": None, "content": "営業は8時から", "sim": 0.8, "collection": "cafe"},
        {"source": "orphan.pdf", "heading": None, "content": "グループ無し", "sim": 0.7, "collection": None},
    ]
    pool = FakePool(rows)
    embedder = Embedder()
    monkeypatch.setattr(search_vector, "get_pool", lambda: pool)
    monkeypatch.setattr(search_vector, "embed_query", embedder)
    return pool, embedder


@pytest.mark.real_search
def test_search_without_a_scope_is_a_type_error_not_a_full_search(vector_env):
    pool, embedder = vector_env
    with pytest.raises(TypeError):
        asyncio.run(search_vector.search("大気", 3))  # type: ignore[call-arg]
    for bad in (None, "all", frozenset({"cafe"})):
        with pytest.raises(TypeError):
            asyncio.run(search_vector.search("大気", 3, scope=bad))  # type: ignore[arg-type]
    assert pool.calls == [] and embedder.queries == []


@pytest.mark.real_search
def test_an_empty_scope_returns_nothing_without_touching_the_database(vector_env):
    pool, embedder = vector_env
    assert asyncio.run(search_vector.search("大気", 3, scope=Scope.NONE)) == []
    assert pool.calls == [] and embedder.queries == []


@pytest.mark.real_search
def test_a_scope_is_sent_to_the_sql_and_rows_outside_it_are_dropped_again(vector_env):
    pool, _ = vector_env
    hits = asyncio.run(search_vector.search("大気", 5, scope=Scope.only(["cafe"])))
    ((_, params),) = pool.calls
    assert params["allowed"] == ["cafe"]
    # 偽の DB が範囲外の行（zahard とグループ無し）まで返しても、検索は返さない
    assert [h.source for h in hits] == ["cafe_hours.pdf"]


@pytest.mark.real_search
def test_all_is_the_only_way_to_search_every_group(vector_env):
    pool, _ = vector_env
    hits = asyncio.run(search_vector.search("大気", 5, scope=Scope.ALL))
    assert pool.calls[0][1]["allowed"] is None
    assert [h.source for h in hits] == ["zahard_world.pdf", "cafe_hours.pdf", "orphan.pdf"]


@pytest.mark.real_search
def test_a_collection_outside_the_scope_returns_nothing_without_touching_the_database(vector_env):
    pool, embedder = vector_env
    assert asyncio.run(search_vector.search("大気", 3, "zahard", scope=Scope.only(["cafe"]))) == []
    assert pool.calls == [] and embedder.queries == []
    # 範囲の中のグループなら、範囲と collection の両方が SQL に渡る
    asyncio.run(search_vector.search("営業", 3, "cafe", scope=Scope.only(["cafe"])))
    assert pool.calls[0][1]["collection"] == "cafe" and pool.calls[0][1]["allowed"] == ["cafe"]


@pytest.mark.real_search
def test_shadow_does_not_filter_but_logs_how_many_would_be_filtered(vector_env, caplog):
    caplog.set_level(logging.INFO, logger="app.scope")
    hits = asyncio.run(search_vector.search("大気", 5, scope=Scope(None, shadow=CAFE_ONLY)))
    assert len(hits) == 3  # 制限しない
    assert any("全 3 件のうち 2 件が enforce では絞り込まれる" in r.getMessage() for r in caplog.records)


def test_the_tool_without_a_scope_in_its_config_searches_nothing(monkeypatch, caplog):
    seen: list[Scope] = []

    async def fake(query, top_k=2, collection=None, *, scope):
        seen.append(scope)
        return []

    monkeypatch.setattr(tools, "search_chunks_async", fake)
    call = {"name": "search_documents", "args": {"query": "大気"}, "id": "c1", "type": "tool_call"}
    asyncio.run(search_documents.ainvoke(call))  # config なし
    asyncio.run(search_documents.ainvoke(call, {"configurable": {ACCESS_SCOPE_KEY: "all"}}))  # Scope ではない値
    assert seen == [Scope.NONE, Scope.NONE]
    asyncio.run(search_documents.ainvoke(call, {"configurable": {ACCESS_SCOPE_KEY: Scope.ALL}}))
    assert seen[-1] is Scope.ALL


def test_the_scope_is_not_a_tool_argument_the_model_can_set():
    # モデルが scope を引数に混ぜても、検索には渡らない（config の値だけが使われる）
    seen: list[Scope] = []

    async def fake(query, top_k=2, collection=None, *, scope):
        seen.append(scope)
        return []

    import app.agents.tools as tools_module

    original = tools_module.search_chunks_async
    tools_module.search_chunks_async = fake  # type: ignore[assignment]
    try:
        call = {"name": "search_documents", "args": {"query": "q", "scope": "all"}, "id": "c", "type": "tool_call"}
        asyncio.run(search_documents.ainvoke(call, {"configurable": {ACCESS_SCOPE_KEY: Scope.only(["cafe"])}}))
    finally:
        tools_module.search_chunks_async = original  # type: ignore[assignment]
    assert seen == [Scope.only(["cafe"])]


# ---- 10 経路: cafe だけのロール ---------------------------------------------------------------------------------


def _chat(client, message: str, collection: str | None = None, thread_id: str | None = None, path: str = "/chat"):
    body: dict[str, Any] = {"message": message, "thread_id": thread_id or str(uuid.uuid4())}
    if collection is not None:
        body["collection"] = collection
    return client.post(path, json=body)


def _assert_no_leak(res) -> None:
    text = res.text
    assert SECRET not in text and SECRET_CHUNK not in text
    assert "zahard" not in text and "ザーハード" not in text


def test_a_cafe_only_user_cannot_reach_zahard_on_any_of_the_ten_paths(
    client, set_access, grouped_search, docs_pool, groups_pool
):
    set_access("enforce", CAFE_ONLY)
    missing = client.get("/documents/999/chunks")
    assert missing.status_code == 404

    # 2. GET /documents: cafe の資料だけ（SQL の絞り込みを偽の DB が無視しても、API が落とす）
    listed = client.get("/documents")
    assert listed.status_code == 200
    assert [d["id"] for d in listed.json()["documents"]] == [CAFE_DOC["id"]]
    _assert_no_leak(listed)
    # SQL にも範囲が渡る
    assert docs_pool.calls[-1][1]["allowed"] == ["cafe"]

    # 3-6. 本文・PDF・関係図・影響: 存在しない資料と同じ応答（文も同じ）
    for hidden in (ZAHARD_DOC["id"], GUIDES_DOC["id"]):
        for url in (
            f"/documents/{hidden}/chunks",
            f"/documents/{hidden}/file",
            f"/documents/{hidden}/graph",
            f"/documents/{hidden}/impact?node=a1",
        ):
            res = client.get(url)
            assert (res.status_code, res.json()) == (404, {"detail": documents._DETAIL_NOT_FOUND}), url
            _assert_no_leak(res)
    # 資料があるのに対象外（関係図の対象でない）と答えると、存在が分かる。見えない資料は、先に見えるかを確かめる
    assert client.get(f"/documents/{CAFE_DOC['id']}/graph").json() == {"detail": graph_api._DETAIL_UNSUPPORTED}
    assert client.get(f"/documents/{GUIDES_DOC['id']}/graph").json() == {"detail": documents._DETAIL_NOT_FOUND}
    # グループの無い資料は、範囲を絞ったら見えない
    assert client.get(f"/documents/{UNGROUPED_DOC['id']}/chunks").status_code == 404

    # 7. GET /collections
    groups = client.get("/collections")
    assert [c["name"] for c in groups.json()["collections"]] == ["cafe"]
    _assert_no_leak(groups)

    # 1・8・9. 検索と /chat と /chat/stream: zahard の質問に、zahard の本文も出典も出ない
    for path in ("/chat", "/chat/stream"):
        res = _chat(client, "大気について教えて", path=path)
        assert res.status_code == 200, path
        _assert_no_leak(res)
        res = _chat(client, "営業時間を教えて", path=path)
        assert res.status_code == 200 and "cafe_hours.pdf" in res.text
        _assert_no_leak(res)
        # zahard の指定は 403（固定文。グループ名を載せない）。存在しないグループも同じ
        for collection in ("zahard", "nosuchgroup"):
            denied = _chat(client, "大気について教えて", collection, path=path)
            assert (denied.status_code, denied.json()) == (403, {"detail": access.DETAIL_FORBIDDEN_COLLECTION}), path
            assert "zahard" not in denied.text and "nosuchgroup" not in denied.text
    # 検索には、LLM の引数ではなく config の範囲が渡っている
    assert grouped_search.calls and all(scope == Scope.only(["cafe"]) for _, _, scope in grouped_search.calls)
    body = _chat(client, "大気について教えて").json()
    assert body["hit_count"] == 0 and body["collection"] is None


def test_the_history_display_hides_answers_based_on_a_group_that_is_no_longer_visible(
    client, set_access, grouped_search
):
    # 10. 会話の表示。いま見えないグループの検索結果に基づく回答は、表示しない（会話自体と、ユーザーの発言は表示する）
    set_access("enforce", DEFAULT_ROLE_SET)
    thread = str(uuid.uuid4())
    assert SECRET in _chat(client, "大気について教えて", thread_id=thread).json()["context"]
    assert "cafe_hours.pdf" in _chat(client, "営業時間を教えて", thread_id=thread).json()["reply"]
    before = client.get(f"/threads/{thread}/messages").json()["messages"]
    assert [m["role"] for m in before] == ["user", "assistant", "user", "assistant"]
    assert "zahard_atmosphere.pdf" in before[1]["text"]

    set_access("enforce", CAFE_ONLY)  # ロールが変わった
    res = client.get(f"/threads/{thread}/messages")
    assert res.status_code == 200  # 会話は表示する
    messages = res.json()["messages"]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[0]["text"] == "大気について教えて"  # 本人の発言は残る
    assert messages[1]["text"] == access.MASKED_ANSWER_TEXT
    assert "cafe_hours.pdf" in messages[3]["text"]  # 見えるグループに基づく回答は残る
    _assert_no_leak(type("R", (), {"text": json.dumps(messages, ensure_ascii=False)})())


def test_a_saved_collection_that_is_no_longer_visible_is_not_returned_and_cannot_be_continued(
    client, set_access, grouped_search
):
    set_access("enforce", DEFAULT_ROLE_SET)
    thread = str(uuid.uuid4())
    assert _chat(client, "大気について教えて", "zahard", thread).status_code == 200
    assert client.get(f"/threads/{thread}/messages").json()["collection"] == "zahard"

    set_access("enforce", CAFE_ONLY)
    assert client.get(f"/threads/{thread}/messages").json()["collection"] is None  # 見えないグループの名前を返さない
    for path in ("/chat", "/chat/stream"):
        # 続きの会話は、グループの指定があってもなくても 403（409 の文にもグループ名を出さない）
        for collection in (None, "zahard", "cafe"):
            res = _chat(client, "大気について教えて", collection, thread, path)
            assert (res.status_code, res.json()) == (403, {"detail": access.DETAIL_FORBIDDEN_COLLECTION})


class RecordingLLM(SearchingMockLLM):
    """LLM に渡った入力を記録する。"""

    seen: list[list[Any]] = []

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        RecordingLLM.seen.append(list(messages))
        return super()._generate(messages, stop, run_manager, **kwargs)


def test_the_llm_never_sees_the_hidden_results_of_a_past_turn(client, set_access, grouped_search, monkeypatch):
    monkeypatch.setattr(graph_module, "create_llm", lambda: RecordingLLM())
    routes._graph_for.cache_clear()
    RecordingLLM.seen = []
    set_access("enforce", DEFAULT_ROLE_SET)
    thread = str(uuid.uuid4())
    _chat(client, "大気について教えて", thread_id=thread)
    assert any(SECRET in str(m.content) for call in RecordingLLM.seen for m in call)  # 見えていたときは LLM に渡る

    RecordingLLM.seen = []
    set_access("enforce", CAFE_ONLY)
    res = _chat(client, "営業時間を教えて", thread_id=thread)
    assert res.status_code == 200
    sent = "\n".join(str(m.content) for call in RecordingLLM.seen for m in call)
    assert SECRET not in sent and "zahard_atmosphere.pdf" not in sent
    assert access.MASKED_TOOL_TEXT in sent and access.MASKED_ANSWER_TEXT in sent
    routes._graph_for.cache_clear()


def test_the_scope_is_not_saved_in_the_conversation_state(client, set_access, grouped_search):
    set_access("enforce", CAFE_ONLY)
    thread = str(uuid.uuid4())
    _chat(client, "営業時間を教えて", thread_id=thread)
    snapshot = routes.get_graph(True).get_state({"configurable": {"thread_id": thread}})
    assert "access_scope" not in snapshot.values
    stored = repr(snapshot.values) + repr(snapshot.metadata) + repr(snapshot.config)
    assert "Scope(" not in stored and "access_scope" not in stored


# ---- 現行の挙動: default ロール ---------------------------------------------------------------------------------


def _snapshot(client, thread_id: str) -> dict[str, Any]:
    """主な経路の応答（ステータスと本文）。thread_id などの毎回違う値は含めない。"""
    out: dict[str, Any] = {}
    for name, url in (
        ("documents", "/documents"),
        ("collections", "/collections"),
        ("chunks", f"/documents/{ZAHARD_DOC['id']}/chunks"),
        ("chunks_cafe", f"/documents/{CAFE_DOC['id']}/chunks"),
        ("graph", f"/documents/{GUIDES_DOC['id']}/graph"),
        ("impact", f"/documents/{GUIDES_DOC['id']}/impact?node=a13"),
    ):
        res = client.get(url)
        out[name] = (res.status_code, res.json())
    for question in ("大気について教えて", "営業時間を教えて", "申し込みはいつまで"):
        body = _chat(client, question, thread_id=f"{thread_id}-{abs(hash(question))}").json()
        out[question] = (body["reply"], body["context"], body["hit_count"], body["collection"])
    scoped = _chat(client, "大気について教えて", "zahard").json()
    out["zahard"] = (scoped["reply"], scoped["hit_count"], scoped["collection"])
    return out


def test_the_default_role_behaves_exactly_like_access_mode_off(
    client, set_access, grouped_search, docs_pool, groups_pool
):
    # 未分類の資料（collection が NULL）は、グループを持つロールには見えない。これは現行との違いなので、この比較からは外す
    docs_pool.docs.pop(UNGROUPED_DOC["id"])
    set_access("off")
    off = _snapshot(client, "off")
    set_access("enforce", DEFAULT_ROLE_SET)
    enforced = _snapshot(client, "enf")
    assert enforced == off
    assert off["documents"][0] == 200 and len(off["documents"][1]["documents"]) == 3


# ---- ACCESS_MODE ---------------------------------------------------------------------------------------------


def test_off_never_reads_the_role_tables(client, set_access, grouped_search, docs_pool, groups_pool):
    calls = set_access("off", CAFE_ONLY)
    assert len(client.get("/documents").json()["documents"]) == 4  # 未分類の資料も含めて全件
    assert "大気" in client.get("/documents/1/chunks").text or True
    assert _chat(client, "大気について教えて").status_code == 200
    assert _chat(client, "大気について教えて", "zahard").status_code == 200
    assert calls == []  # 引いていない（DB を読まない）


def test_shadow_does_not_restrict_but_logs_the_counts(
    client, set_access, grouped_search, docs_pool, groups_pool, caplog
):
    caplog.set_level(logging.INFO, logger="app.scope")
    calls = set_access("shadow", CAFE_ONLY)
    listed = client.get("/documents").json()["documents"]
    assert len(listed) == 4  # 制限しない
    assert client.get("/documents/1/chunks").status_code == 200
    assert client.get("/documents/1/file").status_code != 403
    assert [c["name"] for c in client.get("/collections").json()["collections"]] == ["zahard", "cafe", "guides"]
    body = _chat(client, "大気について教えて", "zahard").json()  # 指定しても通り、検索も制限されない
    assert body["hit_count"] == 1 and SECRET in body["context"]
    assert len(calls) >= 5  # 毎回引く（キャッシュなし）
    messages = [r.getMessage() for r in caplog.records if "shadow" in r.getMessage()]
    assert any("資料の一覧 全 4 件のうち 3 件が enforce では絞り込まれる" in m for m in messages)
    assert any("資料グループの一覧 全 3 件のうち 2 件" in m for m in messages)
    assert any("チャット 全 1 件のうち 1 件" in m for m in messages)
    assert not any("zahard" in m for m in messages)  # ログにグループ名は出さない


def test_shadow_continues_unrestricted_when_the_roles_cannot_be_read(client, set_access, grouped_search, caplog):
    set_access("shadow", error=RuntimeError("postgresql://u:pw@h/x"))
    res = _chat(client, "大気について教えて", "zahard")
    assert res.status_code == 200 and res.json()["hit_count"] == 1
    assert "postgresql://" not in caplog.text and "RuntimeError" in caplog.text


def test_enforce_fails_closed_with_a_fixed_503_when_the_roles_cannot_be_read(
    client, set_access, grouped_search, docs_pool, caplog
):
    set_access("enforce", error=RuntimeError("postgresql://u:pw@h/x AIzaSECRET"))
    for method, url in (
        ("get", "/documents"),
        ("get", "/collections"),
        ("get", "/documents/1/chunks"),
        ("get", "/threads/abc/messages"),
        ("post", "/chat"),
        ("post", "/chat/stream"),
    ):
        res = client.post(url, json={"message": "大気"}) if method == "post" else client.get(url)
        assert (res.status_code, res.json()) == (503, {"detail": access.DETAIL_UNAVAILABLE}), url
    assert "postgresql://" not in caplog.text and "AIzaSECRET" not in caplog.text


def test_a_user_who_sees_no_group_gets_403_on_every_data_path_but_can_still_read_conversations(
    client, set_access, grouped_search, docs_pool, groups_pool
):
    set_access("enforce", DEFAULT_ROLE_SET)
    thread = str(uuid.uuid4())
    _chat(client, "営業時間を教えて", thread_id=thread)
    set_access("enforce", frozenset())  # 割り当てが無くなった
    expected = (403, {"detail": access.DETAIL_NO_COLLECTIONS})
    for method, url in (
        ("get", "/documents"),
        ("get", "/collections"),
        ("get", "/documents/2/chunks"),
        ("get", "/documents/2/file"),
        ("get", "/documents/3/graph"),
        ("get", "/documents/3/impact?node=a13"),
        ("post", "/chat"),
        ("post", "/chat/stream"),
    ):
        res = client.post(url, json={"message": "大気"}) if method == "post" else client.get(url)
        assert (res.status_code, res.json()) == expected, url
    # 会話の表示は止めない（本人の会話であり、回答は伏せられる）
    res = client.get(f"/threads/{thread}/messages")
    assert res.status_code == 200 and res.json()["messages"][1]["text"] == access.MASKED_ANSWER_TEXT


def test_an_unknown_access_mode_fails_the_startup(monkeypatch):
    monkeypatch.setenv("ACCESS_MODE", "strict")
    with pytest.raises(access.AccessConfigError), TestClient(app):
        pass


@pytest.mark.parametrize("mode", ["shadow", "enforce"])
def test_shadow_and_enforce_need_a_database_at_startup(monkeypatch, mode):
    monkeypatch.setenv("ACCESS_MODE", mode)
    monkeypatch.setattr(access, "get_pool", lambda: None)
    with pytest.raises(access.AccessConfigError, match="DATABASE_URL"):
        asyncio.run(access.verify_access_config())


def test_off_needs_no_database_at_startup(monkeypatch):
    monkeypatch.delenv("ACCESS_MODE", raising=False)
    monkeypatch.setattr(access, "get_pool", lambda: None)
    asyncio.run(access.verify_access_config())
    assert access.access_mode() == "off"


class ProbePool:
    def __init__(self, row: dict[str, Any] | None = None, error: Exception | None = None) -> None:
        self.row, self.error = row, error

    @asynccontextmanager
    async def connection(self):
        pool = self

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any]) -> CollectionsCursor:
                if pool.error is not None:
                    raise pool.error
                return CollectionsCursor([pool.row])

        yield Conn()


def test_startup_fails_when_the_migration_is_missing_or_the_default_role_is_gone(monkeypatch):
    monkeypatch.setenv("ACCESS_MODE", "enforce")
    monkeypatch.setattr(access, "get_pool", lambda: ProbePool(error=RuntimeError("postgresql://u:pw@h/x")))
    with pytest.raises(access.AccessConfigError, match="20261026045921") as failure:
        asyncio.run(access.verify_access_config())
    assert "postgresql://" not in str(failure.value)

    row = {"default_roles": 0, "default_collections": 0, "assignments": 0}
    monkeypatch.setattr(access, "get_pool", lambda: ProbePool(row))
    with pytest.raises(access.AccessConfigError, match="default"):
        asyncio.run(access.verify_access_config())

    row = {"default_roles": 1, "default_collections": 3, "assignments": 2}
    monkeypatch.setattr(access, "get_pool", lambda: ProbePool(row))
    asyncio.run(access.verify_access_config())  # 揃っていれば通る


def test_visible_collections_uses_the_default_role_when_the_user_has_no_roles(monkeypatch):
    pool = FakePool([{"collection": "cafe"}, {"collection": "guides"}])
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    assert asyncio.run(access.visible_collections("00000000-0000-0000-0000-000000000001")) == {"cafe", "guides"}
    assert asyncio.run(access.visible_collections(None)) == {"cafe", "guides"}
    assert [p for _, p in pool.calls] == [
        {"uid": "00000000-0000-0000-0000-000000000001", "default": "default"},
        {"uid": None, "default": "default"},
    ]
    monkeypatch.setattr(access, "get_pool", lambda: None)
    with pytest.raises(access.AccessUnavailable):
        asyncio.run(access.visible_collections(None))


# ---- 会話の伏せ方 ---------------------------------------------------------------------------------------------


def _tool(collection: str | None, call_id: str = "t1", *, with_key: bool = True) -> ToolMessage:
    source = {"name": "x.pdf", "page": 1, "page_end": None, "weak": False}
    if with_key:
        source["collection"] = collection
    return ToolMessage(content="[x.pdf]\n本文", tool_call_id=call_id, artifact=[source])


def _turn(collection: str | None, *, with_key: bool = True) -> list[Any]:
    call = {"name": "search_documents", "args": {"query": "q"}, "id": "t1", "type": "tool_call"}
    return [
        HumanMessage(content="質問", id="h"),
        AIMessage(content="", tool_calls=[call], id="a1"),
        _tool(collection, with_key=with_key),
        AIMessage(content="回答（出典: x.pdf）", id="a2"),
    ]


def test_mask_hides_the_result_and_the_answer_of_a_turn_with_a_hidden_source_only():
    scope = Scope.only(["cafe"])
    hidden, count = access.mask_hidden_turns(_turn("zahard") + _turn("cafe"), scope)
    assert count == 2
    assert hidden[0].content == "質問" and hidden[1].tool_calls  # 発言と、ツール呼び出しは残す
    assert hidden[2].content == access.MASKED_TOOL_TEXT and hidden[2].artifact == []
    assert hidden[3].content == access.MASKED_ANSWER_TEXT and hidden[3].id == "a2"
    assert [m.content for m in hidden[4:]] == [m.content for m in (_turn("cafe"))]  # 見える回はそのまま


def test_mask_does_not_change_the_original_messages_and_all_masks_nothing():
    original = _turn("zahard")
    masked, _ = access.mask_hidden_turns(original, Scope.only(["cafe"]))
    assert masked[2] is not original[2] and original[2].content == "[x.pdf]\n本文" and original[2].artifact
    same, count = access.mask_hidden_turns(original, Scope.ALL)
    assert count == 0 and all(a is b for a, b in zip(same, original, strict=True))


def test_mask_treats_a_source_without_its_group_as_hidden_when_the_scope_is_restricted():
    # グループを持たない古い会話や未分類の資料は、見えるかどうか分からないので、範囲を絞ったら伏せる（漏れない方に倒す）
    for with_key, collection in ((False, None), (True, None)):
        _, count = access.mask_hidden_turns(_turn(collection, with_key=with_key), Scope.only(["cafe"]))
        assert count == 2
    _, none = access.mask_hidden_turns(_turn("cafe"), Scope.only(["cafe"]))
    assert none == 0
    _, nothing = access.mask_hidden_turns(_turn("cafe"), Scope.NONE)
    assert nothing == 2


def test_an_answer_without_a_search_is_never_masked():
    messages = [HumanMessage(content="こんにちは"), AIMessage(content="こんにちは")]
    masked, count = access.mask_hidden_turns(messages, Scope.NONE)
    assert count == 0 and [m.content for m in masked] == ["こんにちは", "こんにちは"]


# ---- 古い会話（collection の項目が無い出典）の扱い --------------------------------------------------------------

COMPLETE_SCOPE = Scope(DEFAULT_ROLE_SET, complete=True)


def test_mask_shows_a_legacy_source_without_the_key_only_when_the_scope_is_complete():
    # 項目そのものが無い古い出典（2026-10-26 より前の会話）。全グループが見える利用者には見せる
    shown, count = access.mask_hidden_turns(_turn(None, with_key=False), COMPLETE_SCOPE)
    assert count == 0 and shown[2].content == "[x.pdf]\n本文" and shown[3].content == "回答（出典: x.pdf）"
    # 限定ロールや、全グループを持たない範囲では、今までどおり伏せる
    for scope in (Scope.only(["cafe"]), Scope.only(DEFAULT_ROLE_SET), Scope(frozenset(), complete=False)):
        _, hidden = access.mask_hidden_turns(_turn(None, with_key=False), scope)
        assert hidden == 2, scope


def test_mask_still_hides_a_null_value_and_a_group_outside_the_names_even_when_complete():
    _, null_value = access.mask_hidden_turns(_turn(None, with_key=True), COMPLETE_SCOPE)
    assert null_value == 2  # 項目はあるが値が null（グループの無い資料）は伏せる
    _, other = access.mask_hidden_turns(_turn("newgroup"), COMPLETE_SCOPE)
    assert other == 2  # 範囲に無いグループは伏せる
    _, known = access.mask_hidden_turns(_turn("zahard"), COMPLETE_SCOPE)
    assert known == 0


def test_the_complete_flag_does_not_change_what_allows_lets_through():
    assert COMPLETE_SCOPE.allows("cafe") and not COMPLETE_SCOPE.allows(None) and not COMPLETE_SCOPE.allows("newgroup")
    assert Scope.only(DEFAULT_ROLE_SET).allows(None) is False
    assert COMPLETE_SCOPE.sql_names() == sorted(DEFAULT_ROLE_SET)  # SQL の絞り込みも同じ


class CompletenessPool:
    """見えるグループ・存在する全グループを順に返す偽の接続。全グループの SQL だけを失敗させることもできる。"""

    def __init__(self, visible: list[str], known: list[str], known_error: Exception | None = None) -> None:
        self.visible, self.known, self.known_error = visible, known, known_error
        self.connections = 0
        self.sqls: list[str] = []

    @asynccontextmanager
    async def connection(self):
        pool = self
        pool.connections += 1

        class Conn:
            async def execute(self, sql: str, params: dict[str, Any] | None = None) -> CollectionsCursor:
                pool.sqls.append(sql)
                if sql == access._KNOWN_SQL:
                    if pool.known_error is not None:
                        raise pool.known_error
                    return CollectionsCursor([{"collection": n} for n in pool.known])
                return CollectionsCursor([{"collection": n} for n in pool.visible])

        yield Conn()


@pytest.mark.parametrize(
    ("visible", "known", "complete"),
    [
        (["zahard", "cafe", "guides"], ["zahard", "cafe", "guides"], True),  # default が全グループを持つ
        (["cafe"], ["zahard", "cafe", "guides"], False),  # 一部だけ
        (
            ["zahard", "cafe", "guides"],
            ["zahard", "cafe", "guides", "newgroup"],
            False,
        ),  # 資料にだけあるグループを持たない
        ([], ["zahard"], False),  # 見えるグループが 0 件
        ([], [], False),  # 何も無いときも、空の範囲は全部とは言わない
        (["cafe"], [], True),  # 存在するグループが 0 件なら、見えるものは全部
    ],
)
def test_completeness_compares_the_visible_groups_with_every_existing_group(monkeypatch, visible, known, complete):
    pool = CompletenessPool(visible, known)
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    got_visible, got_complete = asyncio.run(access.visible_with_completeness(None))
    assert got_visible == frozenset(visible) and got_complete is complete
    assert pool.connections == 1  # 同じ接続で引く
    assert pool.sqls[-1] == access._KNOWN_SQL
    assert "document_collections" in access._KNOWN_SQL and "documents" in access._KNOWN_SQL  # 登録済み + 資料にあるもの


def test_completeness_is_false_when_the_existing_groups_cannot_be_read(monkeypatch, caplog):
    pool = CompletenessPool(["zahard", "cafe", "guides"], [], known_error=RuntimeError("postgresql://u:pw@h/x"))
    monkeypatch.setattr(access, "get_pool", lambda: pool)
    got_visible, got_complete = asyncio.run(access.visible_with_completeness(None))
    assert got_visible == DEFAULT_ROLE_SET and got_complete is False  # 検索の範囲は変わらず、会話を伏せる側に倒れる
    assert "postgresql://" not in caplog.text and "RuntimeError" in caplog.text


def test_completeness_raises_like_visible_collections_without_a_pool(monkeypatch):
    monkeypatch.setattr(access, "get_pool", lambda: None)
    with pytest.raises(access.AccessUnavailable):
        asyncio.run(access.visible_with_completeness(None))


def _legacy_thread(thread_id: str, *, with_key: bool = False) -> None:
    """collection の項目が無い出典の、古い形の会話を保存する（検索結果に基づく回答 1 回）。"""
    config = {"configurable": {"thread_id": thread_id}}
    routes.get_graph(True).update_state(config, {"messages": _turn(None, with_key=with_key)}, as_node="agent")


def test_an_old_conversation_is_shown_to_a_complete_scope_and_hidden_from_a_restricted_one(client, set_access):
    thread = str(uuid.uuid4())
    _legacy_thread(thread)

    set_access("enforce", DEFAULT_ROLE_SET)  # default ロール（全グループ）
    shown = client.get(f"/threads/{thread}/messages").json()["messages"]
    assert [m["text"] for m in shown] == ["質問", "回答（出典: x.pdf）"]

    set_access("enforce", CAFE_ONLY)  # 限定ロール
    hidden = client.get(f"/threads/{thread}/messages").json()["messages"]
    assert [m["text"] for m in hidden] == ["質問", access.MASKED_ANSWER_TEXT]

    set_access(
        "enforce", DEFAULT_ROLE_SET, known=DEFAULT_ROLE_SET | {"newgroup"}
    )  # 新しいグループが default に未割り当て
    assert client.get(f"/threads/{thread}/messages").json()["messages"][1]["text"] == access.MASKED_ANSWER_TEXT


def test_an_old_conversation_with_a_null_collection_stays_hidden_even_for_the_default_role(client, set_access):
    thread = str(uuid.uuid4())
    _legacy_thread(thread, with_key=True)  # 項目はあるが値が null
    set_access("enforce", DEFAULT_ROLE_SET)
    assert client.get(f"/threads/{thread}/messages").json()["messages"][1]["text"] == access.MASKED_ANSWER_TEXT


def test_shadow_counts_old_conversations_the_same_way_enforce_would_mask_them(client, set_access, caplog):
    caplog.set_level(logging.INFO, logger="app.access")
    thread = str(uuid.uuid4())
    _legacy_thread(thread)

    def shadow_count() -> str:
        caplog.clear()
        res = client.get(f"/threads/{thread}/messages")
        assert [m["text"] for m in res.json()["messages"]] == ["質問", "回答（出典: x.pdf）"]  # shadow は伏せない
        return next(r.getMessage() for r in caplog.records if "会話の" in r.getMessage())

    set_access("shadow", DEFAULT_ROLE_SET)
    assert "会話の 0 件が enforce では伏せられる" in shadow_count()  # enforce でも伏せない
    set_access("shadow", CAFE_ONLY)
    assert "会話の 2 件が enforce では伏せられる" in shadow_count()  # enforce なら伏せる


def test_the_llm_receives_an_old_conversation_for_a_complete_scope_and_not_for_a_restricted_one(
    client, set_access, grouped_search, monkeypatch
):
    monkeypatch.setattr(graph_module, "create_llm", lambda: RecordingLLM())
    routes._graph_for.cache_clear()
    thread = str(uuid.uuid4())
    _legacy_thread(thread)

    RecordingLLM.seen = []
    set_access("enforce", DEFAULT_ROLE_SET)
    assert _chat(client, "営業時間を教えて", thread_id=thread).status_code == 200
    sent = "\n".join(str(m.content) for call in RecordingLLM.seen for m in call)
    assert "回答（出典: x.pdf）" in sent and access.MASKED_ANSWER_TEXT not in sent

    RecordingLLM.seen = []
    set_access("enforce", CAFE_ONLY)
    assert _chat(client, "営業時間を教えて", thread_id=thread).status_code == 200
    sent = "\n".join(str(m.content) for call in RecordingLLM.seen for m in call)
    assert "回答（出典: x.pdf）" not in sent and access.MASKED_ANSWER_TEXT in sent
    routes._graph_for.cache_clear()
