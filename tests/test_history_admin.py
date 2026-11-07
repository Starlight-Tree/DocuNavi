"""app/history_admin.py の、DB を使わない部分（引数の検査と、DATABASE_URL 未設定の扱い）。

本物の Postgres での削除は tests/test_history_postgres.py（TEST_DATABASE_URL があるときだけ動く）。
"""

import pytest

from app import history_admin


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    # .env の DATABASE_URL を読み込まないようにする（テストが本物の DB に触れないため）
    monkeypatch.setattr(history_admin, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("D1_API_URL", "")


def test_a_target_is_required(capsys):
    with pytest.raises(SystemExit) as exc:
        history_admin.main([])
    assert exc.value.code == 2


def test_older_than_days_and_thread_id_are_exclusive():
    with pytest.raises(SystemExit) as exc:
        history_admin.main(["--older-than-days", "30", "--thread-id", "abc"])
    assert exc.value.code == 2


@pytest.mark.parametrize("days", ["0", "-5"])
def test_days_below_one_are_refused(days, capsys):
    assert history_admin.main(["--older-than-days", days]) == 2
    assert "1 以上" in capsys.readouterr().err


def test_blank_thread_id_is_refused(capsys):
    assert history_admin.main(["--thread-id", "  "]) == 2
    assert "空" in capsys.readouterr().err


def test_without_database_url_nothing_is_done(capsys):
    assert history_admin.main(["--older-than-days", "30", "--apply"]) == 2
    assert "D1_API_URL" in capsys.readouterr().err
