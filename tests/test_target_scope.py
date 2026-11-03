"""質問の対象（`target`。資料の `documents.target`）。実 API・実 DB は使わない。tests/test_collection_scope.py（旧 collection）の対になるテスト。

確かめるのは、検索への受け渡し（LLM の引数ではなく、会話の状態から）、会話ごとの固定と 409、`collection` との同時指定（422）、
新しい会話で使えない対象（403。名前を載せない）、続きの会話の対象が空になった場合、旧 collection の会話が target を足されても
範囲を変えないこと、画面向けの情報（メタデータ・履歴の取得）。Postgres の本物の SQL と実際のロールは
tests/test_classification_postgres.py。
"""

import asyncio
import json
import uuid
from typing import Any

import pytest
from langchain_core.utils.function_calling import convert_to_openai_tool

from app import access
from app.agents import tools
from app.agents.tools import search_documents
from app.api import routes, threads
from app.scope import Scope
from app.search.types import Hit


class TargetedSearch:
    """検索の偽物。資料を対象別に持ち、渡された collection と target で絞る。呼び出しを記録する。"""

    DOCS = (
        ("zahard", "zahard", "ザーハード1.pdf", "ザーハードの大気は薄い。"),
        ("cafe", "minato-coffee", "メニュー.pdf", "大気の話ではなく、コーヒーのメニューがある。"),
        ("guides", "aoba-aw820", "AW-820.pdf", "大気ではなく、洗濯機の取扱説明書。"),
        ("guides", "tsubasa-dorm", "入寮規程.pdf", "大気の話ではなく、寮の規程。"),
        (None, None, "メモ.pdf", "グループも対象も無い資料にも大気という語がある。"),
    )

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(
        self,
        query: str,
        top_k: int = 2,
        collection: str | None = None,
        *,
        scope: Scope | None = None,
        target: str | None = None,
    ) -> list[Hit]:
        self.calls.append({"query": query, "collection": collection, "target": target})
        hits = [
            Hit(name, text, 1, None, 0.8, group, None, doc_target)
            for group, doc_target, name, text in self.DOCS
            if "大気" in query
        ]
        if collection is not None:
            hits = [h for h in hits if h.collection == collection]
        if target is not None:
            hits = [h for h in hits if h.target == target]
        return hits[:top_k]


@pytest.fixture
def search(monkeypatch: pytest.MonkeyPatch, searching_llm) -> TargetedSearch:
    fake = TargetedSearch()
    monkeypatch.setattr(tools, "search_chunks_async", fake)
    return fake


def chat(client, thread_id: str | None, **scope: str):
    body: dict[str, Any] = {"message": "大気について教えて", **scope}
    if thread_id is not None:
        body["thread_id"] = thread_id
    return client.post("/chat", json=body)


def stream(client, thread_id: str | None, **scope: str):
    body: dict[str, Any] = {"message": "大気について教えて", **scope}
    if thread_id is not None:
        body["thread_id"] = thread_id
    return client.post("/chat/stream", json=body)


def final_metadata(res) -> dict:
    found: dict = {}
    for block in res.text.split("\n\n"):
        if block.startswith("data: {"):
            chunk = json.loads(block[len("data: ") :])
            if chunk.get("type") == "message-metadata":
                found = chunk["messageMetadata"]
    return found


# ---- 検索への受け渡し ----


def test_without_a_target_the_search_is_called_as_before(client, search):
    res = chat(client, str(uuid.uuid4()))

    assert res.status_code == 200
    assert search.calls == [{"query": "大気について教えて", "collection": None, "target": None}]
    assert res.json()["target"] is None and res.json()["collection"] is None


def test_the_target_reaches_the_search_and_only_that_targets_documents_come_back(client, search):
    res = chat(client, str(uuid.uuid4()), target="aoba-aw820")

    assert res.status_code == 200
    assert search.calls == [{"query": "大気について教えて", "collection": None, "target": "aoba-aw820"}]
    body = res.json()
    assert body["target"] == "aoba-aw820" and body["collection"] is None
    assert "AW-820.pdf" in body["reply"] and "メニュー.pdf" not in body["reply"] and "入寮規程.pdf" not in body["reply"]


