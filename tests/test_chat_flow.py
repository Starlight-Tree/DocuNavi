"""既存の /chat 機能（正常系・RAG・マルチターン・入力検証・CORS）の回帰テスト。

実装詳細ではなく HTTP とグラフの状態から見える挙動だけを固定する。
thread_id はテストごとに uuid で分け、MemorySaver を共有しても干渉しないようにする。
"""

import os
import re
import uuid

import pytest
from langchain_core.messages import HumanMessage

from app.agents import tools
from app.agents.graph import MOCK_REPLY
from app.api import routes
from tests.zahard_corpus import zahard_texts

# ツール出力は "[ファイル名.pdf]\n本文" の繰り返し
_SOURCE_HEAD = re.compile(r"(?m)^\[([^\]\n]+\.pdf)\]$")
_NOT_FOUND_QUESTION = "xyzqwerty"


def _chat(client, message: str, thread_id: str | None = None):
    body: dict = {"message": message}
    if thread_id is not None:
        body["thread_id"] = thread_id
    return client.post("/chat", json=body)


def _state_values(thread_id: str) -> dict:
    """アプリが使っているモック用グラフから、スレッドの現在状態を取り出す。"""
    config = {"configurable": {"thread_id": thread_id}}
    return routes.get_graph(True).get_state(config).values


def _human_texts(thread_id: str) -> list[str]:
    messages = _state_values(thread_id).get("messages", [])
    return [str(m.content) for m in messages if isinstance(m, HumanMessage)]


def _sources(context: str) -> list[str]:
    return _SOURCE_HEAD.findall(context)


# --- 1. 正常系 ---------------------------------------------------------------


def test_chat_returns_expected_shape_in_mock_mode(client):
    thread_id = str(uuid.uuid4())
    res = _chat(client, "ザーハードの大気について教えて", thread_id)

    assert res.status_code == 200
    body = res.json()
    assert set(body) == {"reply", "thread_id", "context", "mode"}
    assert body["mode"] == "mock"
    assert body["thread_id"] == thread_id
    assert isinstance(body["reply"], str) and body["reply"].strip()
    assert isinstance(body["context"], str)


def test_mock_mode_replies_with_the_fixed_text_and_never_searches(client, monkeypatch):
    # モックモード（GEMINI_API_KEY が未設定）は、資料の検索も回答もしない固定の返答。DB も埋め込みの API も使わない
    async def must_not_run(*args, **kwargs):
        raise AssertionError("モックモードで資料を検索してはいけない")

    monkeypatch.setattr(tools, "search_chunks_async", must_not_run)
    for question in ("ザーハードの大気について教えて", _NOT_FOUND_QUESTION):
        body = _chat(client, question, str(uuid.uuid4())).json()
        assert body["reply"] == MOCK_REPLY
        assert body["context"] == ""
        assert "出典" not in body["reply"]


def test_chat_assigns_uuid_thread_id_when_omitted(client):
    first = _chat(client, "ザーハードの大気について教えて").json()["thread_id"]
    second = _chat(client, "ザーハードの大気について教えて").json()["thread_id"]

    # UUID として解釈でき、呼び出しごとに別の会話として採番される
    assert str(uuid.UUID(first)) == first
    assert str(uuid.UUID(second)) == second
    assert first != second


# --- 2. RAG ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("question", "expected_chunk"),
    [
        ("ザーハードの大気について教えて", "ザーハード1.pdf"),  # 大気・環境
        ("ザーハリアン文明とは何ですか", "ザーハード2.pdf"),  # 遺跡・異星人
        ("エネルギー源の正体は", "ザーハード3.pdf"),  # 未知のエネルギー源
    ],
)
def test_rag_returns_sources_from_chunks(client, searching_llm, question, expected_chunk):
    # 前提: 期待するチャンクが実在する（資料を差し替えたらここで気づける）
    assert expected_chunk in zahard_texts()

    body = _chat(client, question, str(uuid.uuid4())).json()

    assert body["context"].startswith("[ザーハード")
    names = _sources(body["context"])
    assert expected_chunk in names
    assert all(name in zahard_texts() for name in names)

    # 回答には根拠にした資料名が出典として載る
    assert "出典" in body["reply"]
    for name in names:
        assert name in body["reply"]


