"""規程の PDF のテキストを、章・条・項・脚注に分ける（正規表現だけ。LLM は使わない）。

条は「条番号 + 本文のハッシュ」で識別する（本文が変われば別の版として見分けられる）。
項は、条の本文を第 1 項、行頭の「2 」「3 」…（直前の項番号 + 1 のときだけ）を第 2 項以降とする。
行は折り返されるので、続きの行は区切りなしでつなぐ（日本語。行頭の空白は元の本文の空白なので残す）。
"""

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass

from app.ingestion.pdf_parser import Page

_CHAPTER = re.compile(r"^第(\d+)章\s*(.*)$")
_ARTICLE = re.compile(r"^第(\d+)条\s+(.*)$")
_PARAGRAPH = re.compile(r"^(\d+)\s+(.*)$")
_PAGE_NUMBER = re.compile(r"^\d+ / \d+$")
_FOOTNOTE_MARK = re.compile(r"^(.*。)(\d+)$")


@dataclass(frozen=True)
class Paragraph:
    id: str  # a13p2
    article: int
    number: int  # 項番号（本文が 1）
    text: str  # 脚注の番号を除いた本文
    raw: str  # 脚注の番号を含む、PDF のままの本文
    page: int  # この項が始まるページ
    footnote: int | None = None


@dataclass(frozen=True)
class Article:
    number: int
    chapter: int
    heading: str  # 条の見出し行（「第4条 入寮できる者は、…」）
    paragraphs: tuple[Paragraph, ...]
    page: int
    text_hash: str  # 項の本文を改行でつないだものの sha256

    @property
    def id(self) -> str:
        return f"a{self.number}"


@dataclass(frozen=True)
class Chapter:
    number: int
    title: str
    heading: str  # 「第2章 入寮」

    @property
    def id(self) -> str:
        return f"ch{self.number}"


@dataclass(frozen=True)
class Footnote:
    number: int
    text: str
    page: int

    @property
    def id(self) -> str:
        return f"fn{self.number}"


@dataclass(frozen=True)
class Regulation:
    title: str | None
    chapters: tuple[Chapter, ...]
    articles: tuple[Article, ...]
    footnotes: tuple[Footnote, ...]
    supplementary: str


def _body_lines(pages: Sequence[Page]) -> tuple[str | None, list[tuple[int, str]]]:
    """ページ番号・全ページに繰り返される脚の行を除いた (ページ, 行) の並びと、1 ページ目の先頭行（題名）。"""
    per_page = [[ln.rstrip() for ln in page.text.splitlines() if ln.strip()] for page in pages]
    footers: set[str] = set()
    if len(per_page) >= 2:
        # 各ページの末尾（ページ番号を除いた最後の行）が全ページで同じなら、走っている脚とみなす
        tails = []
        for lines in per_page:
            body = [ln for ln in lines if not _PAGE_NUMBER.match(ln)]
            tails.append(body[-1] if body else None)
        if tails[0] is not None and all(t == tails[0] for t in tails):
            footers.add(tails[0])
    title = None
    out: list[tuple[int, str]] = []
    for page, lines in zip(pages, per_page, strict=True):
        for i, ln in enumerate(lines):
            if _PAGE_NUMBER.match(ln) or ln in footers:
                continue
            if page is pages[0] and i == 0 and not _CHAPTER.match(ln) and not _ARTICLE.match(ln):
                title = ln
                continue
            out.append((page.number, ln))
    return title, out


def parse_regulation(pages: Sequence[Page]) -> Regulation:
    title, lines = _body_lines(pages)

    chapters: list[Chapter] = []
    # 項の素材: (条番号, 章, 項番号, 行の断片のリスト, 始まるページ)
    raw_articles: list[dict] = []
    raw_notes: list[dict] = []
    supplementary: list[str] = []
    mode = "body"
    cur_art: dict | None = None
    cur_para: dict | None = None
    cur_note: dict | None = None
    cur_chapter = 0

    for page, ln in lines:
        if mode == "body":
            m = _CHAPTER.match(ln)
            if m:
                cur_chapter = int(m.group(1))
                chapters.append(Chapter(cur_chapter, m.group(2).strip(), ln.strip()))
                cur_art = cur_para = None
                continue
            if ln.strip() == "附則":
                mode, cur_art, cur_para = "supp", None, None
                continue
            if ln.strip() == "脚注":
                mode, cur_art, cur_para = "notes", None, None
                continue
            m = _ARTICLE.match(ln)
            last = raw_articles[-1]["number"] if raw_articles else 0
            if m and int(m.group(1)) == last + 1:
                cur_art = {"number": int(m.group(1)), "chapter": cur_chapter, "heading": ln, "page": page, "paras": []}
                cur_para = {"number": 1, "parts": [m.group(2)], "page": page}
                cur_art["paras"].append(cur_para)
                raw_articles.append(cur_art)
                continue
            m = _PARAGRAPH.match(ln)
            if m and cur_art is not None and cur_para is not None and int(m.group(1)) == cur_para["number"] + 1:
                cur_para = {"number": int(m.group(1)), "parts": [m.group(2)], "page": page}
                cur_art["paras"].append(cur_para)
                continue
            if cur_para is not None:
                cur_para["parts"].append(ln)
        elif mode == "supp":
            if ln.strip() == "脚注":
                mode = "notes"
            else:
                supplementary.append(ln)
        else:  # notes
            m = _PARAGRAPH.match(ln)
            expected = len(raw_notes) + 1
            if m and int(m.group(1)) == expected:
                cur_note = {"number": expected, "parts": [m.group(2)], "page": page}
                raw_notes.append(cur_note)
            elif cur_note is not None:
                cur_note["parts"].append(ln)

    notes = tuple(Footnote(n["number"], "".join(n["parts"]), n["page"]) for n in raw_notes)
    note_numbers = {n.number for n in notes}

    articles: list[Article] = []
    for ra in raw_articles:
        paragraphs: list[Paragraph] = []
        for rp in ra["paras"]:
            raw = "".join(rp["parts"])
            text, mark = raw, None
            m = _FOOTNOTE_MARK.match(raw)
            if m and int(m.group(2)) in note_numbers:
                text, mark = m.group(1), int(m.group(2))
            paragraphs.append(
                Paragraph(f"a{ra['number']}p{rp['number']}", ra["number"], rp["number"], text, raw, rp["page"], mark)
            )
        digest = hashlib.sha256("\n".join(p.text for p in paragraphs).encode()).hexdigest()
        articles.append(Article(ra["number"], ra["chapter"], ra["heading"], tuple(paragraphs), ra["page"], digest))

    return Regulation(title, tuple(chapters), tuple(articles), notes, "".join(supplementary))
