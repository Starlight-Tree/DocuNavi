import asyncio
import logging

import httpx
import pytest
from fastapi.testclient import TestClient
from google.genai.errors import ClientError, ServerError
from langchain_core.exceptions import (
    ModelAuthenticationError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.messages import AIMessage

from app.api import routes
from app.main import app

# 例外メッセージに混ぜて、レスポンスへ漏れていないかを確認するための文字列
SECRET = "SECRET-KEY-abc123 internal-host.local"


class FakeGraph:
    """get_graph の代わりに差し込む偽グラフ。exc があれば送出し、無ければ固定の返答を返す。"""

    def __init__(self, exc: BaseException | None = None, delay: float = 0.0, result=None):
        self.exc = exc
        self.delay = delay
        self.result = result
        self.calls: list[dict] = []

    async def ainvoke(self, state, config):
        self.calls.append({"state": state, "config": config})
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.exc is not None:
            raise self.exc
        if self.result is not None:
            return self.result
        return {"messages": [AIMessage(content="ok")], "context": "ctx"}


@pytest.fixture
def use_graph(monkeypatch):
    """routes.get_graph を偽グラフへ差し替える（lru_cache 済みの本物には触れない）。"""
    monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)

    def _use(graph: FakeGraph) -> FakeGraph:
        monkeypatch.setattr(routes, "get_graph", lambda mock: graph)
        return graph

    return _use


def _client_error(code: int, status: str, *, details=None, response=None) -> ClientError:
    body = {"error": {"code": code, "message": SECRET, "status": status}}
    if details is not None:
        body["error"]["details"] = details
    return ClientError(code, body, response)


def _google_wrapped(name: str, cause: BaseException) -> BaseException:
    """langchain-google-genai が ClientError を包み直す形を再現する。"""
    chat_models = pytest.importorskip("langchain_google_genai.chat_models")
    wrapper = getattr(chat_models, name)(f"Error calling model ({SECRET})")
    wrapper.__cause__ = cause
    return wrapper


RETRY_INFO = [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "37.2s"}]

# (ID, 例外を作る関数, 期待ステータス)
ERROR_CASES = [
    ("quota-genai", lambda: _client_error(429, "RESOURCE_EXHAUSTED"), 429),
    (
        "quota-langchain-wrapped",
        lambda: _google_wrapped("GoogleRateLimitError", _client_error(429, "RESOURCE_EXHAUSTED")),
        429,
    ),
    ("quota-model-error", lambda: ModelRateLimitError(SECRET), 429),
    ("timeout-builtin", lambda: TimeoutError(SECRET), 504),
    ("timeout-asyncio", lambda: asyncio.TimeoutError(SECRET), 504),
    ("timeout-httpx", lambda: httpx.ReadTimeout(SECRET), 504),
    ("timeout-model-error", lambda: ModelTimeoutError(SECRET), 504),
    ("timeout-server-504", lambda: ServerError(504, {"error": {"message": SECRET}}), 504),
    ("auth-model-error", lambda: ModelAuthenticationError(SECRET), 500),
    (
        "auth-langchain-wrapped",
        lambda: _google_wrapped("GoogleAuthenticationError", _client_error(401, "UNAUTHENTICATED")),
        500,
    ),
    ("auth-403", lambda: _client_error(403, "PERMISSION_DENIED"), 500),
    (
        "auth-invalid-key-400",
        lambda: _client_error(
            400,
            "INVALID_ARGUMENT",
            details=[{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}],
        ),
        500,
    ),
    ("other-runtime", lambda: RuntimeError(SECRET), 502),
    ("other-value", lambda: ValueError(SECRET), 502),
    ("other-server-503", lambda: ServerError(503, {"error": {"message": SECRET}}), 502),
    ("other-bad-request-400", lambda: _client_error(400, "INVALID_ARGUMENT"), 502),
]


@pytest.mark.parametrize("make_exc,expected", [pytest.param(m, s, id=i) for i, m, s in ERROR_CASES])
def test_llm_errors_map_to_status_without_leaking(client, use_graph, make_exc, expected):
    use_graph(FakeGraph(exc=make_exc()))

    res = client.post("/chat", json={"message": "こんにちは"})

    assert res.status_code == expected
    assert isinstance(res.json()["detail"], str) and res.json()["detail"]
    assert "SECRET" not in res.text
    assert "internal-host" not in res.text
    assert "Traceback" not in res.text


def test_error_detail_is_fixed_per_kind(client, use_graph):
    # 種別が同じなら、元の例外が何であっても detail は同一の固定文
    details = set()
    for exc in (RuntimeError("a"), ValueError("b"), ServerError(500, {"error": {"message": "c"}})):
        use_graph(FakeGraph(exc=exc))
        details.add(client.post("/chat", json={"message": "x"}).json()["detail"])
    assert len(details) == 1


def test_rate_limit_uses_retry_info_for_retry_after(client, use_graph):
    exc = _client_error(429, "RESOURCE_EXHAUSTED", details=RETRY_INFO)
    use_graph(FakeGraph(exc=_google_wrapped("GoogleRateLimitError", exc)))

    res = client.post("/chat", json={"message": "x"})

    assert res.status_code == 429
    assert res.headers["Retry-After"] == "38"  # 37.2 秒を切り上げ


def test_rate_limit_prefers_retry_after_header(client, use_graph):
    response = httpx.Response(429, headers={"Retry-After": "12"})
    use_graph(FakeGraph(exc=_client_error(429, "RESOURCE_EXHAUSTED", details=RETRY_INFO, response=response)))

    res = client.post("/chat", json={"message": "x"})

    assert res.status_code == 429
    assert res.headers["Retry-After"] == "12"


