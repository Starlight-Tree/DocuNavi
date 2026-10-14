"""会話履歴の保存先（app/history.py）の検証。本物の DB は使わず、接続プールと保存先を偽物に差し替える。

本物の Postgres での検証は tests/test_history_postgres.py（TEST_DATABASE_URL があるときだけ動く）。
"""

import asyncio
import logging
import time

import psycopg
import psycopg_pool
import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import MemorySaver
from psycopg.errors import UniqueViolation
from psycopg.rows import dict_row

from app import history
from app.api import routes
from app.main import app

# 接続情報に混ぜて、ログや応答へ漏れていないかを確かめるための文字列
SECRET_PASSWORD = "pw-SECRET-abc123"
FAKE_URL = f"postgresql://app:{SECRET_PASSWORD}@db.internal.example:5432/postgres"


class FakePool:
    """AsyncConnectionPool の代わり。作られた引数と、open/close の呼ばれ方を記録する。"""

    instances: list["FakePool"] = []
    open_error: BaseException | None = None

    def __init__(self, conninfo, **kwargs):
        self.conninfo = conninfo
        self.kwargs = kwargs
        self.opened = False
        self.closed = 0
        FakePool.instances.append(self)

    @staticmethod
    async def check_connection(conn):  # 本物は静的メソッド。ここでは参照できれば足りる
        return None

    async def open(self, wait=False, timeout=30.0):
        self.open_wait = wait
        self.open_timeout = timeout
        if FakePool.open_error is not None:
            raise FakePool.open_error
        self.opened = True

    async def close(self):
        self.closed += 1


class FakeSaver:
    """AsyncPostgresSaver の代わり。setup() が呼ばれた回数と、送出する例外を設定できる。"""

    instances: list["FakeSaver"] = []
    setup_errors: list[BaseException] = []

    def __init__(self, pool):
        self.pool = pool
        self.setup_calls = 0
        FakeSaver.instances.append(self)

    async def setup(self):
        self.setup_calls += 1
        if FakeSaver.setup_errors:
            raise FakeSaver.setup_errors.pop(0)


@pytest.fixture
def fake_db(monkeypatch):
    """history_store が import する接続プールと保存先を偽物にし、DATABASE_URL を設定する。"""
    import langgraph.checkpoint.postgres.aio as saver_module
    import psycopg_pool

    FakePool.instances = []
    FakePool.open_error = None
    FakeSaver.instances = []
    FakeSaver.setup_errors = []
    monkeypatch.setattr(psycopg_pool, "AsyncConnectionPool", FakePool)
    monkeypatch.setattr(saver_module, "AsyncPostgresSaver", FakeSaver)
    monkeypatch.setattr(history, "_SETUP_RETRY_SECONDS", 0.0)
    monkeypatch.setenv("DATABASE_URL", FAKE_URL)
    monkeypatch.delenv("DATABASE_POOL_MAX_SIZE", raising=False)
    return FakePool


def _enter_and_leave() -> tuple[object | None, object | None, object | None]:
    """history_store に入って、中と外での get_checkpointer() を返す。"""

    async def scenario():
        async with history.history_store() as saver:
            inside = history.get_checkpointer()
        return saver, inside, history.get_checkpointer()

    return asyncio.run(scenario())


# ---- DATABASE_URL が空: 従来どおり ----


def test_without_database_url_nothing_is_opened(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "")

    saver, inside, after = _enter_and_leave()

    assert saver is None and inside is None and after is None
    assert history.history_backend() == "memory"


