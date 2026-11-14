"""PreToolUse フック .claude/hooks/guard-git-write.py の判定のテスト。

フックは Claude Code が子プロセスとして呼ぶので、標準入力に JSON を渡して実行する（何も出さず 0 = 判断しない、
標準出力の permissionDecision が deny / ask）。差分の走査は、git を呼ぶ関数を差し替えた `decide` で確かめる。
GCP・GitHub・実際のコミットには触れない。
"""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "guard-git-write.py"
spec = importlib.util.spec_from_file_location("guard_git_write", HOOK)
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)

# テスト用に組み立てた偽の値（実在のキーではない）
FAKE_GOOGLE_KEY = "AIza" + "A" * 35


def run_hook(command: str, tool_name: str = "Bash") -> str | None:
    payload = {"tool_name": tool_name, "tool_input": {"command": command}, "cwd": str(HOOK.parent)}
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    if not proc.stdout.strip():
        return None
    return json.loads(proc.stdout)["hookSpecificOutput"]["permissionDecision"]


def no_diff(*_args: str, **_kwargs: str) -> str:
    return ""


@pytest.mark.parametrize(
    "command",
    [
        "git commit --no-verify -m x",
        "git commit -m x --no-verify",
        "git commit -nm x",
        "git -C . commit --no-verify -m x",
        "git push --no-verify",
        "git push origin main --no-verify",
    ],
)
def test_no_verify_is_denied(command: str) -> None:
    assert run_hook(command) == "deny"


@pytest.mark.parametrize(
    "command",
    [
        "git status",
        "git log --oneline -3",
        "git diff --staged",
        "git push origin main",
        "git commit -m 'fix: nothing to see'",  # 差分が空（またはこのリポジトリの通常の差分）なら通る
        "echo --no-verify",
        "ls -n",
    ],
)
def test_ordinary_commands_pass_through(command: str) -> None:
    # ステージ済みの差分があるリポジトリでは commit が拒否されうるので、commit は差分の走査を差し替えて確かめる
    if command.startswith("git commit"):
        assert guard.decide(command, ".", no_diff) is None
    else:
        assert run_hook(command) is None


def test_non_bash_tool_is_ignored() -> None:
    assert run_hook("git commit --no-verify", tool_name="Read") is None


def test_secret_in_staged_diff_is_denied_without_echoing_the_value() -> None:
    diff = f"+++ b/app/x.py\n@@ -0,0 +1,2 @@\n+ok = 1\n+KEY = '{FAKE_GOOGLE_KEY}'\n"
    result = guard.decide("git commit -m x", ".", lambda *_a: diff)
    assert result is not None and result[0] == "deny"
    assert "app/x.py:2" in result[1]
    assert FAKE_GOOGLE_KEY not in result[1]


def test_removed_lines_and_clean_diff_pass() -> None:
    diff = f"+++ b/app/x.py\n@@ -1,1 +1,1 @@\n-KEY = '{FAKE_GOOGLE_KEY}'\n+KEY = 'removed'\n"
    assert guard.decide('git commit -m "chore: x"', ".", lambda *_a: diff) is None


def test_secret_in_command_text_is_denied() -> None:
    result = guard.decide(f"git commit -m 'key {FAKE_GOOGLE_KEY}'", ".", no_diff)
    assert result is not None and result[0] == "deny"
    assert FAKE_GOOGLE_KEY not in result[1]


@pytest.mark.parametrize(
    "command", ["gh pr comment 1 --body hi", "gh issue comment 2 -b hi", "gh pr review 3 --approve"]
)
def test_gh_comments_ask(command: str) -> None:
    assert run_hook(command) == "ask"


def test_gh_read_commands_pass() -> None:
    assert run_hook("gh pr view 1") is None
    assert run_hook("gh run list --limit 3") is None
