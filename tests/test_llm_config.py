"""LLM の選択（モック / Gemini API キー / Vertex AI）の切り替え。

実際の LLM クライアントは作らず、graph.ChatGoogleGenerativeAI を記録用の偽物に差し替える。
"""

import pytest

from app.agents import graph
from app.agents.graph import MockChatModel, create_llm, is_mock_mode, use_vertex_ai

_ENV_NAMES = (
    "GEMINI_API_KEY",
    "GEMINI_MODEL",
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
)


class RecordingLLM:
    def __init__(self, **kwargs):
        self.kwargs = kwargs


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(graph, "ChatGoogleGenerativeAI", RecordingLLM)


@pytest.mark.parametrize("key", ["", "   ", "your_api_key_here", " your_api_key_here "])
def test_missing_or_placeholder_key_is_mock_mode(monkeypatch, key):
    monkeypatch.setenv("GEMINI_API_KEY", key)
    assert is_mock_mode() is True
    assert isinstance(create_llm(), MockChatModel)


def test_unset_key_is_mock_mode():
    assert is_mock_mode() is True
    assert isinstance(create_llm(), MockChatModel)


def test_api_key_uses_gemini_with_that_key(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "real-looking-key")
    assert is_mock_mode() is False
    llm = create_llm()
    assert isinstance(llm, RecordingLLM)
    assert llm.kwargs == {
        "model": "gemini-3.1-flash-lite",
        "google_api_key": "real-looking-key",
        "temperature": 0,
    }


@pytest.mark.parametrize("value", ["true", "TRUE", "1", "yes", " on "])
def test_vertex_flag_values_that_enable_vertex(monkeypatch, value):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", value)
    assert use_vertex_ai() is True


@pytest.mark.parametrize("value", ["", "false", "0", "no", "off", "maybe"])
def test_vertex_flag_values_that_do_not_enable_vertex(monkeypatch, value):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", value)
    assert use_vertex_ai() is False
    assert is_mock_mode() is True


def test_vertex_needs_no_api_key_and_is_not_mock(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    assert is_mock_mode() is False
    llm = create_llm()
    assert isinstance(llm, RecordingLLM)
    # キーは渡さず ADC に任せる。プロジェクト・ロケーションも未設定なら SDK に解決させる
    assert llm.kwargs == {"model": "gemini-3.1-flash-lite", "vertexai": True, "temperature": 0}


def test_vertex_ignores_api_key_and_passes_project_and_location(monkeypatch):
    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    monkeypatch.setenv("GEMINI_API_KEY", "should-not-be-used")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "my-project")
    monkeypatch.setenv("GOOGLE_CLOUD_LOCATION", "global")
    llm = create_llm()
    assert llm.kwargs == {
        "model": "gemini-3.1-flash-lite",
        "vertexai": True,
        "temperature": 0,
        "project": "my-project",
        "location": "global",
    }


def test_model_name_can_be_overridden(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", " gemini-custom ")
    monkeypatch.setenv("GEMINI_API_KEY", "k")
    assert create_llm().kwargs["model"] == "gemini-custom"

    monkeypatch.setenv("GOOGLE_GENAI_USE_VERTEXAI", "true")
    assert create_llm().kwargs["model"] == "gemini-custom"


def test_model_override_does_not_leave_mock_mode(monkeypatch):
    monkeypatch.setenv("GEMINI_MODEL", "gemini-custom")
    assert isinstance(create_llm(), MockChatModel)
