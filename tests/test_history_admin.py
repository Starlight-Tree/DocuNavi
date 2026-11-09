"""app/history_admin.py の、DB を使わない部分（引数の検査と、DATABASE_URL 未設定の扱い）。"""

import time

import pytest

from app import history_admin
from app.d1 import SyncD1


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    # .env の DATABASE_URL を読み込まないようにする（テストが本物の DB に触れないため）
    monkeypatch.setattr(history_admin, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("D1_API_URL", "")


def test_without_a_target_the_default_is_seven_days_and_nothing_is_deleted(d1_worker, monkeypatch, capsys):
    _use_fake(d1_worker, monkeypatch)
    _thread(d1_worker, "old", days_ago=8)
    _thread(d1_worker, "recent", days_ago=6)
    assert history_admin.main([]) == 0
    assert "対象の会話: 1 件（削除はしていません" in capsys.readouterr().out
    assert _threads(d1_worker) == {"old", "recent"}


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


# ---- 削除の挙動（本物の SQLite の偽 Worker。DB は触らない） ----


def _use_fake(worker, monkeypatch):
    monkeypatch.setenv("D1_API_URL", "https://worker.test")
    monkeypatch.setattr(history_admin, "SyncD1", lambda url: SyncD1(url, transport=worker.transport()))


def _thread(worker, thread_id, days_ago):
    created_at = int((time.time() - days_ago * 86400) * 1000)
    worker.run(
        "INSERT INTO checkpoints (thread_id, checkpoint_id, checkpoint, created_at) VALUES (?, 'c1', '{}', ?)",
        (thread_id, created_at),
    )
    worker.run(
        "INSERT INTO checkpoint_writes (thread_id, checkpoint_id, task_id, idx, channel) VALUES (?, 'c1', 't', 0, 'ch')",
        (thread_id,),
    )


def _threads(worker):
    return {r["thread_id"] for r in worker.rows("SELECT thread_id FROM checkpoints")}


def _writes(worker):
    return {r["thread_id"] for r in worker.rows("SELECT thread_id FROM checkpoint_writes")}


def test_dry_run_only_counts_and_deletes_nothing(d1_worker, monkeypatch, capsys):
    _use_fake(d1_worker, monkeypatch)
    _thread(d1_worker, "old", days_ago=8)
    _thread(d1_worker, "recent", days_ago=6)
    assert history_admin.main(["--older-than-days", "7"]) == 0
    assert "対象の会話: 1 件（削除はしていません" in capsys.readouterr().out
    assert _threads(d1_worker) == {"old", "recent"}
    assert _writes(d1_worker) == {"old", "recent"}


def test_apply_deletes_only_threads_older_than_the_limit(d1_worker, monkeypatch, capsys):
    _use_fake(d1_worker, monkeypatch)
    _thread(d1_worker, "old", days_ago=8)
    _thread(d1_worker, "recent", days_ago=6)
    assert history_admin.main(["--older-than-days", "7", "--apply"]) == 0
    assert "削除した会話: 1 件" in capsys.readouterr().out
    assert _threads(d1_worker) == {"recent"}
    assert _writes(d1_worker) == {"recent"}


def test_threads_newer_than_the_limit_are_kept_with_apply(d1_worker, monkeypatch, capsys):
    _use_fake(d1_worker, monkeypatch)
    _thread(d1_worker, "recent", days_ago=6)
    _thread(d1_worker, "today", days_ago=0)
    assert history_admin.main(["--apply"]) == 0  # 日数を省くと 7 日
    assert "削除した会話: 0 件" in capsys.readouterr().out
    assert _threads(d1_worker) == {"recent", "today"}


def test_the_number_of_days_can_be_overridden(d1_worker, monkeypatch, capsys):
    _use_fake(d1_worker, monkeypatch)
    _thread(d1_worker, "two-days", days_ago=2)
    _thread(d1_worker, "today", days_ago=0)
    assert history_admin.main(["--older-than-days", "1", "--apply"]) == 0
    assert "削除した会話: 1 件" in capsys.readouterr().out
    assert _threads(d1_worker) == {"today"}
