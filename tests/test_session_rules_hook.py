"""SessionStart フック（.claude/hooks/session-rules.py）の出力を確かめる。"""

import json
import subprocess
import sys
from pathlib import Path

HOOK = Path(__file__).resolve().parent.parent / ".claude" / "hooks" / "session-rules.py"


def _run() -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", str(HOOK)],
        input="{}",
        capture_output=True,
        text=True,
        timeout=10,
    )


def test_outputs_session_start_context_json():
    proc = _run()
    assert proc.returncode == 0
    out = json.loads(proc.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "SessionStart"
    assert out["additionalContext"].strip()


def test_context_is_short():
    text = json.loads(_run().stdout)["hookSpecificOutput"]["additionalContext"]
    assert len(text) <= 1200
    assert len(text.splitlines()) <= 10


def test_context_keeps_the_key_rules():
    text = json.loads(_run().stdout)["hookSpecificOutput"]["additionalContext"]
    for word in ("承認", "課金", "MCP", "キー", "push"):
        assert word in text
