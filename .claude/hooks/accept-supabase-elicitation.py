#!/usr/bin/env python3
import json, os, sys, time

ENFORCE = False  # 入力の形は確認済み（match: true）。条件に合う要求だけ accept を返す
LOG = os.path.expanduser("~/.claude/elicitation-hook.log")
PREFIX = "This SQL includes destructive operations"


def log(entry):
    entry["at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def main():
    data = json.load(sys.stdin)
    server = data.get("mcp_server_name") or data.get("server_name")
    message = data.get("message") or ""
    ref = os.environ.get("SUPABASE_PROJECT_REF", "")
    ok = (
        server == "supabase"
        and data.get("mode", "form") == "form"
        and message.startswith(PREFIX)
        and bool(ref)
        and ref in message
    )
    log({"keys": sorted(data), "server": server, "match": ok, "enforce": ENFORCE})
    if ok and ENFORCE:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": "Elicitation", "action": "accept"}}))


try:
    main()
except Exception as e:
    log({"error": type(e).__name__})
