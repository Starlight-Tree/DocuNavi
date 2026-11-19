"""PreToolUse フック .claude/hooks/guard-foreground-wait.py の判定のテスト。

CI の完了待ち（`gh run watch`・待ちループ内の `gh run`）を前景で止め、`run_in_background` が真なら通す。
フックを子プロセスとして標準入力の JSON で呼ぶ。GitHub には触れない。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "guard-foreground-wait.py"


def run_hook(command: str, *, background: bool = False, tool_name: str = "Bash") -> str | None:
    tool_input: dict[str, object] = {"command": command}
    if background:
        tool_input["run_in_background"] = True
    payload = {"tool_name": tool_name, "tool_input": tool_input}
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    if not proc.stdout.strip():
        return None
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["permissionDecision"] == "deny"
    return out["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "gh run watch 123 --exit-status",
        "id=$(gh run list --limit 1 --json databaseId --jq '.[0].databaseId'); gh run watch $id",
        "timeout 280 gh run watch 123 --exit-status >/dev/null 2>&1; echo exit=$?",
        "cd /workspaces/docunavi && gh run watch 1",
        "until gh run view 1 --json conclusion | grep -q success; do sleep 5; done",
        "while ! gh run view 1 | grep -q completed; do sleep 5; done",
    ],
)
def test_foreground_ci_waits_are_denied_with_a_fix(command):
    reason = run_hook(command)
    assert reason is not None and "run_in_background" in reason


@pytest.mark.parametrize(
    "command",
    [
        "gh run watch 123 --exit-status",
        "until gh run view 1 --json conclusion | grep -q success; do sleep 5; done",
    ],
)
def test_the_same_commands_pass_in_the_background(command):
    assert run_hook(command, background=True) is None


@pytest.mark.parametrize(
    "command",
    [
        "gh run list --limit 3",
        "gh run view 123 --log-failed",
        "gh pr view 5",
        "git log --oneline origin/main..HEAD",
        "echo watch the run",
        "ls .github/workflows",
        'git commit -m "deny gh run watch in the foreground"',
        "echo 'until gh run view 1; do sleep 5; done'",
    ],
)
def test_reads_and_other_commands_are_not_touched(command):
    assert run_hook(command) is None


def test_other_tools_and_broken_input_are_ignored():
    assert run_hook("gh run watch 1", tool_name="Monitor") is None
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input="not json", capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0 and proc.stdout == ""