def test_the_model_cannot_choose_the_target_the_tool_definition_is_unchanged():
    parameters = convert_to_openai_tool(search_documents)["function"]["parameters"]
    assert parameters == {"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]}


def test_a_target_in_the_tool_arguments_is_not_a_way_to_change_the_scope(search):
    call = {"name": "search_documents", "args": {"query": "大気", "target": "zahard"}, "id": "c1", "type": "tool_call"}
    asyncio.run(search_documents.ainvoke(call, {"configurable": {"target": "minato-coffee"}}))

    assert search.calls == [{"query": "大気", "collection": None, "target": "minato-coffee"}]


def test_the_sources_record_the_target_at_answer_time(client, search):
    meta = final_metadata(stream(client, str(uuid.uuid4()), target="tsubasa-dorm"))

    assert meta["target"] == "tsubasa-dorm"
    assert [(s["name"], s["target"]) for s in meta["sources"]] == [("入寮規程.pdf", "tsubasa-dorm")]
    assert meta["hit_count"] == 1


# ---- 入力の検証 ----


@pytest.mark.parametrize("bad", ["", "a b", "対象", "x" * 41, "a/b", "A-b", "-abc", "a;b"])
def test_a_malformed_target_is_422_for_both_endpoints(client, bad):
    for path in ("/chat", "/chat/stream"):
        res = client.post(path, json={"message": "大気", "target": bad})
        assert res.status_code == 422, (path, bad)


def test_target_and_collection_together_are_422_for_a_new_and_an_existing_conversation(client, search):
    both = {"target": "zahard", "collection": "zahard"}
    for path in ("/chat", "/chat/stream"):
        assert client.post(path, json={"message": "大気", **both}).status_code == 422
    thread = str(uuid.uuid4())
    chat(client, thread, target="zahard")
    calls_before = len(search.calls)
    assert client.post("/chat", json={"message": "大気", "thread_id": thread, **both}).status_code == 422
    assert len(search.calls) == calls_before  # 黙って片方を捨てて、検索はしない


# ---- 会話ごとの固定 ----


def test_a_follow_up_without_a_target_keeps_the_target_of_the_conversation(client, search):
    thread = str(uuid.uuid4())
    chat(client, thread, target="zahard")

    res = chat(client, thread)  # 省略 = 状態から復元（広がらない）

    assert res.status_code == 200
    assert res.json()["target"] == "zahard"
    assert search.calls[-1]["target"] == "zahard"


def test_the_same_target_again_is_accepted(client, search):
    thread = str(uuid.uuid4())
    chat(client, thread, target="zahard")

    assert chat(client, thread, target="zahard").status_code == 200


def test_a_different_target_is_409_and_names_no_scope(client, search):
    thread = str(uuid.uuid4())
    chat(client, thread, target="zahard")
    calls_before = len(search.calls)

    res = chat(client, thread, target="minato-coffee")

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert "zahard" not in detail and "minato-coffee" not in detail
    assert detail == routes._DETAIL_SCOPE_CHANGED
    assert len(search.calls) == calls_before  # 検索も LLM も動かない


def test_a_conversation_without_a_target_is_not_narrowed_later_so_a_target_is_409(client, search):
    thread = str(uuid.uuid4())
    chat(client, thread)

    assert chat(client, thread, target="zahard").status_code == 409


def test_a_target_conversation_cannot_switch_to_a_collection(client, search):
    thread = str(uuid.uuid4())
    chat(client, thread, target="zahard")

    assert chat(client, thread, collection="zahard").status_code == 409


def test_a_legacy_collection_conversation_keeps_its_scope_and_is_never_converted(client, search):
    thread = str(uuid.uuid4())
    first = chat(client, thread, collection="guides")
    assert first.json()["collection"] == "guides" and first.json()["target"] is None

    # target を足しても変換されない（409）。省略なら、旧 collection のまま（全資料に広がらない）
    assert chat(client, thread, target="aoba-aw820").status_code == 409
    again = chat(client, thread)
    assert again.status_code == 200
    assert (again.json()["collection"], again.json()["target"]) == ("guides", None)
    assert search.calls[-1] == {"query": "大気について教えて", "collection": "guides", "target": None}
    assert "メニュー.pdf" not in again.json()["reply"]


def test_the_target_is_per_conversation(client, search):
    a, b = str(uuid.uuid4()), str(uuid.uuid4())
    chat(client, a, target="zahard")
    chat(client, b, target="minato-coffee")

    assert chat(client, a).json()["target"] == "zahard"
    assert chat(client, b).json()["target"] == "minato-coffee"


# ---- 新しい会話で使えない対象（403） ----


def test_a_target_without_visible_documents_is_403_for_a_new_conversation_with_a_fixed_text(
    client, search, monkeypatch
):
    seen: list[str] = []

    async def invisible(scope, target):
        seen.append(target)
        return False

    monkeypatch.setattr(routes, "target_is_visible", invisible)

    for call in (chat, stream):
        res = call(client, str(uuid.uuid4()), target="secret-target")
        assert res.status_code == 403
        assert res.json()["detail"] == access.DETAIL_FORBIDDEN_TARGET
        assert "secret-target" not in res.text  # 見えない・存在しない・資料のない対象は、同じ文で名前も載せない
    assert seen == ["secret-target", "secret-target"]
    assert search.calls == []


def test_a_target_that_became_empty_does_not_stop_an_existing_conversation(client, search, monkeypatch):
    thread = str(uuid.uuid4())
    chat(client, thread, target="zahard")

    async def never(scope, target):  # 続きの会話では確かめ直さない
        raise AssertionError("続きの会話で対象の有無を引いた")

    monkeypatch.setattr(routes, "target_is_visible", never)
    res = chat(client, thread)

    assert res.status_code == 200
    assert search.calls[-1]["target"] == "zahard"


def test_a_failure_checking_the_target_is_a_fixed_503(client, search, monkeypatch):
    async def broken(scope, target):
        raise access.AccessUnavailable("postgresql://user:pw@db.example.com/x")

    monkeypatch.setattr(routes, "target_is_visible", broken)
    res = chat(client, str(uuid.uuid4()), target="zahard")

    assert res.status_code == 503
    assert res.json()["detail"] == access.DETAIL_UNAVAILABLE and "pw@" not in res.text


# ---- /chat/stream ----


def test_stream_returns_409_as_http_before_the_response_starts(client, search):
    thread = str(uuid.uuid4())
    stream(client, thread, target="zahard")

    res = stream(client, thread, target="minato-coffee")

    assert res.status_code == 409
    assert res.headers["content-type"].startswith("application/json")


def test_stream_metadata_carries_the_target_and_restores_it(client, search):
    thread = str(uuid.uuid4())
    first = final_metadata(stream(client, thread, target="zahard"))
    again = final_metadata(stream(client, thread))

    assert first["target"] == again["target"] == "zahard"
    assert first["hit_count"] == again["hit_count"] == 1


def test_stream_zero_hits_with_a_target_is_hit_count_zero(client, search):
    meta = final_metadata(client.post("/chat/stream", json={"message": "コーヒーの値段", "target": "zahard"}))

    assert meta["target"] == "zahard"
    assert meta["hit_count"] == 0 and meta["sources"] == []


# ---- 履歴の取得（既存の会話を開いたとき） ----


def test_the_messages_endpoint_restores_the_target(client, search):
    thread, other, legacy = (str(uuid.uuid4()) for _ in range(3))
    chat(client, thread, target="zahard")
    chat(client, other)
    chat(client, legacy, collection="cafe")

    body = client.get(f"/threads/{thread}/messages").json()
    assert (body["target"], body["collection"], body["scope_unavailable"]) == ("zahard", None, False)
    body = client.get(f"/threads/{other}/messages").json()
    assert (body["target"], body["collection"], body["scope_unavailable"]) == (None, None, False)
    body = client.get(f"/threads/{legacy}/messages").json()
    assert (body["target"], body["collection"], body["scope_unavailable"]) == (None, "cafe", False)


def test_the_messages_endpoint_does_not_name_a_target_that_is_no_longer_visible(client, search, monkeypatch):
    thread = str(uuid.uuid4())
    chat(client, thread, target="zahard")

    async def invisible(scope, target):
        return False

    monkeypatch.setattr(threads, "target_is_visible", invisible)
    body = client.get(f"/threads/{thread}/messages").json()

    assert body["target"] is None and body["scope_unavailable"] is True  # 「すべて」ではなく「利用できない」
    assert "zahard" not in json.dumps(body)


# ---- 検索の部品 ----


def test_the_scope_helper_sql_matches_the_documents_target_column():
    from app.scope import TARGET_FILTER_SQL

    assert "d.target" in TARGET_FILTER_SQL and "%(target)s" in TARGET_FILTER_SQL
