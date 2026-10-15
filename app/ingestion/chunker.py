"""ページを、検索の単位（チャンク）に分ける。

座標から組み直した行（`Page.lines`）があれば、それを使う。無ければ、通常の抽出テキストの行を使う（第 1 版と同じ）。

**座標がある場合**

- 縦の間隔で段落に分ける（行の間隔の基準＝第 1 四分位より明らかに広いところが段落の切れ目）。
  間隔に変化が無いページは、文末で終わる行の後ろと、短くて文末が無い行の前後で切る。
- 段落は 1 つのチャンクにする。折り返された行はつなぎ、文は `\\n` で区切る（検索側が改行を文の区切りに使うため）。
- 見出し（文字が大きい、または短くて文末が無い 1 行）は、チャンクにせず、後ろのチャンクの `heading` にする。
- 間が空いたセルが 2 つ以上ある行が続いたら表とみなす。表は、行ごとに改行で区切った 1 つのチャンク（`kind="table"`）にする
  （見出し行つき）。**ページをまたいでも、全体が `HARD_LIMIT_CHARS` に収まるなら 1 つにまとめる**（`page` は先頭、`page_end` は最終ページ）。
  続きのページで見出し行が繰り返されていれば、重ねない。収まらない表だけを行の範囲で分け、見出し行を繰り返す。
  表のすぐ後に、同じ列数の表が次のページの先頭から続くときだけ、続きとみなす（間に段落や別の表があれば別の表）。
- ページ番号（「3 / 4」など）は捨てる。

**共通**

- 段落のチャンクは 1 ページの中に収める（`page` が正確になる）。表だけが、ページをまたぎうる（`page_end`）。
- 長さで詰めて大きなチャンクにはしない。詰める方式は、段落を 1 つ書き換えて文字数が変わっただけで、後ろの切れ目がずれ、
  変わっていないチャンクまで「変更」になる（テストで確かめた）。段落単位なら、書き換えた段落のチャンクだけが変わる。
  段落の挿入・削除でも、増減するのはその段落のチャンクだけで、ほかのチャンクの内容（ハッシュ）は変わらない（番号は 1 つずれる）。
- 長すぎる段落だけ、文の切れ目で分ける（`HARD_LIMIT_CHARS`）。

**限界（まだやっていない）**

- 2 段組みは、段をまたいだ行が表に見えることがある。
- 太字だけで大きさが同じ見出しは、短くて文末が無い 1 行のときだけ見出しになる。
- 見出しの階層（章・節）は持たず、直前の見出しだけを `heading` にする。
- 埋め込みに向く大きさ（見出しの下の段落をまとめる）は、まだ決めていない。
"""

import hashlib
import re
import statistics
import unicodedata
from dataclasses import dataclass

from app.ingestion.pdf_parser import Line, Page

HARD_LIMIT_CHARS = 1600

_SENTENCE_END = ("。", "．", "！", "？", "!", "?", "」", "』", "）", ")")
# 行の間隔がこの倍率を超えたら、段落の切れ目とみなす（行の間隔の基準に対して）
_PARAGRAPH_GAP = 1.25
# 本文の文字の大きさに対して、これ以上大きい 1 行は見出し
_HEADING_SIZE = 1.05
# 短くて文末が無い 1 行を、見出しとみなす長さの上限
_HEADING_MAX_CHARS = 30

_PAGE_NUMBER = re.compile(r"^(?:[-–—]\s*)?\d+\s*(?:/\s*\d+)?(?:\s*[-–—])?$")


@dataclass(frozen=True)
class Chunk:
    index: int  # 文書の中での順序（0 始まり）
    page: int  # 先頭のページ
    heading: str | None
    text: str
    content_hash: str
    page_end: int | None = None  # 表がページをまたぐときの最終ページ（またがなければ None）
    kind: str = "text"  # text（段落）または table（表。1 行 1 行を改行で区切る）


def normalize_for_hash(text: str) -> str:
    """ハッシュの前に揃える。全角半角と空白の揺れだけでは、別のチャンクにならないようにする。"""
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", text)).strip()


def content_hash(text: str) -> str:
    return hashlib.sha256(normalize_for_hash(text).encode("utf-8")).hexdigest()


