"""会話の一覧（GET /threads）、発言の取得（GET /threads/{id}/messages）と本人による削除（DELETE /threads/{id}）。履歴はメモリ。

確かめるのは、本人の会話だけが出ること、題名、他人の会話を消せないこと、匿名（off）の扱い、失敗時の固定メッセージ。
Postgres での同じ確認は tests/test_threads_postgres.py（TEST_DATABASE_URL があるときだけ）。
"""

import time
import uuid

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import auth
from app.api import routes, threads
from tests.test_auth import SUPABASE_URL, Clock, JwksServer, Signer


@pytest.fixture
def signer() -> Signer:
    return Signer()


@pytest.fixture
def required(monkeypatch, signer) -> None:
    monkeypatch.setattr(auth, "fetch_jwks_document", JwksServer(signer))
    monkeypatch.setattr(auth, "_cache", None)
    monkeypatch.setattr(auth, "_now", Clock())
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("SUPABASE_URL", SUPABASE_URL)


@pytest.fixture
def user() -> str:
    # テストごとに新しい利用者にする（メモリ履歴はグラフと一緒に使い回されるので、他のテストの会話が混ざらない）
    return str(uuid.uuid4())


def _bearer(signer: Signer, sub: str) -> dict:
    return {"Authorization": f"Bearer {signer.token(sub=sub)}"}


def _chat(client, headers: dict, message: str, thread_id: str | None = None) -> str:
    body: dict = {"message": message}
    if thread_id is not None:
        body["thread_id"] = thread_id
    res = client.post("/chat", json=body, headers=headers)
    assert res.status_code == 200
    return res.json()["thread_id"]


# --- 一覧 --------------------------------------------------------------------


def test_list_returns_only_the_users_own_threads_newest_first(client, required, signer, user):
    other = str(uuid.uuid4())
    mine = _bearer(signer, user)
    first = _chat(client, mine, "最初の質問です")
    time.sleep(0.01)
    second = _chat(client, mine, "二つ目の質問です")
    _chat(client, _bearer(signer, other), "他人の質問です")

    res = client.get("/threads", headers=mine)

    assert res.status_code == 200
    items = res.json()["threads"]
    assert [item["thread_id"] for item in items] == [second, first]  # 新しい順。接頭辞は付かない
    assert [item["title"] for item in items] == ["二つ目の質問です", "最初の質問です"]
    assert all(item["created_at"] and item["updated_at"] for item in items)
    assert "他人の質問です" not in res.text


def test_title_is_the_first_user_message_even_after_more_turns(client, required, signer, user):
    headers = _bearer(signer, user)
    thread_id = _chat(client, headers, "最初の質問です")
    _chat(client, headers, "続きの質問です", thread_id)

    items = client.get("/threads", headers=headers).json()["threads"]

    assert [item["title"] for item in items] == ["最初の質問です"]


def test_list_is_empty_for_a_user_without_threads(client, required, signer, user):
    assert client.get("/threads", headers=_bearer(signer, user)).json() == {"threads": []}


def test_list_is_capped_to_the_newest_threads(client, required, signer, user, monkeypatch):
    monkeypatch.setattr(threads, "MAX_THREADS", 2)
    headers = _bearer(signer, user)
    for text in ("一番目", "二番目", "三番目"):
        _chat(client, headers, text)
        time.sleep(0.01)

    items = client.get("/threads", headers=headers).json()["threads"]

    assert [item["title"] for item in items] == ["三番目", "二番目"]


def test_list_needs_a_token_when_auth_is_required(client, required):
    res = client.get("/threads")
    assert res.status_code == 401
    assert res.headers["www-authenticate"] == "Bearer"


def test_list_is_404_when_auth_is_off(client, monkeypatch):
    # 匿名の会話には持ち主がいない。一覧にすると、他人の会話が見える
    monkeypatch.delenv("AUTH_MODE", raising=False)
    _chat(client, {}, "匿名の質問です")

    res = client.get("/threads")

    assert res.status_code == 404
    assert "匿名の質問です" not in res.text


# --- 題名 --------------------------------------------------------------------


def test_make_title_collapses_whitespace_and_truncates():
    assert (
        threads.make_title([HumanMessage(content="  ザーハードの\n大気   について  ")]) == "ザーハードの 大気 について"
    )
    long = threads.make_title([HumanMessage(content="あ" * 100)])
    assert long == "あ" * (threads.MAX_TITLE_LENGTH - 1) + "…"
    assert len(long) == threads.MAX_TITLE_LENGTH
    exact = "い" * threads.MAX_TITLE_LENGTH
    assert threads.make_title([HumanMessage(content=exact)]) == exact


def test_make_title_uses_the_first_human_message_and_handles_block_content():
    messages = [
        AIMessage(content="先に AI"),
        HumanMessage(content=[{"type": "text", "text": "ブロック形式"}, "と文字列"]),
    ]
    assert threads.make_title(messages) == "ブロック形式 と文字列"
    assert threads.make_title([]) == "（題名なし）"
    assert threads.make_title([AIMessage(content="AI だけ")]) == "（題名なし）"


# --- 発言の取得 -------------------------------------------------------------


def test_messages_returns_the_conversation_in_order(client, required, signer, user):
    headers = _bearer(signer, user)
    thread_id = _chat(client, headers, "最初の質問です")
    _chat(client, headers, "続きの質問です", thread_id)

    res = client.get(f"/threads/{thread_id}/messages", headers=headers)

    assert res.status_code == 200
    messages = res.json()["messages"]
    assert [(m["role"], m["text"]) for m in messages if m["role"] == "user"] == [
        ("user", "最初の質問です"),
        ("user", "続きの質問です"),
    ]
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert all(m["text"] for m in messages)