def test_whitespace_only_database_url_is_treated_as_unset(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "   ")

    saver, _, _ = _enter_and_leave()

    assert saver is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, 5),
        ("", 5),
        ("abc", 5),
        ("0", 5),
        ("-3", 5),
        ("2.5", 5),
        ("1", 1),
        ("12", 12),
    ],
)
def test_pool_max_size_falls_back_to_default(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("DATABASE_POOL_MAX_SIZE", raising=False)
    else:
        monkeypatch.setenv("DATABASE_POOL_MAX_SIZE", raw)

    assert history.pool_max_size() == expected


# ---- DATABASE_URL あり: 開く・使う・閉じる ----


def test_opens_pool_sets_up_and_closes(fake_db, monkeypatch):
    monkeypatch.setenv("DATABASE_POOL_MAX_SIZE", "7")

    saver, inside, after = _enter_and_leave()

    pool = FakePool.instances[0]
    assert pool.conninfo == FAKE_URL
    assert pool.opened and pool.open_wait is True
    assert pool.kwargs["max_size"] == 7 and pool.kwargs["min_size"] == 1
    assert pool.kwargs["open"] is False  # 自分で open(wait=True) して、届かなければ起動を失敗させる
    assert pool.kwargs["check"] is FakePool.check_connection
    # checkpointer が要求する接続の設定。prepared statement は transaction mode のプーラーのために切る
    conn_kwargs = pool.kwargs["kwargs"]
    assert conn_kwargs["autocommit"] is True
    assert conn_kwargs["row_factory"] is dict_row
    assert conn_kwargs["prepare_threshold"] is None

    assert saver is FakeSaver.instances[0] and saver.pool is pool
    assert saver.setup_calls == 1
    assert inside is saver  # 開いている間だけ get_checkpointer() で取れる
    assert after is None
    assert pool.closed == 1


def test_backend_is_postgres_only_while_open(fake_db):
    async def scenario():
        before = history.history_backend()
        async with history.history_store():
            during = history.history_backend()
        return before, during, history.history_backend()

    assert asyncio.run(scenario()) == ("memory", "postgres", "memory")


def test_pool_is_closed_and_checkpointer_cleared_when_the_body_raises(fake_db):
    async def scenario():
        async with history.history_store():
            raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        asyncio.run(scenario())

    assert FakePool.instances[0].closed == 1
    assert history.get_checkpointer() is None


def test_unreachable_database_fails_startup_and_closes_the_pool(fake_db):
    FakePool.open_error = psycopg.OperationalError("could not connect")

    with pytest.raises(psycopg.OperationalError):
        _enter_and_leave()

    assert FakePool.instances[0].closed == 1
    assert FakeSaver.instances == []  # 保存先は作らない
    assert history.get_checkpointer() is None  # メモリへ黙って切り替えない


def test_setup_is_retried_when_instances_start_at_the_same_time(fake_db):
    FakeSaver.setup_errors = [UniqueViolation("dup"), UniqueViolation("dup")]

    saver, _, _ = _enter_and_leave()

    assert saver.setup_calls == 3


@pytest.mark.parametrize(
    "race_error",
    [psycopg.errors.DuplicateTable, psycopg.errors.DuplicateObject],
    ids=["DuplicateTable", "DuplicateObject"],
)
def test_other_race_errors_of_a_concurrent_first_start_are_retried(fake_db, race_error):
    """同時に初回起動すると、UniqueViolation のほかに、表・型が既にある旨のエラーも出る（CI で DuplicateTable を実測）。"""
    FakeSaver.setup_errors = [UniqueViolation("dup"), race_error("dup")]

    saver, _, _ = _enter_and_leave()

    assert saver.setup_calls == 3


def test_setup_gives_up_after_the_attempt_limit(fake_db):
    FakeSaver.setup_errors = [UniqueViolation("dup")] * history._SETUP_ATTEMPTS

    with pytest.raises(UniqueViolation):
        _enter_and_leave()

    assert FakeSaver.instances[0].setup_calls == history._SETUP_ATTEMPTS
    assert FakePool.instances[0].closed == 1
    assert history.get_checkpointer() is None


def test_other_setup_errors_are_not_retried(fake_db):
    FakeSaver.setup_errors = [psycopg.errors.InsufficientPrivilege("no create")]

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        _enter_and_leave()

    assert FakeSaver.instances[0].setup_calls == 1
    assert FakePool.instances[0].closed == 1


def test_connection_string_never_reaches_the_log(fake_db, caplog):
    caplog.set_level(logging.DEBUG)

    _enter_and_leave()

    assert "app.history" in caplog.text  # 保存先の記録は出ている
    assert SECRET_PASSWORD not in caplog.text
    assert "db.internal.example" not in caplog.text


def test_unreachable_database_really_fails_startup_quickly(monkeypatch, caplog):
    """偽物ではなく本物の接続プールで、届かない DB の起動失敗を確かめる（ポート 1 は接続を拒否する）。"""
    monkeypatch.setenv("DATABASE_URL", f"postgresql://app:{SECRET_PASSWORD}@127.0.0.1:1/none")
    monkeypatch.setattr(history, "_OPEN_TIMEOUT_SECONDS", 1.0)
    started = time.monotonic()

    with pytest.raises(psycopg_pool.PoolTimeout):
        _enter_and_leave()

    assert time.monotonic() - started < 5.0
    assert history.get_checkpointer() is None
    assert SECRET_PASSWORD not in caplog.text


# ---- アプリへの接続（lifespan・グラフ・/health） ----


def test_lifespan_opens_the_store_and_health_reports_it(fake_db):
    with TestClient(app) as client:
        assert FakePool.instances[0].opened
        assert client.get("/health").json() == {"status": "ok", "mode": "mock", "history": "postgres"}

    assert FakePool.instances[0].closed == 1
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


@pytest.mark.parametrize(
    "make_exc",
    [
        lambda: psycopg.OperationalError(f"connection to server at db.internal.example failed: {SECRET_PASSWORD}"),
        lambda: __import__("psycopg_pool").PoolTimeout(f"couldn't get a connection ({SECRET_PASSWORD})"),
        lambda: psycopg.errors.AdminShutdown("terminating connection due to administrator command"),
    ],
    ids=["operational", "pool-timeout", "admin-shutdown"],
)
def test_store_failure_is_503_with_a_fixed_message(make_exc):
    failure = routes.classify_llm_error(make_exc())

    assert failure.status_code == 503
    assert failure.detail == routes._DETAIL_HISTORY_STORE
    assert SECRET_PASSWORD not in failure.detail


def test_store_failure_behind_a_wrapper_is_found_in_the_cause_chain():
    wrapper = RuntimeError("graph failed")
    wrapper.__cause__ = psycopg.OperationalError("server closed the connection")

    assert routes.classify_llm_error(wrapper).status_code == 503


def test_chat_returns_503_when_the_store_fails(monkeypatch):
    class BrokenGraph:
        async def ainvoke(self, state, config):
            raise psycopg.OperationalError(f"connection refused {SECRET_PASSWORD}")

    monkeypatch.setattr(routes, "get_graph", lambda mock: BrokenGraph())

    with TestClient(app) as client:
        res = client.post("/chat", json={"message": "ザーハードの大気について教えて"})

    assert res.status_code == 503
    assert res.json() == {"detail": routes._DETAIL_HISTORY_STORE}
    assert SECRET_PASSWORD not in res.text