# ---- 座標が無いとき（通常の抽出テキストだけ）----


def _split_lines(lines: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for line in lines:
        current.append(line)
        size += len(line) + 1
        if line.endswith(_SENTENCE_END) or size >= HARD_LIMIT_CHARS:
            groups.append(current)
            current, size = [], 0
    if current:
        groups.append(current)
    return groups


# ---- 座標があるとき ----


def _is_page_number(line: Line) -> bool:
    return bool(_PAGE_NUMBER.match(line.text.strip()))


def _sentences(paragraph: str) -> list[str]:
    parts = re.split(r"(?<=[。！？!?])(?![」』）)])", paragraph)
    return [p.strip() for p in parts if p.strip()]


def _join_wrapped(lines: list[str]) -> str:
    """折り返された行をつなぐ。日本語は空白なしで、英数字どうしが接するときだけ空白を入れる。"""
    text = ""
    for line in lines:
        if text and text[-1].isascii() and text[-1].isalnum() and line[:1].isascii() and line[:1].isalnum():
            text += " "
        text += line
    return text


def _cap(sentences: list[str]) -> list[list[str]]:
    groups: list[list[str]] = []
    current: list[str] = []
    size = 0
    for sentence in sentences:
        if current and size + len(sentence) + 1 > HARD_LIMIT_CHARS:
            groups.append(current)
            current, size = [], 0
        current.append(sentence)
        size += len(sentence) + 1
    if current:
        groups.append(current)
    return groups


def _is_table_row(line: Line) -> bool:
    return len(line.cells) >= 2


def _is_heading(block: list[Line], body_size: float) -> bool:
    if body_size > 0 and all(
        line.size >= body_size * _HEADING_SIZE and not _is_table_row(line) for line in block
    ):
        return True  # 大きな文字だけの行（折り返した見出しを含む）
    if len(block) != 1 or _is_table_row(block[0]):
        return False
    line = block[0]
    if line.size >= body_size * _HEADING_SIZE:
        return True
    text = line.text.strip()
    return len(text) <= _HEADING_MAX_CHARS and not text.endswith(_SENTENCE_END) and not text.endswith(("、", "，", ","))


def _split_leading_headings(block: list[Line], body_size: float) -> list[list[Line]]:
    """ブロックの先頭にある大きな文字の行（見出し）を、本文から切り離す。

    見出しの直後は行の間隔が狭いので、間隔だけでは本文と同じブロックに入る。
    """
    if body_size <= 0 or len(block) < 2:
        return [block]
    count = 0
    while (
        count < len(block) - 1
        and not _is_table_row(block[count])
        and block[count].size >= body_size * _HEADING_SIZE
    ):
        count += 1
    if count == 0:
        return [block]
    return [block[:count], block[count:]]  # 折り返した見出しは、まとめて 1 つ


def _line_pitch(gaps: list[float]) -> float:
    """行の間隔の基準。段落の間の広い間隔に引きずられないよう、中央値ではなく第 1 四分位を使う。"""
    if len(gaps) >= 4:
        return statistics.quantiles(gaps, n=4)[0]
    return min(gaps) if gaps else 0.0


def _is_short_unterminated(line: Line) -> bool:
    text = line.text.strip()
    return len(text) <= _HEADING_MAX_CHARS and not text.endswith(_SENTENCE_END + ("、", "，", ","))


def _blocks(lines: list[Line]) -> list[list[Line]]:
    """縦の間隔と、表の行かどうかで、行をブロックに分ける。

    行の間隔に変化が無いページ（折り返しが無く、どの行も同じ間隔）は、間隔では段落が分からない。
    そのときは、文末で終わる行の後ろと、短くて文末が無い行（見出し）の前後で切る。
    """
    if not lines:
        return []
    gaps = [prev.y - cur.y for prev, cur in zip(lines, lines[1:], strict=False) if prev.y > cur.y]
    pitch = _line_pitch(gaps)
    has_variation = pitch > 0 and any(gap > pitch * _PARAGRAPH_GAP for gap in gaps)

    blocks: list[list[Line]] = [[lines[0]]]
    for prev, cur in zip(lines, lines[1:], strict=False):
        gap = prev.y - cur.y
        if has_variation:
            split = gap > pitch * _PARAGRAPH_GAP
        elif _is_table_row(prev) and _is_table_row(cur):
            split = False  # 表の行どうしは、間隔に関係なく続ける
        else:
            split = (
                prev.text.strip().endswith(_SENTENCE_END)
                or _is_short_unterminated(prev)
                or _is_short_unterminated(cur)
            )
        if split or _is_table_row(prev) != _is_table_row(cur):
            blocks.append([cur])
        else:
            blocks[-1].append(cur)
    return blocks


def _table_chunks(rows: list[tuple[str, int]]) -> list[list[tuple[str, int]]]:
    """表の行（文字列, ページ）を、見出し行を繰り返しながら、長すぎるときだけ行の範囲で分ける。"""
    header, body = rows[0], rows[1:]
    if sum(len(r[0]) + 1 for r in rows) <= HARD_LIMIT_CHARS or not body:
        return [rows]
    groups: list[list[tuple[str, int]]] = []
    current: list[tuple[str, int]] = [header]
    size = len(header[0]) + 1
    for row in body:
        if len(current) > 1 and size + len(row[0]) + 1 > HARD_LIMIT_CHARS:
            groups.append(current)
            current, size = [header], len(header[0]) + 1
        current.append(row)
        size += len(row[0]) + 1
    if len(current) > 1:
        groups.append(current)
    return groups


@dataclass
class _PendingTable:
    rows: list[tuple[str, int]]  # (行の文字列, ページ)
    cells: int
    heading: str | None
    at_page_end: bool = False  # ページの最後のブロックが、この表だった


def chunk_pages(pages: list[Page]) -> list[Chunk]:
    """ページを順に、チャンクの列にする。同じ入力からは、同じチャンク・同じハッシュになる。"""
    sizes = [line.size for page in pages for line in page.lines if not _is_table_row(line)]
    body_size = statistics.median(sizes) if sizes else 0.0

    chunks: list[Chunk] = []
    heading: str | None = None
    pending: _PendingTable | None = None

    def add(page: int, text: str, *, page_end: int | None = None, kind: str = "text", under: str | None = None):
        chunks.append(
            Chunk(
                index=len(chunks),
                page=page,
                heading=under if kind == "table" else heading,
                text=text,
                content_hash=content_hash(text),
                page_end=page_end if page_end and page_end != page else None,
                kind=kind,
            )
        )

    def flush() -> None:
        nonlocal pending
        if pending is None:
            return
        for group in _table_chunks(pending.rows):
            pages_in = [r[1] for r in (group[1:] or group)]  # 見出し行は、繰り返されても範囲に数えない
            add(
                min(pages_in),
                "\n".join(r[0] for r in group),
                page_end=max(pages_in),
                kind="table",
                under=pending.heading,
            )
        pending = None

    for page in pages:
        if not page.lines:
            flush()
            lines = [line.strip() for line in page.text.splitlines() if line.strip()]
            for group in _split_lines(lines):
                add(page.number, "\n".join(group))
            continue

        body = [line for line in page.lines if not _is_page_number(line)]
        blocks = [part for block in _blocks(body) for part in _split_leading_headings(block, body_size)]
        carried = pending is not None and pending.at_page_end  # 前のページの最後が表だった
        if pending is not None:
            pending.at_page_end = False
        for number, block in enumerate(blocks):
            if _is_table_row(block[0]):
                rows = [(line.text, page.number) for line in block]
                cells = len(block[0].cells)
                if pending is not None and carried and number == 0 and pending.cells == cells:
                    if normalize_for_hash(rows[0][0]) == normalize_for_hash(pending.rows[0][0]):
                        rows = rows[1:]  # 続きのページで繰り返された見出し行
                    pending.rows.extend(rows)
                else:
                    flush()
                    pending = _PendingTable(rows=rows, cells=cells, heading=heading)
                continue
            flush()
            if _is_heading(block, body_size):
                heading = _join_wrapped([line.text.strip() for line in block])
                continue
            paragraph = _join_wrapped([line.text.strip() for line in block])
            for group in _cap(_sentences(paragraph)):
                add(page.number, "\n".join(group))
        if pending is not None and blocks and _is_table_row(blocks[-1][0]):
            pending.at_page_end = True
    flush()
    return chunks