def test_messages_of_another_users_thread_is_404(client, required, signer, user):
    other = str(uuid.uuid4())
    thread_id = _chat(client, _bearer(signer, other), "他人の会話です")

    res = client.get(f"/threads/{thread_id}/messages", headers=_bearer(signer, user))

    assert res.status_code == 404
    assert res.json()["detail"] == "指定された会話が見つかりません。"
    assert "他人の会話です" not in res.text


def test_messages_of_an_unknown_thread_is_404(client, required, signer, user):
    assert client.get("/threads/nothing/messages", headers=_bearer(signer, user)).status_code == 404


def test_messages_need_a_token_when_auth_is_required(client, required, signer, user):
    thread_id = _chat(client, _bearer(signer, user), "守る会話です")

    assert client.get(f"/threads/{thread_id}/messages").status_code == 401


def test_messages_reject_ids_that_could_carry_a_prefix(client, required, signer, user):
    res = client.get(f"/threads/{uuid.uuid4()}:abc/messages", headers=_bearer(signer, user))
    assert res.status_code == 422


def test_messages_in_off_mode_are_read_by_thread_id(client, monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    thread_id = _chat(client, {}, "匿名の会話です")

    res = client.get(f"/threads/{thread_id}/messages")

    assert res.status_code == 200
    assert res.json()["messages"][0] == {"role": "user", "text": "匿名の会話です"}
    assert client.get(f"/threads/{uuid.uuid4()}/messages").status_code == 404


def test_visible_messages_skip_tool_traffic_and_empty_text():
    messages = [
        HumanMessage(content="ザーハードの大気は？"),
        AIMessage(content="", tool_calls=[{"name": "search_documents", "args": {"query": "大気"}, "id": "c1"}]),
        ToolMessage(content="資料の本文（画面に出さない）", tool_call_id="c1"),
        AIMessage(content=[{"type": "text", "text": "窒素が主成分です。"}, {"type": "thinking", "thinking": "内部"}]),
        AIMessage(content="   "),
    ]

    assert [(m.role, m.text) for m in threads.visible_messages(messages)] == [
        ("user", "ザーハードの大気は？"),
        ("assistant", "窒素が主成分です。"),
    ]


# --- 削除 --------------------------------------------------------------------


def test_user_can_delete_their_own_thread(client, required, signer, user):
    headers = _bearer(signer, user)
    keep = _chat(client, headers, "残す会話です")
    gone = _chat(client, headers, "消す会話です")

    res = client.delete(f"/threads/{gone}", headers=headers)

    assert res.status_code == 204
    assert res.content == b""
    assert [item["thread_id"] for item in client.get("/threads", headers=headers).json()["threads"]] == [keep]
    # 履歴そのものが消えている（同じ ID で話しかけても、前の発言は残っていない）
    state = routes.get_graph(True).get_state({"configurable": {"thread_id": f"{user}:{gone}"}}).values
    assert state.get("messages", []) == []
    assert client.delete(f"/threads/{gone}", headers=headers).status_code == 404  # 二度目は無い


def test_user_cannot_delete_another_users_thread(client, required, signer, user):
    other = str(uuid.uuid4())
    thread_id = _chat(client, _bearer(signer, other), "他人の会話です")

    res = client.delete(f"/threads/{thread_id}", headers=_bearer(signer, user))

    assert res.status_code == 404  # 存在しないのと区別しない
    assert res.json()["detail"] == "指定された会話が見つかりません。"
    still = client.get("/threads", headers=_bearer(signer, other)).json()["threads"]
    assert [item["thread_id"] for item in still] == [thread_id]  # 持ち主の会話は残っている


def test_delete_needs_a_token_when_auth_is_required(client, required, signer, user):
    thread_id = _chat(client, _bearer(signer, user), "消されない会話です")

    assert client.delete(f"/threads/{thread_id}").status_code == 401
    assert len(client.get("/threads", headers=_bearer(signer, user)).json()["threads"]) == 1


def test_delete_rejects_ids_that_could_carry_a_prefix(client, required, signer, user):
    other = str(uuid.uuid4())
    res = client.delete(f"/threads/{other}:abc", headers=_bearer(signer, user))
    assert res.status_code == 422  # ':' は thread_id に使えない（/chat と同じ検証）


def test_delete_in_off_mode_removes_the_thread_by_its_id(client, monkeypatch):
    monkeypatch.delenv("AUTH_MODE", raising=False)
    thread_id = _chat(client, {}, "匿名の会話です")

    assert client.delete(f"/threads/{thread_id}").status_code == 204
    assert client.delete(f"/threads/{thread_id}").status_code == 404
    assert client.delete(f"/threads/{uuid.uuid4()}").status_code == 404


# --- 失敗 --------------------------------------------------------------------


class _BrokenSaver:
    """履歴の保存先が壊れているときの偽物。例外の文字列に、応答へ出してはいけない値を入れる。"""

    SECRET = "postgresql://user:SECRET@db.internal/postgres"

    async def aget_tuple(self, config):
        raise RuntimeError(self.SECRET)

    async def alist(self, config):
        raise RuntimeError(self.SECRET)
        yield  # pragma: no cover


@pytest.mark.parametrize(
    "method, path", [("get", "/threads"), ("get", "/threads/abc/messages"), ("delete", "/threads/abc")]
)
def test_store_failure_is_a_fixed_503(client, required, signer, user, monkeypatch, method, path):
    monkeypatch.setattr(threads, "_saver", lambda: _BrokenSaver())

    res = getattr(client, method)(path, headers=_bearer(signer, user))

    assert res.status_code == 503
    assert res.json()["detail"] == "会話履歴を操作できませんでした。しばらく待ってから再試行してください。"
    assert "SECRET" not in res.text
