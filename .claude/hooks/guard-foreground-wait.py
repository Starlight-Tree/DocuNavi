#!/usr/bin/env python3
"""PreToolUse(Bash) の小さなガード: CI の完了待ちを、前景で行わせない（`.claude/rules/parallel-work.md`）。

前景で止めるもの（`run_in_background` が真のときは通す）:
  - `gh run watch`（`timeout N gh run watch` なども）
  - `until` / `while` の待ちループの中の `gh run`

CI・デプロイの完了待ちは、`run_in_background: true` で走らせ、完了の通知を待つ間に次の独立した作業を進める。
これはセキュリティのガードではなく、作業の進め方の矯正なので、`deny`（理由に、直し方を書く）。
引用符の中（コミットメッセージ・echo の文言など）は見ない。そのため、引用符で包んだ `bash -c '…'` の中身は止められない。
限界: コマンド文字列の解析だけ。別名・スクリプト経由の待ちは防げない。
"""

from __future__ import annotations

import json
import re
import sys

WATCH = re.compile(r"(?:^|[\s;&|(])gh\s+run\s+watch\b")
LOOP_WAIT = re.compile(r"\b(?:until|while)\b[^\n]*\bgh\s+run\b")
QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")

REASON = (
    "CI の完了待ちは、前景で行わない（.claude/rules/parallel-work.md）。"
    "`run_in_background: true` で実行し、完了の通知を待つ間に次の独立した作業を進める。"
)


def decide(tool_input: dict[str, object]) -> str | None:
    """止めるときは理由、通すときは None。"""
    if tool_input.get("run_in_background") is True:
        return None
    command = tool_input.get("command")
    if not isinstance(command, str):
        return None
    command = QUOTED.sub("''", command)  # 引用符の中の文言には反応しない
    if WATCH.search(command) or LOOP_WAIT.search(command):
        return REASON
    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except json.JSONDecodeError:
        return 0
    if payload.get("tool_name") != "Bash":
        return 0
    tool_input = payload.get("tool_input")
    reason = decide(tool_input) if isinstance(tool_input, dict) else None
    if reason:
        json.dump(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            },
            sys.stdout,
            ensure_ascii=False,
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
