#!/usr/bin/env python3
"""Markdown の「死んだ参照」を検査する（標準ライブラリだけ）。

AI のコーディングエージェントは docs・rules・CLAUDE.md を読んで動く。消えたファイルや見出しを指す参照は、誤った手がかりになる。

検査:
  1. 相対リンク `[text](target)`: target が存在すること（http(s)・mailto・`#` だけのものは対象外。アンカーは見ない）
  2. バッククォートのパス `app/x.py`（`:123` `:12-34` 付きも可）: 先頭が TOP_DIRS のどれかで、末尾が拡張子つきか `/` のとき、存在すること
  3. 節の参照 `docs/foo.md の「見出し」`（`[foo.md](…) の「見出し」`、一意に決まる `foo.md` も可）: その見出し行に引用の文字列があること
  コードフェンス（```）の中は 2・3 を見ない。`check-docs: ignore` を含む行は丸ごと飛ばす。

使い方:
  python3 scripts/check_docs.py [ファイル...]   # 引数なしは既定の対象（default_files）
終了コード: 0 = 問題なし、1 = 問題あり。1 件ごとに `パス:行: 説明` を標準出力へ出す。
"""

import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parent.parent
IGNORE_MARK = "check-docs: ignore"
# 意図して残す歴史的な参照（書かれたとおりの文字列。例: "docs/old-plan.md"）。
# 文書の側が「削除済み」「別のリポジトリ」「当時の記録」と書いているものだけを入れる。ただの書き漏れ・消し忘れは、ここへ入れず文書を直す。
IGNORE_TARGETS: set[str] = {
    # 2026-11-08 に廃止して削除した運用者向けの管理画面（ADR 0003）
    "ops/access_admin/",
    # 2026-11-08 に撤去した D1 のマイグレーション（Postgres は db/migrations/）
    "workers/api/migrations/",
    "workers/api/migrations/0001_schema.sql",
    # 星庭ジョブ（別のリポジトリ）の台帳と確認スクリプト
    "docs/image-gen-resources.md",
    "scripts/inspect-image-gen.sh",
    # 2026-11-08 の d693e86 などで削除した。文書は「当時の記録」として残している
    "frontend/scripts/generate-images/",
    "tests/test_history_postgres.py",
    "scripts/eval_answers.py",
    "scripts/eval_search_compare.py",
    # evals/fixtures/exact/ へ移した旧置き場
    "evals/corpora/exact/",
}
TOP_DIRS = (
    "app",
    "db",
    "docs",
    "scripts",
    "tests",
    "frontend",
    "workers",
    "ops",
    "evals",
    ".claude",
    ".github",
    ".devcontainer",
)
# CLAUDE.md / README.md を集める領域（深さ 2 まで。例: ops/sse-relay/CLAUDE.md）
AREA_DIRS = ("app", "db", "tests", "frontend", "evals", "workers", "ops")
AREA_FILES = ("CLAUDE.md", "README.md")

CODE_SPAN = re.compile(r"`([^`\n]+)`")
LINK = re.compile(r"\[[^\]]*\]\(\s*<?([^)\s>]+)>?(?:\s+[^)]*)?\)")
# バッククォートのパス: 空白なし。`:12` `:12-34` `::pytest の名前` の接尾辞は外して調べる
PATH_SPAN = re.compile(r"^(?P<path>[\w.@+-][\w.@+/-]*?)(?::\d+(?:-\d+)?|::\S+)?$")
EXTENSION = re.compile(r"\.[A-Za-z][A-Za-z0-9]{0,9}$")
PLACEHOLDER = re.compile(r"[*<>…{}$]|N{3,}|X{3,}|YYYY|\.\.\.", re.IGNORECASE)
SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# `docs/foo.md` の「見出し」 / [foo.md](…) の「見出し」
SECTION_REF = re.compile(
    r"(?:\[[^\]]*\]\((?P<link>[^)\s]+)\)|`(?P<code>[^`\n]+)`)\s*の「(?P<quote>[^」\n]+)」(?P<after>.{0,2})"
)
HEADING = re.compile(r"^ {0,3}#{1,6}\s+(.*)$")
# 太字で始まる行（`- **監視設定:** …`）も、見出しと同じ「節の名前」として扱う（この repo は箇条書きの見出し語を節として参照する）
BOLD_LEAD = re.compile(r"^\s*(?:>\s*)*(?:[-*+]|\d+\.)?\s*\*\*(.+?)\*\*")
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")


def is_skipped(rel: Path) -> bool:
    parts = rel.parts
    if parts[:2] == ("evals", "corpora"):
        return True
    return any(p in ("node_modules", "storage") or p.startswith((".venv", ".next")) for p in parts)


def default_files(root: Path) -> list[Path]:
    """既定の検査対象（root は絶対パス）。"""
    found: set[Path] = set(root.glob("*.md"))
    for pattern in ("docs/**/*.md", ".claude/**/*.md", ".devcontainer/*.md"):
        found.update(root.glob(pattern))
    for area in AREA_DIRS:
        for name in AREA_FILES:
            found.update(root.glob(f"{area}/{name}"))
            found.update(root.glob(f"{area}/*/{name}"))
    return sorted(p for p in found if p.is_file() and not is_skipped(p.relative_to(root)))


def body_lines(text: str):
    """(行番号, 行, コードフェンスの中か) を返す。"""
    fence = ""
    for no, line in enumerate(text.splitlines(), 1):
        match = FENCE.match(line)
        if match and not fence:
            fence = match.group(1)
            yield no, line, True
        elif match and fence and match.group(1).startswith(fence) and line.strip() == match.group(1):
            fence = ""
            yield no, line, True
        else:
            yield no, line, bool(fence)


