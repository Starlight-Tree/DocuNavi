#!/usr/bin/env python3
"""コミットの件名を検査する（英語・Conventional Commits・72 文字以内）。

規則（CLAUDE.md の「Git」の節と同じ）:
  - `type(scope): 要約` の形。type は feat fix docs refactor test chore build ci perf style revert
  - 件名は英語（ASCII だけ）。日本語は件名に入れない（本文には書いてよい）
  - 72 文字以内、要約は小文字で始め、末尾にピリオドを付けない

使い方:
  scripts/check_commit_messages.py --range A..B     # A..B の新しいコミットだけを検査（CI 用）
  scripts/check_commit_messages.py --subject "..."  # 件名 1 つを検査

--range の A が無い・全部 0・解決できないとき（強制プッシュの直後など）は、B の 1 件だけを見る。
マージコミットと、`[bot]` が作者のコミット（Dependabot など）は対象外。
過去のコミットは検査しない（範囲で渡さない）。終了コード: 0 = 問題なし、1 = 違反あり、2 = 使い方の誤り。

Claude Code のフック（.claude/hooks/guard-git-write.py）と Git の commit-msg フック（.githooks/commit-msg）も、このファイルの check_subject を使う。
--message-file は、件名を取れないとき（ファイルが読めない・メッセージが空）を違反として扱う（Claude のフックは判断しないで通す）。
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

TYPES = ("feat", "fix", "docs", "refactor", "test", "chore", "build", "ci", "perf", "style", "revert")
MAX_LENGTH = 72
HEADER = re.compile(rf"^(?:{'|'.join(TYPES)})(?:\([a-z0-9][a-z0-9._/-]*\))?!?: (?P<summary>\S.*)$")
# 件名の検査を飛ばすもの（Git が作る件名）
EXEMPT_PREFIXES = ("Merge ", 'Revert "', "fixup! ", "squash! ", "amend! ")


def check_subject(subject: str) -> list[str]:
    """違反の説明の一覧（空なら問題なし）。"""
    if subject.startswith(EXEMPT_PREFIXES):
        return []
    problems: list[str] = []
    if not subject.isascii():
        problems.append("件名は英語（ASCII だけ）で書く。日本語は本文に書く")
    match = HEADER.match(subject)
    if not match:
        problems.append(f"`type(scope): summary` の形にする（type: {' '.join(TYPES)}）")
    else:
        summary = match.group("summary")
        if summary[0].isupper():
            problems.append("要約は小文字で始める")
        if summary.endswith("."):
            problems.append("末尾にピリオドを付けない")
    if len(subject) > MAX_LENGTH:
        problems.append(f"{MAX_LENGTH} 文字以内にする（今は {len(subject)} 文字）")
    return problems


def subject_from_message_file(path: str) -> str | None:
    """メッセージファイルの件名（`#` で始まる行と先頭の空行を除いた最初の行）。読めない・空なら None。"""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            lines = handle.read().splitlines()
    except OSError:
        return None
    for line in lines:
        if line.startswith("#") or not line.strip():
            continue
        return line.rstrip()
    return None


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False)


def commits_in(range_spec: str) -> list[tuple[str, str]]:
    """範囲の（短いハッシュ, 件名）。マージと bot の作者を除く。起点が使えなければ終点の 1 件だけ。"""
    start, _, end = range_spec.partition("..")
    end = end or "HEAD"
    if not start or set(start) == {"0"} or _git("rev-parse", "--verify", "--quiet", f"{start}^{{commit}}").returncode:
        spec = ["-1", end]
    else:
        spec = [f"{start}..{end}"]
    log = _git("log", "--no-merges", "--format=%h%x1f%an%x1f%s", *spec)
    if log.returncode:
        raise SystemExit(f"git log に失敗しました: {log.stderr.strip()}")
    rows = []
    for line in log.stdout.splitlines():
        short, author, subject = line.split("\x1f", 2)
        if not author.endswith("[bot]"):
            rows.append((short, subject))
    return rows


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--range", dest="range_spec", help="A..B（新しいコミットだけを検査する）")
    group.add_argument("--subject", help="件名 1 つを検査する")
    group.add_argument("--message-file", help="コミットメッセージのファイルの件名を検査する（取れなければ違反）")
    args = parser.parse_args()

    if args.message_file is not None:
        subject = subject_from_message_file(args.message_file)
        if subject is None:
            print(f"件名を読み取れません: {args.message_file}")
            return 1
        rows = [("-", subject)]
    elif args.subject is not None:
        rows = [("-", args.subject)]
    else:
        rows = commits_in(args.range_spec)
    failed = 0
    for short, subject in rows:
        problems = check_subject(subject)
        if problems:
            failed += 1
            print(f"{short} {subject}")
            for problem in problems:
                print(f"    - {problem}")
    print(f"検査したコミット: {len(rows)} 件、違反: {failed} 件")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
