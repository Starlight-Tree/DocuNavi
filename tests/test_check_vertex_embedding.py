"""scripts/check_vertex_embedding.py の配線（SDK は偽物。実 API は呼ばない）。"""

from types import SimpleNamespace

import pytest

from app.ingestion import embedding
from scripts import check_vertex_embedding as script


def _vec(seed: float = 1.0) -> list[float]:
    return [seed] + [0.5] * (embedding.DIMENSIONS - 1)


class FakeClient:
    def __init__(self, per_call: int | None = None) -> None:
        self.calls = 0
        self.per_call = per_call

    @property
    def models(self):
        return self

    def embed_content(self, *, model, contents, config):
        self.calls += 1
        n = self.per_call if self.per_call is not None else len(contents)
        return SimpleNamespace(embeddings=[SimpleNamespace(values=_vec(i + 1)) for i in range(n)])


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    # スクリプトは os.environ を直接書き換えるので、monkeypatch に元の状態を記録させて、テストの後に戻す
    monkeypatch.setenv("EMBEDDING_USE_VERTEXAI", "")
    monkeypatch.setenv("GOOGLE_CLOUD_PROJECT", "")


def test_vertex_only_run_needs_no_api_key_and_makes_three_calls(monkeypatch, capsys):
    client = FakeClient()
    seen = {}

    def make(api_key, timeout_ms):
        seen["api_key"], seen["vertex"] = api_key, embedding.use_vertex()
        return client

    monkeypatch.setattr(embedding, "make_client", make)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert script.main(["--project", "some-project"]) == 0
    assert client.calls == 3
    assert seen == {"api_key": None, "vertex": True}
    out = capsys.readouterr().out
    assert "返却 3 本" in out and "未確認" in out


def test_a_batch_that_returns_fewer_vectors_is_reported(monkeypatch, capsys):
    monkeypatch.setattr(embedding, "make_client", lambda api_key, timeout_ms: FakeClient(per_call=1))
    assert script.main(["--project", "some-project"]) == 1
    assert "1 本ずつ呼ぶ" in capsys.readouterr().out


def test_failures_print_only_the_type_and_status(monkeypatch, capsys):
    class Boom(Exception):
        code = 403

    class Failing(FakeClient):
        def embed_content(self, **kwargs):
            raise Boom("https://secret.example/projects/some-project")

    monkeypatch.setattr(embedding, "make_client", lambda api_key, timeout_ms: Failing())
    assert script.main(["--project", "some-project"]) == 1
    err = capsys.readouterr().err
    assert "Boom (status=403)" in err and "secret.example" not in err


def test_compare_api_needs_a_key(monkeypatch, capsys):
    monkeypatch.setattr(embedding, "make_client", lambda api_key, timeout_ms: FakeClient())
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    assert script.main(["--project", "p", "--compare-api"]) == 2
