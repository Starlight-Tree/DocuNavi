"""生成した本文が従う Markdown の小集合（見出し # ##・段落・表・脚注だけ）。

`evals/corpora/cafe/source/make_pdf.py` と同じ集合にしてある（PDF の組版を同じ形で再利用するため）。
検証（validate.py）と組版（render.py）が同じ解析を使う。reportlab には依存しない。
"""

from __future__ import annotations

import re

_FOOTNOTE_DEF = re.compile(r"^\[\^\d+\]:")
_TABLE_SEPARATOR_CHARS = set("-: ")


def parse(md: str) -> list[tuple[str, object]]:
    """('h1'|'h2'|'p'|'table'|'footnote', 値) の列にする。表の値は行（セルの文字列の列）のリスト。"""
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
        if line.startswith("## "):
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


def plain_text(md: str) -> str:
    """検証用に、記号を除いた本文だけにする（見出し・段落・表のセル・脚注を、改行でつなぐ）。"""
    lines: list[str] = []
    for kind, value in parse(md):
        if kind == "table":
            for row in value:  # type: ignore[union-attr]
                lines.append(" ".join(row))
        else:
            lines.append(str(value))
    return "\n".join(lines)
