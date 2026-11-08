"""trace_check の一括取得（evals/golden/trace_check.py の RunsQuery・fetch_traces）。LangSmith には接続しない（httpx の偽の転送）。"""

import datetime as dt
import json
from typing import Any

import httpx
import pytest

from evals.golden import trace_check
from evals.golden.trace_check import RunsQuery, fetch_traces, retry_delay

SECRET = "ls-secret-key-value"


def make(handler, **kw: Any) -> tuple[RunsQuery, list[float]]:
    slept: list[float] = []
    client = RunsQuery(
        "https://example.invalid",
        SECRET,
        transport=httpx.MockTransport(handler),
        sleep=slept.append,
        rand=lambda: 0.0,
        **kw,
    )
    return client, slept


def page(items: list[dict], cursor: str | None = None) -> httpx.Response:
    return httpx.Response(200, json={"items": items, "next_cursor": cursor})


def test_query_follows_the_cursor_to_the_last_page_and_selects_only_what_it_needs() -> None:
    seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v2/runs/query" and request.headers["x-api-key"] == SECRET
        body = json.loads(request.content)
        seen.append(body)
        return {
            None: page([{"id": "1"}, {"id": "2"}], "c1"),
            "c1": page([{"id": "3"}], "c2"),
            "c2": page([{"id": "4"}]),
        }[body.get("cursor")]

    client, _ = make(handler)
    assert [r["id"] for r in client.query({"project_ids": ["p"]})] == ["1", "2", "3", "4"]
    assert [b.get("cursor") for b in seen] == [None, "c1", "c2"] and client.requests == 3
    assert seen[0]["page_size"] == 100 and seen[0]["project_ids"] == ["p"]
    assert {
        "ID",
        "NAME",
        "RUN_TYPE",
        "START_TIME",
        "END_TIME",
        "TRACE_ID",
        "TOTAL_TOKENS",
        "TOTAL_COST",
        "FIRST_TOKEN_TIME",
    } <= set(seen[0]["selects"])


def test_a_429_honours_retry_after_then_succeeds() -> None:
    answers = iter([httpx.Response(429, headers={"retry-after": "7"}), page([{"id": "1"}])])
    client, slept = make(lambda request: next(answers))
    assert [r["id"] for r in client.query({})] == ["1"]
    assert len(slept) == 1 and 7.0 <= slept[0] <= 7.1 and client.retries == 1


def test_a_429_without_retry_after_backs_off_exponentially_with_jitter() -> None:
    assert retry_delay(0, None, lambda: 0.0) == 0.5 and retry_delay(0, None, lambda: 1.0) == 1.0
    assert retry_delay(3, None, lambda: 1.0) == 8.0 and retry_delay(3, None, lambda: 0.0) == 4.0
    assert retry_delay(20, None, lambda: 1.0) == trace_check.BACKOFF_CAP_S  # 上限
    answers = iter([httpx.Response(429), httpx.Response(429), httpx.Response(429), page([])])
    client, slept = make(lambda request: next(answers))
    list(client.query({}))
    assert slept == [0.5, 1.0, 2.0]  # rand=0 なので、下限（半分）の 1, 2, 4 秒の半分


def test_retry_after_can_be_an_http_date_and_never_exceeds_the_cap() -> None:
    soon = (dt.datetime.now(dt.UTC) + dt.timedelta(seconds=30)).strftime("%a, %d %b %Y %H:%M:%S GMT")
    assert 25 <= retry_delay(0, soon, lambda: 0.0) <= 31
    assert retry_delay(0, "9999", lambda: 0.0) == trace_check.BACKOFF_CAP_S
    assert retry_delay(0, "not-a-date", lambda: 0.0) == 0.5  # 読めなければ指数バックオフ
    assert retry_delay(0, "-5", lambda: 0.0) == 0.0


def test_it_gives_up_after_the_retry_limit_without_leaking_the_key() -> None:
    client, slept = make(lambda request: httpx.Response(429), max_retries=2)
    with pytest.raises(RuntimeError) as excinfo:
        list(client.query({}))
    assert client.requests == 3 and len(slept) == 2 and SECRET not in str(excinfo.value)


def test_other_errors_are_not_retried() -> None:
    client, slept = make(lambda request: httpx.Response(500))
    with pytest.raises(httpx.HTTPStatusError):
        list(client.query({}))
    assert client.requests == 1 and slept == []


def test_the_api_key_is_required_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LANGSMITH_API_KEY", raising=False)
    with pytest.raises(SystemExit):
        RunsQuery.from_env()


def test_fetch_traces_uses_two_query_series_and_matches_by_label_and_trace() -> None:
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        calls.append(body)
        if body.get("is_root"):
            return page(
                [
                    {"trace_id": "t1", "metadata": {"label": "base", "golden_id": "gc01", "rep": 1}},
                    {"trace_id": "t2", "metadata": {"label": "other", "golden_id": "gc01", "rep": 1}},  # 別のラベル
                ]
            )
        tool = {"inputs": {"query": "酸素"}, "outputs": {"output": {"artifact": [{"name": "a.pdf"}]}}}
        return page([{"trace_id": "t1", "start_time": "2", **tool}, {"trace_id": "t2", "start_time": "1", **tool}])

    client, _ = make(handler)
    traces = fetch_traces("base", dt.datetime(2026, 11, 9, tzinfo=dt.UTC), client, project_ids=["p1", "p2"])
    assert traces == {("gc01", 1): {"queries": ["酸素"], "sources": ["a.pdf"]}}
    assert (
        len(calls) == 2 and calls[0]["project_ids"] == ["p1", "p2"] and calls[1]["run_type"] == "TOOL"
    )  # 45 ターンでも 2 系列
    assert calls[0]["min_start_time"].startswith("2026-11-09")


def test_fetch_traces_skips_the_tool_query_when_no_trace_matches() -> None:
    client, _ = make(lambda request: page([]))
    assert fetch_traces("base", dt.datetime(2026, 11, 9, tzinfo=dt.UTC), client, project_ids=["p"]) == {}
    assert client.requests == 1