# --- 3. 該当なし -------------------------------------------------------------


def test_unrelated_question_has_no_source_citation(client, searching_llm):
    res = _chat(client, _NOT_FOUND_QUESTION, str(uuid.uuid4()))

    assert res.status_code == 200
    body = res.json()
    # 資料が引かれず、出典も付かない
    assert "[ザーハード" not in body["context"]
    assert "出典" not in body["reply"]
    assert body["reply"].strip()


# --- 4. マルチターン ---------------------------------------------------------


def test_same_thread_accumulates_history(client):
    thread_id = str(uuid.uuid4())
    first_q = "ザーハードの大気について教えて"
    second_q = "ザーハリアン文明とは何ですか"

    _chat(client, first_q, thread_id)
    after_first = len(_state_values(thread_id)["messages"])
    assert _human_texts(thread_id) == [first_q]

    _chat(client, second_q, thread_id)
    after_second = len(_state_values(thread_id)["messages"])

    # 2 ターン目でも 1 ターン目の履歴が残り、その後ろに追記される
    assert after_second > after_first
    assert _human_texts(thread_id) == [first_q, second_q]


def test_different_threads_have_isolated_history(client):
    thread_a = str(uuid.uuid4())
    thread_b = str(uuid.uuid4())

    _chat(client, "ザーハードの大気について教えて", thread_a)
    _chat(client, "ザーハリアン文明とは何ですか", thread_b)

    assert _human_texts(thread_a) == ["ザーハードの大気について教えて"]
    assert _human_texts(thread_b) == ["ザーハリアン文明とは何ですか"]


def test_context_is_replaced_every_turn(client, searching_llm):
    thread_id = str(uuid.uuid4())

    first = _chat(client, "ザーハリアン文明とは何ですか", thread_id).json()
    assert first["context"].startswith("[ザーハード")

    # 該当なしの 2 ターン目: 前ターンの検索結果が残らない
    second = _chat(client, _NOT_FOUND_QUESTION, thread_id).json()
    assert second["thread_id"] == thread_id
    assert "[ザーハード" not in second["context"]
    assert second["context"] != first["context"]
    assert _state_values(thread_id)["context"] == second["context"]

    # 3 ターン目で再び検索すると、その回の検索結果に置き換わる
    third = _chat(client, "エネルギー源の正体は", thread_id).json()
    assert "ザーハード3.pdf" in _sources(third["context"])
    assert third["context"] != first["context"]
    assert _state_values(thread_id)["context"] == third["context"]


# --- 5. 入力検証 -------------------------------------------------------------


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"thread_id": "abc"},
        {"message": ""},
        {"message": "", "thread_id": "abc"},
    ],
    ids=["empty-body", "message-missing", "message-empty", "message-empty-with-thread"],
)
def test_invalid_message_is_rejected_with_422(client, payload):
    res = client.post("/chat", json=payload)

    assert res.status_code == 422


# --- 6. CORS -----------------------------------------------------------------


def _allowed_origins() -> list[str]:
    # app.main と同じ解釈。既定は "*"、.env で CORS_ORIGINS が指定されていればそれに従う
    raw = os.environ.get("CORS_ORIGINS", "*")
    return [o.strip() for o in raw.split(",") if o.strip()]


def _origin_to_test() -> tuple[str, set[str]]:
    """(送る Origin, 許可ヘッダとして許容する値)。"""
    origins = _allowed_origins()
    if "*" in origins:
        return "http://example.com", {"*", "http://example.com"}
    return origins[0], {origins[0]}


def test_cors_header_is_added_for_allowed_origin(client):
    origin, accepted = _origin_to_test()

    res = client.post(
        "/chat",
        json={"message": "ザーハードの大気について教えて", "thread_id": str(uuid.uuid4())},
        headers={"Origin": origin},
    )

    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") in accepted


def test_cors_preflight_is_accepted_for_allowed_origin(client):
    origin, accepted = _origin_to_test()

    res = client.options(
        "/chat",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )

    assert res.status_code == 200
    assert res.headers.get("access-control-allow-origin") in accepted


def test_cors_header_is_not_added_without_origin(client):
    res = client.get("/health")

    assert res.status_code == 200
    assert "access-control-allow-origin" not in res.headers
