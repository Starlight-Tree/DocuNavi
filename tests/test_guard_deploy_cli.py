"""PreToolUse フック .claude/hooks/guard-deploy-cli.py の判定のテスト。

wrangler・infisical・prisma の書き込み系を「確認（ask）」に回し、読み取り・ローカルは何も出さない（0 = 判断しない）。
フックを子プロセスとして標準入力の JSON で呼ぶ。Cloudflare・Infisical・DB には触れない。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "guard-deploy-cli.py"


def run_hook(command: str, tool_name: str = "Bash") -> tuple[str | None, str]:
    payload = {"tool_name": tool_name, "tool_input": {"command": command}}
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input=json.dumps(payload), capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0
    if not proc.stdout.strip():
        return None, ""
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    return out["permissionDecision"], out["permissionDecisionReason"]


@pytest.mark.parametrize(
    "command",
    [
        "wrangler deploy",
        "npx wrangler deploy --name docunavi-gcp-web",
        "cd /tmp && npx wrangler@4.158.0 deploy",
        "pnpm exec wrangler secret put GEMINI_API_KEY",
        "wrangler secret delete X",
        "wrangler delete --name x",
        "wrangler rollback",
        "wrangler versions deploy",
        "wrangler d1 execute db --remote --command 'select 1'",
        "wrangler d1 migrations apply db",
        "wrangler r2 object put bucket/key --file x",
        "wrangler kv key put k v --namespace-id x",
        "wrangler login",
        "echo ok && wrangler deploy",
        "bash -c 'wrangler deploy'",
        'sh -c "npx wrangler secret put X"',
        "infisical secrets set A=1",
        "infisical secrets delete A",
        "infisical secrets",
        "infisical secrets get A",
        "infisical export",
        "infisical login",
        "infisical init",
        "prisma migrate deploy",
        "npx prisma migrate reset",
        "prisma db push",
        "prisma db execute --file x.sql",
    ],
)
def test_asks_for_writing_commands(command: str) -> None:
    decision, reason = run_hook(command)
    assert decision == "ask"
    assert reason.startswith("guard-deploy-cli:")


@pytest.mark.parametrize(
    "command",
    [
        "wrangler dev",
        "npx wrangler dev --local --port 8787",
        "wrangler whoami",
        "wrangler types",
        "wrangler tail",
        "wrangler --version",
        "wrangler deployments list",
        "wrangler versions list",
        "wrangler secret list",
        "wrangler d1 list",
        "wrangler d1 execute db --local --command 'select 1'",
        "wrangler kv key list --namespace-id x",
        "wrangler r2 object get bucket/key",
        "infisical run -- python script.py",
        "infisical --version",
        "prisma generate",
        "prisma validate",
        "prisma migrate status",
        "git status",
        "ls",
        "echo wrangler deploy",
    ],
)
def test_does_not_interfere_with_read_only_or_unrelated(command: str) -> None:
    decision, _ = run_hook(command)
    assert decision is None


def test_reason_never_contains_argument_values() -> None:
    _, reason = run_hook("infisical secrets set TOP_SECRET_NAME=hunter2-value")
    assert "hunter2" not in reason
    _, reason = run_hook("wrangler secret put SOME_NAME")
    assert "SOME_NAME" not in reason


@pytest.mark.parametrize("tool_name", ["Read", "Edit", "mcp__github__actions_get"])
def test_ignores_non_bash_tools(tool_name: str) -> None:
    assert run_hook("wrangler deploy", tool_name) == (None, "")


def test_broken_input_does_not_block() -> None:
    proc = subprocess.run(
        [sys.executable, "-I", str(HOOK)], input="not json", capture_output=True, text=True, timeout=30
    )
    assert proc.returncode == 0 and not proc.stdout.strip()
