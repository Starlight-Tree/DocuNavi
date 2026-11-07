"""会話履歴の保存先（app/history.py）と、アプリへの接続（lifespan・グラフ・/health・失敗の返し方）の検証。

接続は偽の Worker（tests/d1_fakes.py）。保存先そのものの動き（開閉・checkpointer の読み書き）は tests/test_d1.py。
"""

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver

from app import history
from app.api import routes
from app.d1 import D1Error, D1Pool
from app.main import app

# 接続情報に混ぜて、ログや応答へ漏れていないかを確かめるための文字列
SECRET = "pw-SECRET-abc123"


def test_without_a_url_nothing_is_opened(monkeypatch):
    monkeypatch.setenv("D1_API_URL", "   ")
    assert history.database_url() == ""


def test_lifespan_opens_the_store_and_health_reports_it(d1_worker, monkeypatch):
    monkeypatch.setattr(history.d1, "D1Pool", lambda url: D1Pool(url, transport=d1_worker.transport()))
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "mock", "history": "d1"}
    assert history.get_checkpointer() is None and history.get_pool() is None


def test_unreachable_worker_fails_startup(d1_worker, monkeypatch):
    d1_worker.fail_next = "x"

    class Down(D1Pool):
        async def open(self):
            raise D1Error("データ用 Worker に接続できません (ConnectError)")

    monkeypatch.setattr(history.d1, "D1Pool", lambda url: Down(url, transport=d1_worker.transport()))
    with pytest.raises(D1Error), TestClient(app):
        pass
    assert history.get_checkpointer() is None


def test_graph_uses_the_open_checkpointer_and_forgets_it_after_close(monkeypatch):
    saver = MemorySaver()
    monkeypatch.setattr(history, "_checkpointer", saver)

    graph = routes.get_graph(True)
    monkeypatch.setattr(history, "_checkpointer", None)
    after = routes.get_graph(True)

    assert graph.checkpointer is saver
    assert after.checkpointer is not saver  # 閉じた後は、閉じた保存先のグラフを使い回さない


def test_graph_without_a_store_is_cached_per_mode(monkeypatch):
    monkeypatch.setattr(history, "_checkpointer", None)

    assert routes.get_graph(True) is routes.get_graph(True)
    assert routes.get_graph(True) is not routes.get_graph(False)


# ---- 保存先の失敗は LLM の失敗として返さない ----


def test_store_failure_is_503_with_a_fixed_message():
    failure = routes.classify_llm_error(D1Error("D1 API が 500 を返しました"))

    assert failure.status_code == 503
    assert failure.detail == routes._DETAIL_HISTORY_STORE
    assert SECRET not in failure.detail


def test_store_failure_behind_a_wrapper_is_found_in_the_cause_chain():
    wrapper = RuntimeError("graph failed")
    wrapper.__cause__ = D1Error("D1 API の呼び出しに失敗しました (ReadTimeout)")

    assert routes.classify_llm_error(wrapper).status_code == 503


def test_chat_returns_503_when_the_store_fails(monkeypatch):
    class BrokenGraph:
        async def ainvoke(self, state, config):
            raise D1Error(f"D1 API の呼び出しに失敗しました ({SECRET})")

    monkeypatch.setattr(routes, "get_graph", lambda mock: BrokenGraph())

    with TestClient(app) as client:
        res = client.post("/chat", json={"message": "ザーハードの大気について教えて"})

    assert res.status_code == 503
    assert res.json() == {"detail": routes._DETAIL_HISTORY_STORE}
    assert SECRET not in res.text
