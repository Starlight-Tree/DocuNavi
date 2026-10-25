#!/usr/bin/env python3
"""権限まわりのイベントを ~/.claude/hook-events.jsonl に 1 行ずつ記録する（切り分け用）。

記録するのは固定の項目だけ。tool_input・引数・URL・ID・キーは書かない。
Bash は git restore / git reset のときだけ記録し、コマンド本文は書かない（種別名だけ）。
決定は返さない（標準出力なし・常に exit 0）。失敗しても作業を止めない。
"""
import json
import os
import re
import sys
import time
from pathlib import Path

LOG = Path.home() / ".claude" / "hook-events.jsonl"
BASH_KINDS = re.compile(r"^\s*git\s+(restore|reset)\b")


def _tool_label(data):
    name = str(data.get("tool_name", ""))
    if name != "Bash":
        return name
    command = str((data.get("tool_input") or {}).get("command", ""))
    match = BASH_KINDS.match(command)
    return f"Bash:git-{match.group(1)}" if match else None


def main():
    data = json.load(sys.stdin)
    tool = _tool_label(data)
    if tool is None:
        return
    row = {
        "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "event": str(data.get("hook_event_name", "")),
        "tool": tool,
        "mcp_server": str(data.get("mcp_server", "")),
        "permission_mode": str(data.get("permission_mode", "")),
        "session": str(data.get("session_id", ""))[:8],
        "tool_use_id": str(data.get("tool_use_id", "")),
    }
    LOG.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(LOG, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        os.write(fd, (json.dumps(row, ensure_ascii=False) + "\n").encode("utf-8"))
    finally:
        os.close(fd)


try:
    main()
except BaseException:  # 記録の失敗で作業を止めない
    pass
sys.exit(0)
