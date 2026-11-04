"""生成した本文が従う Markdown の小集合（見出し # ## ###・段落・箇条書き・表・脚注・`**太字**`）。

検証（validate.py）は `###`・`**` を「小集合の外」として落とすが、仕上げ（Claude が読んで整える）で受け入れた本文には残りうるので、
組版（render.py）は記号のまま出さず、見出し・太字・箇条書きとして描く。

`evals/corpora/cafe/source/make_pdf.py` と同じ集合にしてある（PDF の組版を同じ形で再利用するため）。
検証（validate.py）と組版（render.py）が同じ解析を使う。reportlab には依存しない。
"""

from __future__ import annotations

import re

_FOOTNOTE_DEF = re.compile(r"^\[\^\d+\]:")
_BULLET = re.compile(r"^(\s*)[*\-+]\s+(.*)$")
_H3 = re.compile(r"^#{3,}\s+(.*)$")
_BOLD = re.compile(r"\*\*(.+?)\*\*")
_TABLE_SEPARATOR_CHARS = set("-: ")


def parse(md: str) -> list[tuple[str, object]]:
    """('h1'|'h2'|'h3'|'p'|'li'|'li2'|'cont'|'table'|'footnote', 値) の列にする。表の値は行（セルの文字列の列）のリスト。

    li は箇条書き、li2 は字下げされた箇条書き、cont は字下げされた続きの行（直前の項目の一部）。
    """
    blocks: list[tuple[str, object]] = []
    table: list[list[str]] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not set("".join(cells)) <= _TABLE_SEPARATOR_CHARS:  # 区切り行（|---|---|）は捨てる
                table.append(cells)
            continue
        if table:
            blocks.append(("table", table))
            table = []
        if not line:
            continue
        bullet = _BULLET.match(line)
        h3 = _H3.match(line)
        if h3:
            blocks.append(("h3", h3.group(1)))
        elif bullet and not line.startswith("**"):
            blocks.append(("li2" if len(bullet.group(1)) >= 2 else "li", bullet.group(2)))
        elif raw[:1].isspace() and blocks and blocks[-1][0] in {"li", "li2", "cont", "p"}:
            blocks.append(("cont", line.strip()))
        elif line.startswith("## "):
            blocks.append(("h2", line[3:]))
        elif line.startswith("# "):
            blocks.append(("h1", line[2:]))
        elif _FOOTNOTE_DEF.match(line):
            blocks.append(("footnote", line))
        else:
            blocks.append(("p", line))
    if table:
        blocks.append(("table", table))
    return blocks


def title_of(md: str) -> str | None:
    """最初の `# ` 見出し。無ければ None。"""
    for kind, value in parse(md):
        if kind == "h1":
            return str(value)
    return None


def strip_bold(text: str) -> str:
    return _BOLD.sub(r"\1", text)


def plain_text(md: str) -> str:
    """検証用に、記号を除いた本文だけにする（見出し・段落・箇条書き・表のセル・脚注を、改行でつなぐ）。"""
    lines: list[str] = []
    for kind, value in parse(md):
        if kind == "table":
            for row in value:  # type: ignore[union-attr]
                lines.append(strip_bold(" ".join(row)))
        else:
            lines.append(strip_bold(str(value)))
    return "\n".join(lines)
