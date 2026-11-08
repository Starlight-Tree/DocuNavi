"""接続プールの上限（`DATABASE_POOL_MAX_SIZE`）のテスト。Postgres は要らない（プールは開かない）。"""

import pytest

from app import pg


def test_default_is_three(monkeypatch):
    monkeypatch.delenv("DATABASE_POOL_MAX_SIZE", raising=False)
    assert pg.pool_max_size() == 3


def test_empty_value_means_default(monkeypatch):
    monkeypatch.setenv("DATABASE_POOL_MAX_SIZE", "  ")
    assert pg.pool_max_size() == 3


@pytest.mark.parametrize(("raw", "expected"), [("2", 2), (" 1 ", 1), ("10", 10)])
def test_reads_the_environment_variable(monkeypatch, raw, expected):
    monkeypatch.setenv("DATABASE_POOL_MAX_SIZE", raw)
    assert pg.pool_max_size() == expected


@pytest.mark.parametrize("raw", ["0", "-1", "two", "2.5"])
def test_invalid_value_fails_instead_of_falling_back(monkeypatch, raw):
    monkeypatch.setenv("DATABASE_POOL_MAX_SIZE", raw)
    with pytest.raises(ValueError, match="DATABASE_POOL_MAX_SIZE"):
        pg.pool_max_size()


def test_new_pool_uses_the_configured_size(monkeypatch):
    monkeypatch.setenv("DATABASE_POOL_MAX_SIZE", "2")
    pool = pg.new_pool("postgresql://u:p@localhost:5432/db")  # 開かないので、接続はしない
    assert pool._pool.max_size == 2
    assert pool._pool.min_size == 1


def test_new_pool_default_keeps_three(monkeypatch):
    monkeypatch.delenv("DATABASE_POOL_MAX_SIZE", raising=False)
    assert pg.new_pool("postgresql://u:p@localhost:5432/db")._pool.max_size == 3
