"""記録フック（.claude/hooks/hook-log.py）の出力・秘匿・失敗時の挙動を確かめる。"""

import json
import os
import stat
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "hook-log.py"
# 秘密に見える偽の値。文字列を割って書くのは、秘密情報の検査（コミット・プッシュ前）に、テストの偽値が当たらないようにするため
SECRET = "dpl_SECRETID123 sb_" + "secret_abc post" + "gres://u:p@h/db"


def _run(home: Path, payload, raw: str | None = None) -> subprocess.CompletedProcess:
    env = {**os.environ, "HOME": str(home)}
    return subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input=raw if raw is not None else json.dumps(payload),
        capture_output=True,
        text=True,
        timeout=10,
        env=env,
    )


def _rows(home: Path) -> list[dict]:
    log = home / ".claude" / "hook-events.jsonl"
    return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]


def _mcp_payload(event: str = "PermissionRequest") -> dict:
    return {
        "hook_event_name": event,
        "tool_name": "mcp__vercel__cancel_deployment",
        "tool_input": {"id": SECRET},
        "mcp_server": "vercel",
        "permission_mode": "auto",
        "session_id": "abcdef1234567890",
        "tool_use_id": "toolu_x",
    }


def test_writes_fixed_fields_only_and_no_stdout(tmp_path):
    proc = _run(tmp_path, _mcp_payload())
    assert proc.returncode == 0
    assert proc.stdout == ""
    (row,) = _rows(tmp_path)
    assert set(row) == {"at", "event", "tool", "mcp_server", "permission_mode", "session", "tool_use_id"}
    assert row["tool"] == "mcp__vercel__cancel_deployment"
    assert row["permission_mode"] == "auto"
    assert row["session"] == "abcdef12"


def test_never_writes_tool_input(tmp_path):
    _run(tmp_path, _mcp_payload("PreToolUse"))
    text = (tmp_path / ".claude" / "hook-events.jsonl").read_text(encoding="utf-8")
    for word in ("SECRET", "sb_secret", "postgres://", "tool_input"):
        assert word not in text


def test_log_file_mode_is_600(tmp_path):
    _run(tmp_path, _mcp_payload())
    mode = stat.S_IMODE((tmp_path / ".claude" / "hook-events.jsonl").stat().st_mode)
    assert mode == 0o600


def test_bash_git_restore_logged_as_kind_without_command(tmp_path):
    payload = {
        "hook_event_name": "PermissionRequest",
        "tool_name": "Bash",
        "tool_input": {"command": "git restore --help"},
        "permission_mode": "auto",
        "session_id": "s" * 8,
    }
    _run(tmp_path, payload)
    (row,) = _rows(tmp_path)
    assert row["tool"] == "Bash:git-restore"
    assert "--help" not in json.dumps(row)


def test_other_bash_is_not_logged(tmp_path):
    payload = {"hook_event_name": "PermissionRequest", "tool_name": "Bash", "tool_input": {"command": "ls"}}
    proc = _run(tmp_path, payload)
    assert proc.returncode == 0
    assert not (tmp_path / ".claude" / "hook-events.jsonl").exists()


def test_broken_input_exits_zero_silently(tmp_path):
    proc = _run(tmp_path, None, raw="not json")
    assert proc.returncode == 0
    assert proc.stdout == ""


def test_unwritable_home_exits_zero_silently(tmp_path):
    blocker = tmp_path / "file"
    blocker.write_text("x")
    proc = _run(blocker, _mcp_payload())
    assert proc.returncode == 0
    assert proc.stdout == ""