class Checker:
    def __init__(self, root: Path):
        self.root = root
        self._headings: dict[Path, list[str]] = {}
        self._by_name: dict[str, list[Path]] | None = None

    def rel(self, path: Path) -> str:
        try:
            return path.relative_to(self.root).as_posix()
        except ValueError:
            return path.as_posix()

    def exists(self, target: Path) -> bool:
        """存在する、または Git の管理外（.gitignore）のファイル（`.env` など。クローン直後には無い）。"""
        if target.exists():
            return True
        try:
            rel = target.relative_to(self.root).as_posix()
            ignored = subprocess.run(["git", "-C", str(self.root), "check-ignore", "-q", rel], capture_output=True)
        except (ValueError, OSError):
            return False
        return ignored.returncode == 0

    def headings(self, path: Path) -> list[str]:
        if path not in self._headings:
            text = path.read_text(encoding="utf-8", errors="replace")
            found = [HEADING.match(line) or BOLD_LEAD.match(line) for _, line, fenced in body_lines(text) if not fenced]
            self._headings[path] = [norm(m.group(1)) for m in found if m]
        return self._headings[path]

    def markdown_named(self, name: str) -> Path | None:
        """既定の対象の中で、その名前のファイルが 1 つだけならそれ（複数・無しは None）。"""
        if self._by_name is None:
            self._by_name = {}
            for p in default_files(self.root):
                self._by_name.setdefault(p.name, []).append(p)
        hits = self._by_name.get(name, [])
        return hits[0] if len(hits) == 1 else None

    def check_file(self, path: Path) -> list[str]:
        problems: list[str] = []
        label = self.rel(path)
        text = path.read_text(encoding="utf-8", errors="replace")
        for no, line, fenced in body_lines(text):
            if fenced or IGNORE_MARK in line:
                continue
            for msg in self.check_links(path, line) + self.check_paths(path, line) + self.check_sections(path, line):
                problems.append(f"{label}:{no}: {msg}")
        return problems

    def link_target(self, path: Path, raw: str) -> Path | None:
        """リンクの相手（相対リンクでないもの・無視するものは None）。"""
        target = unquote(raw.split("#", 1)[0].split("?", 1)[0])
        if not target or SCHEME.match(raw) or PLACEHOLDER.search(target) or raw in IGNORE_TARGETS:
            return None
        base = self.root / target.lstrip("/") if target.startswith("/") else path.parent / target
        return Path(os.path.normpath(base))  # `docs/../README.md` を `README.md` にそろえる

    def check_links(self, path: Path, line: str) -> list[str]:
        out = []
        for match in LINK.finditer(CODE_SPAN.sub("", line)):
            target = self.link_target(path, match.group(1))
            if target and not self.exists(target):
                out.append(f"リンク先が存在しません: {match.group(1)}")
        return out

    def check_paths(self, path: Path, line: str) -> list[str]:
        out = []
        for span in CODE_SPAN.finditer(line):
            text = span.group(1)
            match = PATH_SPAN.match(text)
            if not match or PLACEHOLDER.search(text):
                continue
            rel = match.group("path")
            parts = rel.split("/")
            if parts[0] not in TOP_DIRS or len(parts) < 2:
                continue
            if not (rel.endswith("/") or EXTENSION.search(parts[-1])) or rel in IGNORE_TARGETS:
                continue
            # frontend/CLAUDE.md の `app/x.tsx` のように、そのファイルのあるディレクトリーからの相対も認める
            if not self.exists(self.root / rel) and not (path.parent / rel).exists():
                out.append(f"パスが存在しません: {rel}")
        return out

    def section_file(self, path: Path, link: str | None, code: str | None) -> Path | None:
        """節の参照の相手のファイル（決まらない・Markdown でないときは None）。"""
        if link is not None:
            target = self.link_target(path, link)
        elif code and PATH_SPAN.match(code) and code.endswith(".md") and code not in IGNORE_TARGETS:
            if "/" in code:
                target = self.root / code if code.split("/")[0] in TOP_DIRS else None
            else:
                target = self.markdown_named(code)
        else:
            target = None
        return target if target and target.suffix == ".md" and target.is_file() else None

    def check_sections(self, path: Path, line: str) -> list[str]:
        out = []
        for match in SECTION_REF.finditer(line):
            quote = match.group("quote")
            if "…" in quote or match.group("after").startswith("など"):
                continue
            target = self.section_file(path, match.group("link"), match.group("code"))
            if target and not any(norm(quote) in h for h in self.headings(target)):
                out.append(f"{self.rel(target)} に見出し「{quote}」がありません")
        return out


def norm(text: str) -> str:
    """見出しの比較用（バッククォートを外し、空白をそろえる）。"""
    return " ".join(text.replace("`", "").split())


def check_files(root: Path, files: list[Path] | None = None) -> list[str]:
    """root 以下の files（None なら既定の対象）を検査し、`パス:行: 説明` の一覧を返す。"""
    checker = Checker(root)
    problems: list[str] = []
    for path in default_files(root) if files is None else files:
        if not path.is_file():
            problems.append(f"{checker.rel(path)}:0: ファイルが存在しません")
        else:
            problems.extend(checker.check_file(path))
    return problems


def main(argv: list[str] | None = None, root: Path = ROOT) -> int:
    args = sys.argv[1:] if argv is None else argv
    # 相対パスの引数は、実行したディレクトリーからの相対（Markdown 以外は黙って飛ばす）
    files = [Path.cwd() / a for a in args if a.endswith(".md")] if args else None
    if args and not files:
        return 0
    problems = check_files(root, files)
    for problem in problems:
        print(problem)
    if problems:
        print(f"死んだ参照: {len(problems)} 件", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