def test_rate_limit_without_hint_has_no_retry_after(client, use_graph):
    use_graph(FakeGraph(exc=ModelRateLimitError(SECRET)))

    res = client.post("/chat", json={"message": "x"})

    assert res.status_code == 429
    assert "Retry-After" not in res.headers


def test_slow_llm_times_out_with_env_setting(client, use_graph, monkeypatch):
    monkeypatch.setenv("LLM_TIMEOUT_SECONDS", "0.05")
    use_graph(FakeGraph(delay=5))

    res = client.post("/chat", json={"message": "x"})

    assert res.status_code == 504


@pytest.mark.parametrize(
    "value,expected",
    [
        (None, 60.0),
        ("", 60.0),
        ("abc", 60.0),
        ("0", 60.0),
        ("-3", 60.0),
        ("nan", 60.0),
        ("inf", 60.0),
        ("5", 5.0),
        ("0.5", 0.5),
    ],
)
def test_llm_timeout_seconds_parsing(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("LLM_TIMEOUT_SECONDS", raising=False)
    else:
        monkeypatch.setenv("LLM_TIMEOUT_SECONDS", value)
    assert routes.llm_timeout_seconds() == expected


def test_failure_is_logged_with_traceback(client, use_graph, caplog):
    use_graph(FakeGraph(exc=RuntimeError(SECRET)))

    with caplog.at_level(logging.ERROR, logger="app.api.routes"):
        client.post("/chat", json={"message": "x", "thread_id": "t-1"})

    record = next(r for r in caplog.records if r.name == "app.api.routes")
    assert record.exc_info is not None  # 詳細はログ側に残る
    assert "t-1" in record.getMessage()


def test_success_passes_stripped_message_and_keeps_response_shape(client, use_graph):
    graph = use_graph(FakeGraph())

    res = client.post("/chat", json={"message": "  ザーハードとは？ \n", "thread_id": "abc-1"})

    assert res.status_code == 200
    assert res.json() == {
        "reply": "ok",
        "thread_id": "abc-1",
        "context": "ctx",
        "mode": "mock",
        "collection": None,
        "hit_count": None,
    }
    assert graph.calls[0]["state"]["messages"][0].content == "ザーハードとは？"
    assert graph.calls[0]["config"] == {"configurable": {"thread_id": "abc-1"}}


# ---- 入力検証 ----


@pytest.mark.parametrize("message", ["", " ", "   ", "\n\t 　"])  # 　 は全角空白
def test_blank_message_is_422(client, use_graph, message):
    graph = use_graph(FakeGraph())
    res = client.post("/chat", json={"message": message})
    assert res.status_code == 422
    assert graph.calls == []


def test_missing_message_is_422(client, use_graph):
    use_graph(FakeGraph())
    assert client.post("/chat", json={}).status_code == 422


def test_message_length_boundary(client, use_graph):
    use_graph(FakeGraph())
    assert routes.MAX_MESSAGE_LENGTH == 2000

    assert client.post("/chat", json={"message": "あ" * 2000}).status_code == 200
    assert client.post("/chat", json={"message": "あ" * 2001}).status_code == 422


def test_message_length_is_checked_after_strip(client, use_graph):
    use_graph(FakeGraph())
    padded = " " * 10 + "a" * 2000 + " " * 10
    assert client.post("/chat", json={"message": padded}).status_code == 200


@pytest.mark.parametrize(
    "thread_id",
    ["abc", "ABC-def_123", "0", "a" * 128],
)
def test_valid_thread_id(client, use_graph, thread_id):
    use_graph(FakeGraph())
    res = client.post("/chat", json={"message": "x", "thread_id": thread_id})
    assert res.status_code == 200
    assert res.json()["thread_id"] == thread_id


@pytest.mark.parametrize(
    "thread_id",
    ["a" * 129, "has space", "a/b", "a.b", "日本語", "a\n", "a;b", " ", "<script>"],
)
def test_invalid_thread_id_is_422(client, use_graph, thread_id):
    graph = use_graph(FakeGraph())
    res = client.post("/chat", json={"message": "x", "thread_id": thread_id})
    assert res.status_code == 422
    assert graph.calls == []


@pytest.mark.parametrize(
    "body", [{"message": "x"}, {"message": "x", "thread_id": None}, {"message": "x", "thread_id": ""}]
)
def test_missing_or_empty_thread_id_starts_new_thread(client, use_graph, body):
    use_graph(FakeGraph())
    res = client.post("/chat", json=body)
    assert res.status_code == 200
    assert len(res.json()["thread_id"]) == 36  # uuid4


# ---- 想定外の例外（グローバルハンドラ） ----


def test_unexpected_exception_returns_generic_json(use_graph):
    # result に messages が無いと try ブロックの外で KeyError になり、グローバルハンドラへ抜ける
    use_graph(FakeGraph(result={"context": SECRET}))
    # raise_server_exceptions=False で、Starlette が再送出した例外を握りつぶさず実際の応答を見る
    with TestClient(app, raise_server_exceptions=False) as c:
        res = c.post("/chat", json={"message": "x"})

    assert res.status_code == 500
    assert res.headers["content-type"].startswith("application/json")
    assert set(res.json()) == {"detail"}
    assert "messages" not in res.text and "KeyError" not in res.text and "SECRET" not in res.text


# ---- 防御的な import ----


def test_optional_classes_skips_missing_things():
    found = routes._optional_classes(
        ("no_such_module_xyz", "Foo"),
        ("langchain_core.exceptions", "NoSuchError"),
        ("os", "getcwd"),  # 例外クラスではない
        ("builtins", "ValueError"),
    )
    assert found == (ValueError,)
