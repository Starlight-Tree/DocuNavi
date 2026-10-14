"""LLM の試行設定（LLM_HEDGE_DELAY_SECONDS / LLM_ATTEMPT_TIMEOUT_SECONDS / LLM_MAX_ATTEMPTS）。

tests/test_llm_config.py と同様、実際のクライアントは作らず記録用の偽物に差し替える。
"""

import pytest

from app.agents import graph
from app.agents.graph import (
    DEFAULT_LLM_ATTEMPT_TIMEOUT_SECONDS,
    DEFAULT_LLM_ATTEMPT_TIMEOUT_SECONDS_SEQUENTIAL,
    DEFAULT_LLM_HEDGE_DELAY_SECONDS,
    DEFAULT_LLM_MAX_ATTEMPTS,
    MockChatModel,
    create_llm,
    llm_attempt_timeout_seconds,
    llm_hedge_delay_seconds,
    llm_hedge_policy,
    llm_max_attempts,
)


class RecordingLLM:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in (
        "GEMINI_API_KEY",
        "GEMINI_MODEL",
        "GOOGLE_GENAI_USE_VERTEXAI",
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
        "LLM_HEDGE_DELAY_SECONDS",
        "LLM_ATTEMPT_TIMEOUT_SECONDS",
        "LLM_MAX_ATTEMPTS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(graph, "ChatGoogleGenerativeAI", RecordingLLM)


def test_defaults_hedge_slow_calls_and_leave_retries_to_the_scheduler(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    llm = create_llm()

    assert llm.timeout == DEFAULT_LLM_ATTEMPT_TIMEOUT_SECONDS == 30.0
    # 再試行はヘッジのスケジューラが担う。SDK も再試行すると、投げる本数が掛け算で増える
    assert llm.max_retries == 1
    policy = llm_hedge_policy()
    assert policy is not None
    assert (policy.hedge_delay, policy.max_attempts) == (DEFAULT_LLM_HEDGE_DELAY_SECONDS, DEFAULT_LLM_MAX_ATTEMPTS)
    assert (DEFAULT_LLM_HEDGE_DELAY_SECONDS, DEFAULT_LLM_MAX_ATTEMPTS) == (8.0, 3)


def test_hedging_off_falls_back_to_sdk_retries_with_a_shorter_attempt_cap(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", "0")
    llm = create_llm()

    assert llm_hedge_policy() is None
    assert llm.timeout == DEFAULT_LLM_ATTEMPT_TIMEOUT_SECONDS_SEQUENTIAL == 15.0
    assert llm.max_retries == DEFAULT_LLM_MAX_ATTEMPTS == 3


def test_retry_policy_is_independent_of_model_and_credential_selection(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    # モデル・認証の選択に使う引数は変えない（試行の設定は別に適用する）
    assert create_llm().kwargs == {"model": "gemini-3.1-flash-lite", "google_api_key": "k"}


def test_vertex_ai_gets_the_same_policy(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("LLM_ATTEMPT_TIMEOUT_SECONDS", "8")
    llm = create_llm()

    assert llm.kwargs["vertexai"] is True
    assert (llm.timeout, llm.max_retries) == (8.0, 1)


def test_values_can_be_overridden_by_env(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", " 5 ")
    monkeypatch.setenv("LLM_ATTEMPT_TIMEOUT_SECONDS", " 7.5 ")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", " 4 ")

    assert llm_hedge_delay_seconds() == 5.0
    assert llm_attempt_timeout_seconds() == 7.5
    assert llm_max_attempts() == 4
    policy = llm_hedge_policy()
    assert (policy.hedge_delay, policy.max_attempts) == (5.0, 4)
    llm = create_llm()
    assert (llm.timeout, llm.max_retries) == (7.5, 1)


def test_one_attempt_means_no_retry_with_or_without_hedging(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", "1")
    assert llm_max_attempts() == 1
    assert llm_hedge_policy().max_attempts == 1  # ヘッジあり: スケジューラは 1 本しか投げない

    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", "0")
    assert llm_hedge_policy() is None
    assert create_llm().max_retries == 1  # ヘッジなし: SDK も最初の 1 回だけ（max_retries は最初を含む）


@pytest.mark.parametrize("value", ["", "abc", "-1", "nan", "inf", "-inf"])
def test_invalid_hedge_delay_falls_back_to_default(monkeypatch, value):
    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", value)
    assert llm_hedge_delay_seconds() == DEFAULT_LLM_HEDGE_DELAY_SECONDS


def test_zero_hedge_delay_is_a_valid_way_to_disable_hedging(monkeypatch):
    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", "0")
    assert llm_hedge_delay_seconds() == 0.0


@pytest.mark.parametrize("value", ["", "abc", "0", "-1", "-0.5", "nan", "inf", "-inf"])
@pytest.mark.parametrize(
    ("hedge_delay", "expected"),
    [(None, DEFAULT_LLM_ATTEMPT_TIMEOUT_SECONDS), ("0", DEFAULT_LLM_ATTEMPT_TIMEOUT_SECONDS_SEQUENTIAL)],
    ids=["hedging", "no-hedging"],
)
def test_invalid_attempt_timeout_falls_back_to_the_default_for_the_mode(monkeypatch, value, hedge_delay, expected):
    if hedge_delay is not None:
        monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", hedge_delay)
    monkeypatch.setenv("LLM_ATTEMPT_TIMEOUT_SECONDS", value)
    assert llm_attempt_timeout_seconds() == expected


@pytest.mark.parametrize("value", ["", "abc", "0", "-2", "1.5", "nan"])
def test_invalid_attempts_fall_back_to_default(monkeypatch, value):
    # 0 は SDK が「既定の 5 回」と解釈するため、そのままは渡さない
    monkeypatch.setenv("LLM_MAX_ATTEMPTS", value)
    assert llm_max_attempts() == DEFAULT_LLM_MAX_ATTEMPTS


def test_mock_mode_returns_the_mock_whatever_the_settings(monkeypatch):
    # モックを本物のクライアントのように扱わない（ヘッジの適用除外は build_graph 側のテストで確認する）
    monkeypatch.setenv("LLM_ATTEMPT_TIMEOUT_SECONDS", "1")
    monkeypatch.setenv("LLM_HEDGE_DELAY_SECONDS", "0.01")
    llm = create_llm()
    assert isinstance(llm, MockChatModel)
    assert not hasattr(llm, "max_retries")


def test_real_client_accepts_the_policy_and_keeps_it_through_bind_tools(monkeypatch):
    """偽物ではなく本物のクラスで、構築後の代入が検証・凍結に弾かれず bind_tools 後も残ること。"""
    from langchain_google_genai import ChatGoogleGenerativeAI

    monkeypatch.setattr(graph, "ChatGoogleGenerativeAI", ChatGoogleGenerativeAI)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-key-for-construction-only")
    monkeypatch.setenv("LLM_ATTEMPT_TIMEOUT_SECONDS", "9")

    llm = create_llm()
    bound = llm.bind_tools([])

    assert (llm.timeout, llm.max_retries) == (9.0, 1)
    assert (bound.bound.timeout, bound.bound.max_retries) == (9.0, 1)
