"""会話の一覧（GET /threads）、発言の取得（GET /threads/{id}/messages）と本人による削除（DELETE /threads/{id}）。履歴はメモリ。

確かめるのは、本人の会話だけが出ること、題名、他人の会話を消せないこと、匿名（off）の扱い、失敗時の固定メッセージ。
"""

import time
import uuid
from contextlib import asynccontextmanager

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from app import auth
from app.api import routes, threads
from app.pg_saver import PgSaver
from tests.test_auth import AUTH_BASE_URL, Clock, JwksServer, Signer


@pytest.fixture
def signer() -> Signer:
    return Signer()


@pytest.fixture
def required(monkeypatch, signer) -> None:
    monkeypatch.setattr(auth, "fetch_jwks_document", JwksServer(signer))
    monkeypatch.setattr(auth, "_cache", None)
    monkeypatch.setattr(auth, "_now", Clock())
    monkeypatch.setenv("AUTH_MODE", "required")
    monkeypatch.setenv("AUTH_BASE_URL", AUTH_BASE_URL)


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


def _search_result(call_id: str, name: str, page: int, text: str, number: int = 1) -> ToolMessage:
    """graph.py の tools ノードが保存する検索結果の形（番号つきの本文と、出典の artifact）。"""
    return ToolMessage(
        content=f"[{number}] {name}\n{text}",
        tool_call_id=call_id,
        artifact=[
            {
                "name": name,
                "page": page,
                "page_end": None,
                "weak": False,
                "collection": "zahard",
                "target": "zahard",
                "document_id": 7,
            }
        ],
    )


def _call(call_id: str) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": "search_documents", "args": {"query": "q"}, "id": call_id}])


def test_visible_messages_attach_the_last_search_sources_to_the_answer():
    messages = [
        HumanMessage(content="大気は？"),
        _call("c1"),
        _search_result("c1", "ザーハード1.pdf", 2, "窒素が主成分です。"),
        AIMessage(content="窒素が主成分です。[1]"),
    ]

    answer = threads.visible_messages(messages)[1]

    assert answer.context == "[1] ザーハード1.pdf\n窒素が主成分です。"
    assert [(s.name, s.page, s.document_id, s.collection) for s in answer.sources] == [
        ("ザーハード1.pdf", 2, 7, "zahard")
    ]
    assert threads.visible_messages(messages)[0].sources is None
    assert "target" not in answer.model_dump()["sources"][0]  # 見えない対象の名前を返さないため、対象は返さない
    assert threads.visible_messages(messages, with_sources=False)[1].sources is None


def test_visible_messages_keep_only_the_last_search_of_a_turn_and_do_not_leak_into_the_next():
    messages = [
        HumanMessage(content="1 問目"),
        _call("c1"),
        _search_result("c1", "古い検索.pdf", 1, "最初の検索"),
        _call("c2"),
        _search_result("c2", "最後の検索.pdf", 3, "二度目の検索"),
        AIMessage(content="答えです。"),
        HumanMessage(content="2 問目（検索しない）"),
        AIMessage(content="検索せずに答えます。"),
    ]

    shown = threads.visible_messages(messages)

    assert [s.name for s in shown[1].sources] == ["最後の検索.pdf"]
    assert shown[3].sources is None and shown[3].context is None


def test_visible_messages_join_parallel_searches_of_one_call_group():
    messages = [
        HumanMessage(content="質問"),
        AIMessage(
            content="",
            tool_calls=[
                {"name": "search_documents", "args": {"query": "a"}, "id": "c1"},
                {"name": "search_documents", "args": {"query": "b"}, "id": "c2"},
            ],
        ),
        _search_result("c1", "A.pdf", 1, "本文A", 1),
        _search_result("c2", "B.pdf", 4, "本文B", 2),
        AIMessage(content="答え。"),
    ]

    answer = threads.visible_messages(messages)[1]

    assert [s.name for s in answer.sources] == ["A.pdf", "B.pdf"]
    assert answer.context == "[1] A.pdf\n本文A\n\n[2] B.pdf\n本文B"


def test_visible_messages_omit_sources_for_masked_or_missing_artifacts():
    masked = ToolMessage(content="（伏せた検索結果）", tool_call_id="c1", artifact=[])
    legacy = ToolMessage(content="[資料.pdf]\n古い形式の本文", tool_call_id="c2")  # artifact を持たない古い会話
    messages = [
        HumanMessage(content="Q1"),
        _call("c1"),
        masked,
        AIMessage(content="伏せた回答"),
        HumanMessage(content="Q2"),
        _call("c2"),
        legacy,
        AIMessage(content="古い回答"),
    ]

    shown = threads.visible_messages(messages)

    assert shown[1].sources is None and shown[3].sources is None
    assert shown[1].model_dump(exclude_unset=True) == {"role": "assistant", "text": "伏せた回答"}


def test_thread_messages_endpoint_returns_sources_only_where_present(client):
    from langgraph.checkpoint.memory import MemorySaver

    thread_id = f"t{uuid.uuid4().hex}"
    saver = MemorySaver()
    messages = [
        HumanMessage(content="大気は？"),
        _call("c1"),
        _search_result("c1", "ザーハード1.pdf", 2, "窒素が主成分です。"),
        AIMessage(content="窒素が主成分です。[1]"),
    ]
    cfg = {"configurable": {"thread_id": thread_id, "checkpoint_ns": ""}}
    import asyncio as _asyncio

    from langgraph.checkpoint.base import empty_checkpoint

    checkpoint = empty_checkpoint()
    checkpoint["channel_values"] = {"messages": messages}
    checkpoint["channel_versions"] = {"messages": 1}
    _asyncio.run(saver.aput(cfg, checkpoint, {"source": "loop", "step": 0, "parents": {}}, {"messages": 1}))
    from app import history

    history._checkpointer = saver
    try:
        body = client.get(f"/threads/{thread_id}/messages").json()
    finally:
        history._checkpointer = None

    user, answer = body["messages"]
    assert user == {"role": "user", "text": "大気は？"}
    assert answer["sources"][0]["name"] == "ザーハード1.pdf"
    assert answer["sources"][0]["page"] == 2
    assert answer["context"].startswith("[1] ザーハード1.pdf")


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


