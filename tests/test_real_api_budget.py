"""tests/real_api_budget.py の確認。実 API は呼ばない（httpx の MockTransport。ネットワークには出ない）。

確かめること: Gemini への要求だけを 1 回ずつ数える（非同期・同期の両方、再試行も別の 1 回）／上限（20 回）に達したら、
送信前に止める（ネットワークに出ない）／偽の 503 は数えない／質問は 10 件まで／実行全体の時間の上限／環境変数は下げられるが
天井は超えられない／目的が無いと起動しない／概算費用を起動時に示す。
"""

import asyncio

import httpx
import pytest

from tests.real_api_budget import (
    CALLS_CEILING,
    QUESTIONS_CEILING,
    SECONDS_CEILING,
    BudgetExceeded,
    RealApiBudget,
    banner,
    estimated_max_cost_usd,
    require_purpose,
)
from tests.real_api_faults import GEMINI_HOST, FaultInjector

PRIMARY = "gemini-3.1-flash-lite"
FALLBACK = "gemini-3.5-flash-lite"
GEMINI_URL = f"https://{GEMINI_HOST}/v1beta/models/{PRIMARY}:generateContent"
OTHER_URL = "https://example.invalid/health"


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch):
    """送信の先を、ネットワークに出ない偽物にする。届いた要求の数を返す。"""
    sent: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(str(request.url.host))
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    real_async_init, real_sync_init = httpx.AsyncClient.__init__, httpx.Client.__init__

    def async_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        kwargs["transport"] = transport
        real_async_init(self, *args, **kwargs)

    def sync_init(self, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        kwargs["transport"] = transport
        real_sync_init(self, *args, **kwargs)

    monkeypatch.setattr(httpx.AsyncClient, "__init__", async_init)
    monkeypatch.setattr(httpx.Client, "__init__", sync_init)
    return sent


async def _post(url: str) -> int:
    async with httpx.AsyncClient() as client:
        return (await client.post(url, json={})).status_code


def _installed(budget: RealApiBudget):
    budget.install()
    return budget


def test_counts_only_gemini_requests_and_stops_before_the_network_at_the_cap(network):
    budget = _installed(RealApiBudget(max_calls=20))
    try:
        for _ in range(20):
            assert asyncio.run(_post(GEMINI_URL)) == 200
        assert asyncio.run(_post(OTHER_URL)) == 200  # Gemini 以外は数えない
        assert budget.calls == 20
        assert len(network) == 21
        with pytest.raises(BudgetExceeded):
            asyncio.run(_post(GEMINI_URL))  # 21 回目は、送信前に止まる
        assert len(network) == 21  # ネットワークには出ていない
        assert budget.refused_calls == 1
        assert budget.snapshot()["stopped_reason"]
    finally:
        budget.uninstall()


def test_sync_client_requests_are_counted_too(network):
    budget = _installed(RealApiBudget(max_calls=2))
    try:
        with httpx.Client() as client:
            client.post(GEMINI_URL, json={})
            client.post(GEMINI_URL, json={})
            with pytest.raises(BudgetExceeded):
                client.post(GEMINI_URL, json={})
        assert budget.calls == 2
        assert len(network) == 2
    finally:
        budget.uninstall()


def test_retries_and_embeddings_count_as_calls_like_any_other_request(network):
    budget = _installed(RealApiBudget(max_calls=3))
    embedding = f"https://{GEMINI_HOST}/v1beta/models/gemini-embedding-2:embedContent"
    try:
        asyncio.run(_post(GEMINI_URL))  # LLM の回答
        asyncio.run(_post(embedding))  # 質問の埋め込み
        asyncio.run(_post(GEMINI_URL))  # 再試行（429 などの後の 2 回目）
        with pytest.raises(BudgetExceeded):
            asyncio.run(_post(embedding))
        assert budget.calls == 3
    finally:
        budget.uninstall()


def test_faked_503_does_not_reach_the_network_and_is_not_counted(network):
    budget = _installed(RealApiBudget(max_calls=20))
    injector = FaultInjector(PRIMARY)
    injector.install()  # 予算より後に入れる（偽の 503 は、予算に届く前に返る）
    injector.primary_fail = True
    try:
        assert asyncio.run(_post(GEMINI_URL)) == 503
        assert budget.calls == 0
        assert network == []
        fallback_url = f"https://{GEMINI_HOST}/v1beta/models/{FALLBACK}:generateContent"
        assert asyncio.run(_post(fallback_url)) == 200  # フォールバック先は本物（数える）
        assert budget.calls == 1
    finally:
        injector.uninstall()
        budget.uninstall()


def test_questions_stop_at_the_cap_and_when_the_calls_are_spent():
    budget = RealApiBudget(max_calls=20, max_questions=10)
    for _ in range(10):
        assert budget.take_question() is None
    reason = budget.take_question()
    assert reason and "質問" in reason
    assert budget.questions == 10 and budget.refused_questions == 1

    spent = RealApiBudget(max_calls=2, max_questions=10)
    spent.calls = 2
    assert spent.take_question() is not None  # 呼び出しを使い切ったら、質問も受け付けない
    assert spent.questions == 0


def test_run_deadline_stops_questions_and_calls():
    now = [0.0]
    budget = RealApiBudget(max_seconds=300, clock=lambda: now[0])
    budget.start()
    now[0] = 299.0
    assert budget.take_question() is None
    now[0] = 301.0
    assert budget.take_question() is not None
    with pytest.raises(BudgetExceeded):
        budget.count_call()
    assert "300" in (budget.stopped_reason or "")


def test_env_can_lower_the_limits_but_not_raise_them_above_the_ceilings():
    lowered = RealApiBudget.from_env(
        {"REAL_API_MAX_CALLS": "5", "REAL_API_MAX_QUESTIONS": "2", "REAL_API_MAX_SECONDS": "60"}
    )
    assert (lowered.max_calls, lowered.max_questions, lowered.max_seconds) == (5, 2, 60)
    raised = RealApiBudget.from_env(
        {"REAL_API_MAX_CALLS": "999", "REAL_API_MAX_QUESTIONS": "999", "REAL_API_MAX_SECONDS": "99999"}
    )
    assert (raised.max_calls, raised.max_questions, raised.max_seconds) == (
        CALLS_CEILING,
        QUESTIONS_CEILING,
        SECONDS_CEILING,
    )
    default = RealApiBudget.from_env({"REAL_API_MAX_CALLS": "abc", "REAL_API_MAX_QUESTIONS": "0"})
    assert (default.max_calls, default.max_questions) == (20, 10)
    assert RealApiBudget.from_env({}).max_seconds == 300


def test_purpose_is_required_and_the_banner_shows_the_budget_and_an_estimated_cost():
    with pytest.raises(SystemExit) as exc:
        require_purpose({})
    assert "REAL_API_PURPOSE" in str(exc.value)
    with pytest.raises(SystemExit):
        require_purpose({"REAL_API_PURPOSE": "   "})
    purpose = require_purpose({"REAL_API_PURPOSE": "フォールバックの確認"})
    budget = RealApiBudget()
    text = banner(purpose, budget, (PRIMARY, FALLBACK))
    assert "フォールバックの確認" in text
    assert "20 回" in text and "10 件" in text and "300 秒" in text
    assert PRIMARY in text and FALLBACK in text
    cost = estimated_max_cost_usd(budget, (PRIMARY, FALLBACK))
    assert 0 < cost < 0.5  # 20 回 × 高い方のモデルの料金（有料枠の上限の見積もり）
    assert f"${cost:.2f}" in text
    assert "製品の改善" in text  # 無料枠でも安全とは言わない
