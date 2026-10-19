"""tests/real_api_faults.py の確認。実 API は呼ばない（差し替えた 503 は、ネットワークに出る前に返る）。

確かめること: 差し替えた 503 が、本物の 503 と同じ経路（google-genai の ServerError → resilience.classify_failure）を
通ること。主モデル以外の要求は差し替えず、そのまま元の送信に渡すこと。
"""

import asyncio
from collections.abc import Awaitable, Callable

import httpx
import pytest
from google import genai
from google.genai import errors

from app.agents.resilience import Failure, classify_failure
from tests.real_api_faults import GEMINI_HOST, SERVER_503_BODY, FaultInjector, model_of

PRIMARY = "gemini-3.1-flash-lite"
FALLBACK = "gemini-3.5-flash-lite"


def _run_with_injector(
    monkeypatch: pytest.MonkeyPatch,
    original: Callable[..., Awaitable[httpx.Response]],
    body: Callable[[FaultInjector], Awaitable[None]],
) -> FaultInjector:
    """本物の送信を `original` に置き換えたうえで、差し込みを入れて body を実行し、必ず元に戻す。"""
    monkeypatch.setattr(httpx.AsyncClient, "send", original)
    injector = FaultInjector(PRIMARY)
    injector.install()
    try:
        asyncio.run(body(injector))
    finally:
        injector.uninstall()
    return injector


def test_model_of_reads_the_model_only_for_gemini_api_requests():
    ok = httpx.URL(f"https://{GEMINI_HOST}/v1beta/models/{PRIMARY}:streamGenerateContent?alt=sse")
    assert model_of(ok) == PRIMARY
    assert model_of(httpx.URL(f"https://example.invalid/v1beta/models/{PRIMARY}:generateContent")) is None
    assert model_of(httpx.URL(f"https://{GEMINI_HOST}/v1beta/files")) is None


def test_faked_503_goes_through_the_real_sdk_error_path(monkeypatch):
    async def forbidden(*_a, **_k):
        raise AssertionError("差し替えられず、本物の送信に進んだ（このテストは実 API を呼ばない）")

    caught: list[errors.ServerError] = []

    async def body(injector: FaultInjector) -> None:
        injector.primary_fail = True
        client = genai.Client(api_key="dummy-key-not-a-real-key")
        with pytest.raises(errors.ServerError) as excinfo:
            await client.aio.models.generate_content(model=PRIMARY, contents="hello")
        caught.append(excinfo.value)

    injector = _run_with_injector(monkeypatch, forbidden, body)

    error = caught[0]
    assert error.code == 503
    assert error.status == "UNAVAILABLE"
    assert error.message == SERVER_503_BODY["error"]["message"]
    # アプリの判定: 一時的な失敗（投げ直す・フォールバックに回す対象）
    assert classify_failure(error) is Failure.RETRY
    assert [(c["model"], c["faked"], c["status"]) for c in injector.calls] == [(PRIMARY, True, 503)]


def test_requests_to_other_models_and_other_hosts_pass_through(monkeypatch):
    async def original(_client, request, *_a, **_k):
        return httpx.Response(200, json={}, request=request)

    statuses: list[int] = []

    async def body(injector: FaultInjector) -> None:
        injector.primary_fail = True
        async with httpx.AsyncClient() as client:
            fallback = await client.send(
                httpx.Request("POST", f"https://{GEMINI_HOST}/v1beta/models/{FALLBACK}:streamGenerateContent")
            )
            other = await client.send(httpx.Request("GET", "https://example.invalid/x"))
        statuses.extend([fallback.status_code, other.status_code])

    injector = _run_with_injector(monkeypatch, original, body)

    assert statuses == [200, 200]
    # 記録に残るのは Gemini への要求だけ。フォールバックは差し替えない
    assert [(c["model"], c["faked"], c["status"]) for c in injector.calls] == [(FALLBACK, False, 200)]


def test_primary_is_real_again_after_switching_off(monkeypatch):
    async def original(_client, request, *_a, **_k):
        return httpx.Response(200, json={}, request=request)

    statuses: list[int] = []

    async def body(injector: FaultInjector) -> None:
        request = httpx.Request("POST", f"https://{GEMINI_HOST}/v1beta/models/{PRIMARY}:streamGenerateContent")
        async with httpx.AsyncClient() as client:
            injector.primary_fail = True
            statuses.append((await client.send(request)).status_code)
            injector.primary_fail = False
            statuses.append((await client.send(request)).status_code)

    _run_with_injector(monkeypatch, original, body)
    assert statuses == [503, 200]


def test_uninstall_restores_the_original_send():
    before = httpx.AsyncClient.send
    injector = FaultInjector(PRIMARY)
    injector.install()
    assert httpx.AsyncClient.send is not before
    injector.uninstall()
    assert httpx.AsyncClient.send is before