# --- Postgres の経路: 会話の数によらず 1 回の問い合わせ -------------------------------------
# 本物の Postgres での SQL の実行と、aget_tuple との一致は tests/test_history_pg_backend.py（PG_TEST_URL が要る）。
# ここは、偽の接続で「問い合わせの回数」と「応答の形」を確かめる（Postgres は要らない）。


class _CountingPool:
    """`get_pool()` の偽物。`connection().execute()` の呼び出しを記録し、決めた行を返す。"""

    def __init__(self, rows: list[dict]) -> None:
        self.rows = rows
        self.statements: list[tuple[str, object]] = []

    @asynccontextmanager
    async def connection(self):
        pool = self

        class _Cursor:
            async def fetchall(self) -> list[dict]:
                return pool.rows

        class _Connection:
            async def execute(self, sql, params=None):
                pool.statements.append((sql, params))
                return _Cursor()

        yield _Connection()


def _checkpoint_row(key: str, created_ms: int, updated_ms: int, messages: list) -> dict:
    """`threads._LIST_SQL` が返す 1 行（最新のチェックポイントは、実際の PgSaver と同じ直列化）。"""
    type_, blob = PgSaver(None).serde.dumps_typed({"channel_values": {"messages": messages}})
    return {"thread_id": key, "created_at": created_ms, "updated_at": updated_ms, "type": type_, "checkpoint": blob}


@pytest.fixture
def pg_path(monkeypatch):
    """Postgres の経路（get_pool() が None でない）に入り、行を差し込める。"""

    def install(rows: list[dict]) -> _CountingPool:
        pool = _CountingPool(rows)
        monkeypatch.setattr(threads, "get_pool", lambda: pool)
        monkeypatch.setattr(threads, "_saver", lambda: PgSaver(pool))
        return pool

    return install


@pytest.mark.parametrize("count", [0, 1, 3, 12])
def test_postgres_list_is_one_query_whatever_the_number_of_threads(client, required, signer, user, pg_path, count):
    rows = [
        _checkpoint_row(
            f"{user}:t{n}",
            1_700_000_000_000 + n,
            1_700_000_100_000 - n * 1000,  # n が小さいほど新しい（SQL が新しい順に並べて返す）
            [HumanMessage(f"質問 {n}"), AIMessage(f"答え {n}")],
        )
        for n in range(count)
    ]
    pool = pg_path(rows)

    res = client.get("/threads", headers=_bearer(signer, user))

    assert res.status_code == 200
    assert len(pool.statements) == 1  # 会話ごとの読み取り（aget_tuple）は走らない
    sql, params = pool.statements[0]
    assert sql == threads._LIST_SQL
    assert params == {"prefix": f"{user}:", "limit": threads.MAX_THREADS}
    items = res.json()["threads"]
    assert [item["thread_id"] for item in items] == [
        f"t{n}" for n in range(count)
    ]  # 接頭辞は付かない。SQL の並びのまま
    assert [item["title"] for item in items] == [f"質問 {n}" for n in range(count)]
    assert all(item["created_at"] and item["updated_at"] for item in items)
    assert set(items[0]) == {"thread_id", "title", "created_at", "updated_at"} if items else True


def test_postgres_title_is_the_first_user_message_of_the_latest_checkpoint(client, required, signer, user, pg_path):
    messages = [
        HumanMessage("最初の質問です"),
        AIMessage("答え 1"),
        HumanMessage("二つ目の質問です"),
        AIMessage("答え 2"),
    ]
    pg_path([_checkpoint_row(f"{user}:t1", 1_700_000_000_000, 1_700_000_050_000, messages)])

    items = client.get("/threads", headers=_bearer(signer, user)).json()["threads"]

    assert [item["title"] for item in items] == ["最初の質問です"]


def test_postgres_list_timestamps_match_the_memory_path_shape(client, required, signer, user, pg_path):
    pg_path([_checkpoint_row(f"{user}:t1", 1_700_000_000_000, 1_700_000_050_000, [HumanMessage("q")])])

    item = client.get("/threads", headers=_bearer(signer, user)).json()["threads"][0]

    assert item["created_at"] == "2023-11-14T22:13:20Z"
    assert item["updated_at"] == "2023-11-14T22:14:10Z"


def test_postgres_list_with_a_corrupt_checkpoint_is_a_fixed_503(client, required, signer, user, pg_path):
    secret = "postgresql://user:SECRET@db.internal/postgres"
    row = {
        "thread_id": f"{user}:t1",
        "created_at": 1_700_000_000_000,
        "updated_at": 1_700_000_050_000,
        "type": "msgpack",
        "checkpoint": secret.encode(),  # 復号できない
    }
    pool = pg_path([row])

    res = client.get("/threads", headers=_bearer(signer, user))

    assert res.status_code == 503
    assert res.json()["detail"] == "会話履歴を操作できませんでした。しばらく待ってから再試行してください。"
    assert "SECRET" not in res.text
    assert len(pool.statements) == 1
