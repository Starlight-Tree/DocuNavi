"""app/history_admin.py の、DB を使わない部分（引数の検査と、DATABASE_URL 未設定の扱い）。"""

import time

import pytest

from app import history_admin
from app.d1 import SyncD1
from app.d1_saver import D1Saver


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


# ---- 題名の補完（--backfill-titles）。Go の読み取り API は題名を metadata の title からだけ読む ----


def _legacy_thread(worker, thread_id, messages, *, checkpoint_id="c1", metadata="{}"):
    """題名（metadata の title）が無い、この機能より前の会話。本文は本物のシリアライザーで保存する。"""
    from langchain_core.messages import HumanMessage

    saver = D1Saver(None)
    checkpoint = {
        "v": 4,
        "id": checkpoint_id,
        "ts": "2026-11-20T00:00:00+00:00",
        "channel_values": {"messages": [HumanMessage(content=m) for m in messages]},
    }
    type_, blob = saver.serde.dumps_typed(checkpoint)
    worker.run(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata, created_at) VALUES (?, '', ?, ?, ?, ?, 1)",
        (thread_id, checkpoint_id, type_, saver._enc(blob), metadata),
    )


def _metadata(worker, thread_id, checkpoint_id="c1"):
    import json

    row = worker.rows(
        "SELECT metadata FROM checkpoints WHERE thread_id = ? AND checkpoint_id = ?", (thread_id, checkpoint_id)
    )[0]
    return json.loads(row["metadata"])


def test_backfill_titles_dry_run_counts_and_writes_nothing(d1_worker, monkeypatch, capsys):
    _use_fake(d1_worker, monkeypatch)
    _legacy_thread(d1_worker, "u1:a", ["最初の質問です"])
    _legacy_thread(d1_worker, "u1:empty", [])
    assert history_admin.main(["--backfill-titles"]) == 0
    out = capsys.readouterr().out
    assert (
        "題名が未保存の会話: 2 件（題名を作れる: 1 件、本文のあるユーザー発言が無い: 1 件、復号できない: 0 件）" in out
    )
    assert "書き込みはしていません" in out
    assert "title" not in _metadata(d1_worker, "u1:a")


def test_backfill_titles_writes_the_same_title_the_list_would_make(d1_worker, monkeypatch, capsys):
    from langchain_core.messages import HumanMessage

    from app.thread_title import stored_title

    _use_fake(d1_worker, monkeypatch)
    long_question = "とても長い質問 " * 20
    _legacy_thread(d1_worker, "u1:a", ["  最初の\n質問です  ", "二つ目"], metadata='{"source": "loop", "step": 3}')
    _legacy_thread(d1_worker, "u1:long", [long_question])
    _legacy_thread(d1_worker, "u1:empty", [])
    assert history_admin.main(["--backfill-titles", "--apply"]) == 0
    assert "書き込んだ題名: 2 件" in capsys.readouterr().out
    assert _metadata(d1_worker, "u1:a") == {"source": "loop", "step": 3, "title": "最初の 質問です"}  # 既存の項目は残す
    assert _metadata(d1_worker, "u1:long")["title"] == stored_title([HumanMessage(content=long_question)])
    assert len(_metadata(d1_worker, "u1:long")["title"]) == 40
    assert "title" not in _metadata(d1_worker, "u1:empty")  # 題名にできる発言が無い会話は、書かない


def test_backfill_titles_only_touches_the_latest_checkpoint_without_a_title(d1_worker, monkeypatch, capsys):
    _use_fake(d1_worker, monkeypatch)
    _legacy_thread(d1_worker, "u1:a", ["古い質問"], checkpoint_id="c1")
    _legacy_thread(d1_worker, "u1:a", ["古い質問", "新しい質問"], checkpoint_id="c2")
    _legacy_thread(d1_worker, "u1:titled", ["別の質問"], metadata='{"title": "付いている題名"}')
    assert history_admin.main(["--backfill-titles", "--apply"]) == 0
    assert "題名が未保存の会話: 1 件" in capsys.readouterr().out
    assert _metadata(d1_worker, "u1:a", "c2")["title"] == "古い質問"  # 一覧は最新のチェックポイントの題名を読む
    assert "title" not in _metadata(d1_worker, "u1:a", "c1")
    assert _metadata(d1_worker, "u1:titled") == {"title": "付いている題名"}  # 題名のある行は触らない
    # 流し直しても同じ（題名のある行は対象に入らない）
    assert history_admin.main(["--backfill-titles", "--apply"]) == 0
    assert "題名が未保存の会話: 0 件" in capsys.readouterr().out


def test_backfill_titles_skips_broken_rows_without_echoing_them(d1_worker, monkeypatch, capsys):
    _use_fake(d1_worker, monkeypatch)
    _legacy_thread(d1_worker, "u1:ok", ["質問"])
    d1_worker.run(
        "INSERT INTO checkpoints (thread_id, checkpoint_ns, checkpoint_id, type, checkpoint, metadata, created_at) VALUES ('u1:bad', '', 'c1', 'msgpack', 'not-base64-秘密', '{}', 1)"
    )
    assert history_admin.main(["--backfill-titles", "--apply"]) == 0
    captured = capsys.readouterr()
    assert "復号できない: 1 件" in captured.out and "書き込んだ題名: 1 件" in captured.out
    assert "秘密" not in captured.out + captured.err


def test_backfill_titles_is_exclusive_with_the_deletion_targets():
    for other in (["--older-than-days", "3"], ["--thread-id", "x"]):
        with pytest.raises(SystemExit) as exc:
            history_admin.main(["--backfill-titles", *other])
        assert exc.value.code == 2