def test_app_falls_back_to_the_other_model_when_the_primary_is_faked_to_503(monkeypatch):
    """アプリ全体（/chat/stream）で、差し込んだ 503 がフォールバックを発火させる。本物の Gemini は呼ばない。

    フォールバック先の応答は、本物の API の形（streamGenerateContent の SSE）の作り物。これで、実 API 検証の「失敗の差し込み →
    フォールバック」の配線が、課金なしで確かめられる（フォールバック先が本物のときの互換性は、ここでは確かめられない）。
    """
    from fastapi.testclient import TestClient

    from app.api import routes
    from app.main import app

    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key-not-a-real-key")
    monkeypatch.setenv("GEMINI_MODEL", PRIMARY)
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", FALLBACK)
    monkeypatch.setenv("GEMINI_FALLBACK_HOLD_SECONDS", "0")
    for name in ("LLM_HEDGE_DELAY_SECONDS", "LLM_ATTEMPT_TIMEOUT_SECONDS", "LLM_MAX_ATTEMPTS", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)

    reply = "フォールバックのモデルの返答です。"
    chunk = {
        "candidates": [{"content": {"role": "model", "parts": [{"text": reply}]}, "finishReason": "STOP", "index": 0}],
        "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
        "modelVersion": FALLBACK,
    }
    import json

    sse = f"data: {json.dumps(chunk, ensure_ascii=False)}\r\n\r\n".encode()

    async def canned(_client, request, *_a, **_k):
        model = model_of(request.url)
        assert model == FALLBACK, f"主モデルの要求が差し替えられずに届いた: {model}"
        return httpx.Response(200, content=sse, headers={"content-type": "text/event-stream"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", canned)
    injector = FaultInjector(PRIMARY)
    injector.install()
    injector.primary_fail = True
    routes._graph_for.cache_clear()
    try:
        response = TestClient(app).post("/chat/stream", json={"message": "ザーハードの大気について教えて"})
    finally:
        injector.uninstall()
        routes._graph_for.cache_clear()

    assert response.status_code == 200
    assert reply in response.text
    assert "error" not in response.text.replace("errorText", "")
    primary_calls = [c for c in injector.calls if c["model"] == PRIMARY]
    fallback_calls = [c for c in injector.calls if c["model"] == FALLBACK]
    assert primary_calls and all(c["faked"] and c["status"] == 503 for c in primary_calls)
    assert len(fallback_calls) == 1 and fallback_calls[0]["status"] == 200 and not fallback_calls[0]["faked"]


def _tool_then_answer_sse(model: str, query: str, answer: str):
    """Gemini の streamGenerateContent の応答の作り物。1 回目は search_documents の呼び出し、2 回目は本文の回答。"""
    import json

    def sse(parts):
        chunk = {
            "candidates": [{"content": {"role": "model", "parts": parts}, "finishReason": "STOP", "index": 0}],
            "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5, "totalTokenCount": 15},
            "modelVersion": model,
        }
        return f"data: {json.dumps(chunk, ensure_ascii=False)}\r\n\r\n".encode()

    return [
        sse([{"functionCall": {"name": "search_documents", "args": {"query": query}}}]),
        sse([{"text": answer}]),
    ]


def _stream_final_metadata(monkeypatch, primary_fails: bool, query: str, search=None) -> dict:
    """モデルが `query` で検索ツールを呼ぶ質問を /chat/stream に流し、最後の message-metadata を返す（実 API なし）。
    primary_fails なら主モデルを 503 にして、フォールバックが答える。`search` を渡すと、偽の検索をそれに差し替える。"""
    import json

    from fastapi.testclient import TestClient

    from app.agents import tools
    from app.api import routes
    from app.main import app

    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key-not-a-real-key")
    monkeypatch.setenv("GEMINI_MODEL", PRIMARY)
    monkeypatch.setenv("GEMINI_FALLBACK_MODEL", FALLBACK)
    monkeypatch.setenv("GEMINI_FALLBACK_HOLD_SECONDS", "0")
    for name in ("LLM_HEDGE_DELAY_SECONDS", "LLM_ATTEMPT_TIMEOUT_SECONDS", "LLM_MAX_ATTEMPTS", "LLM_TIMEOUT_SECONDS"):
        monkeypatch.delenv(name, raising=False)
    if search is not None:
        monkeypatch.setattr(tools, "search_chunks_async", search)

    answering = FALLBACK if primary_fails else PRIMARY
    responses = _tool_then_answer_sse(answering, query, "資料によれば、大気は薄いです。")

    async def canned(_client, request, *_a, **_k):
        model = model_of(request.url)
        assert model == answering, f"想定外のモデルの要求: {model}"
        body = responses.pop(0)
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"}, request=request)

    monkeypatch.setattr(httpx.AsyncClient, "send", canned)
    injector = FaultInjector(PRIMARY)
    injector.install()
    injector.primary_fail = primary_fails
    routes._graph_for.cache_clear()
    try:
        response = TestClient(app).post("/chat/stream", json={"message": "ザーハードの大気について教えて"})
    finally:
        injector.uninstall()
        routes._graph_for.cache_clear()

    assert response.status_code == 200
    chunks = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: {")]
    metadata = [c["messageMetadata"] for c in chunks if c.get("type") == "message-metadata"]
    assert metadata, "message-metadata チャンクが無い"
    return metadata[-1]


@pytest.mark.parametrize("primary_fails", [False, True], ids=["primary", "fallback"])
def test_sources_reach_the_stream_with_and_without_fallback(monkeypatch, primary_fails):
    """検索のツール呼び出しを含む質問で、出典（message-metadata の sources）が、主モデルでもフォールバックでも届く。"""
    metadata = _stream_final_metadata(monkeypatch, primary_fails, "ザーハード 大気")
    assert metadata["sources"], f"出典が空（primary_fails={primary_fails}）: {metadata}"


@pytest.mark.parametrize("primary_fails", [False, True], ids=["primary", "fallback"])
def test_sources_are_empty_when_the_search_query_finds_nothing(monkeypatch, primary_fails):
    """検索語が偽の検索のどのキーワードにも当たらないと、ツールが呼ばれても sources は空で、context は「見つからなかった」になる。
    出典が空になる正常な場合（主モデルでもフォールバックでも同じ）。実 API 検証で出典が付かなかった回の、原因の一つ。"""
    metadata = _stream_final_metadata(monkeypatch, primary_fails, "Zahard atmosphere")
    assert metadata["sources"] == []
    assert "見つかりませんでした" in metadata["context"]


def test_recording_search_keeps_only_a_short_head_and_a_hash_of_the_query():
    """RecordingSearch は、検索語の全文を残さず、先頭・文字数・ハッシュ・ヒット数だけを記録する。"""
    import asyncio

    from tests.fakes import FakeSearch
    from tests.real_api_faults import QUERY_HEAD_CHARS, RecordingSearch

    recording = RecordingSearch(FakeSearch())
    long_query = "ザーハードの大気" + "あ" * 100
    asyncio.run(recording("ザーハード atmosphere"))
    hits = asyncio.run(recording(long_query))

    assert [h.source for h in hits] == ["ザーハード1.pdf"]  # 結果は包んだ検索のまま
    assert [(s["seq"], s["hits"]) for s in recording.searches] == [(1, 0), (2, 1)]
    second = recording.searches[1]
    assert second["query_head"] == long_query[:QUERY_HEAD_CHARS]
    assert second["query_chars"] == len(long_query)
    assert len(second["query_sha256"]) == 8
    assert long_query not in str(recording.searches)
    recording.reset()
    assert recording.searches == []


def test_failed_responses_record_only_diagnostic_fields(monkeypatch):
    """失敗した本物の応答は、エラーのコード・状態と切り分け用のヘッダーだけを記録し、キー・他のヘッダー・本文は残さない。"""
    secret = "secret-header-value-must-not-be-recorded"
    message = "message text must not be recorded"

    async def original(_client, request, *_a, **_k):
        model = model_of(request.url)
        if model == FALLBACK:  # 成功した応答
            return httpx.Response(200, json={}, headers={"retry-after": "9"}, request=request)
        return httpx.Response(
            429,
            json={"error": {"code": 429, "message": message, "status": "RESOURCE_EXHAUSTED"}},
            headers={
                "retry-after": "7",
                "x-ratelimit-remaining-requests": "0",
                "x-goog-api-key": secret,
                "set-cookie": secret,
                "content-type": "application/json",
            },
            request=request,
        )

    async def body(_injector: FaultInjector) -> None:
        async with httpx.AsyncClient() as client:
            await client.send(httpx.Request("POST", f"https://{GEMINI_HOST}/v1beta/models/{PRIMARY}:generateContent"))
            await client.send(httpx.Request("POST", f"https://{GEMINI_HOST}/v1beta/models/{FALLBACK}:generateContent"))

    injector = _run_with_injector(monkeypatch, original, body)
    failed, ok = injector.calls

    assert failed["status"] == 429
    assert failed["error_code"] == 429 and failed["error_status"] == "RESOURCE_EXHAUSTED"
    assert failed["headers"] == {"retry-after": "7", "x-ratelimit-remaining-requests": "0"}
    # 成功した応答には、エラーの項目もヘッダーも付かない（既存の形のまま）
    assert set(ok) == {"seq", "model", "endpoint", "faked", "status", "seconds_to_response"}
    assert secret not in repr(injector.calls) and message not in repr(injector.calls)


def test_faked_503_records_error_code_and_status_without_headers(monkeypatch):
    async def forbidden(*_a, **_k):
        raise AssertionError("差し替えられず、本物の送信に進んだ")

    async def body(_injector: FaultInjector) -> None:
        _injector.primary_fail = True
        async with httpx.AsyncClient() as client:
            await client.send(httpx.Request("POST", f"https://{GEMINI_HOST}/v1beta/models/{PRIMARY}:generateContent"))

    injector = _run_with_injector(monkeypatch, forbidden, body)
    call = injector.calls[0]
    assert call["faked"] is True and call["error_code"] == 503 and call["error_status"] == "UNAVAILABLE"
    assert "headers" not in call
    assert SERVER_503_BODY["error"]["message"] not in repr(injector.calls)
