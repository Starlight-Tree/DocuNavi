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
- 表のセルが折り返されて、1 つの表の行が縦にずれた何本もの行に割れているときは、先に列ごとに 1 行へ組み直す（`_merge_wrapped_rows`）。
  条件は関数の説明のとおりで、行の位置（間隔・左端）だけを使う。条件に合わない表や段落は、そのまま。

**共通**

- 段落のチャンクは 1 ページの中に収める（`page` が正確になる）。表だけが、ページをまたぎうる（`page_end`）。
- 長さで詰めて大きなチャンクにはしない。詰める方式は、段落を 1 つ書き換えて文字数が変わっただけで、後ろの切れ目がずれ、
  変わっていないチャンクまで「変更」になる（テストで確かめた）。段落単位なら、書き換えた段落のチャンクだけが変わる。
  段落の挿入・削除でも、増減するのはその段落のチャンクだけで、ほかのチャンクの内容（ハッシュ）は変わらない（番号は 1 つずれる）。
- 長すぎる段落だけ、文の切れ目で分ける（`HARD_LIMIT_CHARS`）。

**限界（まだやっていない）**

- 折り返したセルの組み直しは、行どうしの間隔が、セルの中の行の間隔より広い表だけ（reportlab の表、Google Docs の表。セルの上下に余白がある）。
  Word の既定のように、行の間隔と折り返しの間隔が同じ表は、位置から行の切れ目が分からないので、割れたまま。罫線は読まない。
  表の行が 1 行だけ折り返している表（隣に、間隔の広い表の行が無い）も、組み直さない。
- 2 段組み（`pdf_parser._split_columns`）は、段の検出に失敗すると、段をまたいだ行が表に見えることがある（下の条件を満たすページだけ）。
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
# 折り返したセルの断片とみなす、縦の間隔の上限（文字の大きさに対する比）。表の行どうしの間隔は、これより広い
_TIGHT_GAP = 1.6
# 組み直す前提: 行どうしの間隔が、断片どうしの間隔のこの倍以上あること
_ROW_GAP_RATIO = 1.35
# 1 つの行に割れる断片の数の上限（これより多く詰まって続くなら、表全体や段落の可能性が高い）
_MAX_FRAGMENT_LINES = 6
# 同じ列とみなす左端のずれ、同じ大きさとみなす差
_COLUMN_TOLERANCE = 0.3
_SAME_SIZE = 0.5

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


def _join_wrapped(lines: list[str], *, digits: bool = False) -> str:
    """折り返された行をつなぐ。日本語は空白なしで、英数字どうしが接するときだけ空白を入れる。

    digits=True（表のセルの断片）では、数字どうしが接するときは空白を入れない（「7:00-23:0」と「0」は、1 つの時刻の途中の切れ目）。
    """
    text = ""
    for line in lines:
        if digits and text[-1:].isdecimal() and line[:1].isdecimal():
            pass
        elif text and text[-1].isascii() and text[-1].isalnum() and line[:1].isascii() and line[:1].isalnum():
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


def _on_edge(x: float, edges: list[float], size: float) -> bool:
    return any(abs(x - edge) <= _COLUMN_TOLERANCE * size for edge in edges)


def _in_table(x: float, edges: list[float], size: float) -> bool:
    """表の列の左端に揃う、または表の左端と右端の間にある（行の中に 1 つもセルが無い列の断片）。"""
    return _on_edge(x, edges, size) or edges[0] < x < edges[-1]


def _fragment_group(lines: list[Line], edges: list[float]) -> list[tuple[int, int]]:
    """折り返したセルの断片が作る、行のまとまり（始まりと終わりの位置）を返す。

    縦の間隔が詰まった（文字の大きさの `_TIGHT_GAP` 倍以内）行を、同じ大きさのあいだだけつなぐ。
    1 セルだけの行は、表の列の左端に揃っているか、表の左端と右端の間にあるものだけ、まとまりに入れる。
    """
    groups: list[tuple[int, int]] = []
    start = 0
    for i in range(1, len(lines) + 1):
        if i < len(lines):
            prev, cur = lines[i - 1], lines[i]
            gap = prev.bottom - cur.y
            if (
                0 <= gap <= _TIGHT_GAP * max(prev.size, cur.size)
                and abs(prev.size - cur.size) <= _SAME_SIZE
                and (_is_table_row(cur) or _in_table(cur.xs[0], edges, cur.size))
                and (_is_table_row(prev) or _in_table(prev.xs[0], edges, prev.size))
            ):
                continue
        if i - start >= 2:
            groups.append((start, i - 1))
        start = i
    return groups


def _merge_wrapped_rows(lines: list[Line]) -> list[Line]:
    """表のセルが折り返されて何行にも割れた行を、列ごとに 1 行へ組み直す。

    セルが折り返されると、1 つの表の行が、縦に少しずつずれた何本かの行に割れる（上下中央揃えなら、他のセルは中央の行に入る）。
    断片のうち 1 セルだけの行は表の行に見えず、残りも列が欠けるので、表として読めない。
    行の位置だけで決めるので、次のすべてが揃うときだけ組み直す（詰まった普通の表や段落は、そのままにする）。

    - 縦の間隔が詰まった（文字の大きさの `_TIGHT_GAP` 倍以内）、同じ大きさの行が 2〜`_MAX_FRAGMENT_LINES` 本続く。
    - その中に、セルが 2 つ以上ある行がある。1 セルだけの行は、表の列の左端に揃っているか、表の左端と右端の間にある。
      既知の列に揃わない列は、同じ左端の断片が 2 本以上あるときだけ認める（上下中央揃えの折り返しは、上と下に断片が付く）。
    - 行ごとに埋まっている列が同じではない（同じなら、折り返しではなく、別々の行）。
    - 前か後ろに、表の行があり、そこまでの間隔が、まとまりの中の間隔より `_ROW_GAP_RATIO` 倍以上広い（行どうしの間隔が広い表）。
      行の間隔が断片の間隔と同じ表（Word の既定）は、行の切れ目が位置から分からないので、組み直さない。
    """
    if len(lines) < 3 or any(len(line.xs) != len(line.cells) for line in lines):
        return list(lines)
    edges = sorted({x for line in lines if _is_table_row(line) for x in line.xs})
    if not edges:
        return list(lines)

    def is_member(line: Line) -> bool:
        return _is_table_row(line) or _in_table(line.xs[0], edges, line.size)

    merged: dict[int, tuple[int, Line]] = {}  # 始まりの位置 -> (終わりの位置, 組み直した行)
    for start, end in _fragment_group(lines, edges):
        group = lines[start : end + 1]
        if len(group) > _MAX_FRAGMENT_LINES or not any(_is_table_row(line) for line in group):
            continue
        columns = [{round(x / (_COLUMN_TOLERANCE * line.size)) for x in line.xs} for line in group]
        if all(c == columns[0] for c in columns[1:]):
            continue
        inner = max(a.bottom - b.y for a, b in zip(group, group[1:], strict=False))
        outer = []
        if start > 0 and is_member(lines[start - 1]):
            outer.append(lines[start - 1].bottom - lines[start].y)
        if end + 1 < len(lines) and is_member(lines[end + 1]):
            outer.append(lines[end].bottom - lines[end + 1].y)
        if not any(gap >= _ROW_GAP_RATIO * inner for gap in outer):
            continue

        cells: list[tuple[float, list[str]]] = []  # (列の左端, 上から順の断片)
        for line in group:
            for x, text in zip(line.xs, line.cells, strict=True):
                for column in cells:
                    if abs(x - column[0]) <= _COLUMN_TOLERANCE * line.size:
                        column[1].append(text)
                        break
                else:
                    cells.append((x, [text]))
        if any(len(column[1]) < 2 and not _on_edge(column[0], edges, group[0].size) for column in cells):
            continue  # 既知の列に揃わない断片が 1 本だけなら、表の断片ではなく、近くの別の行かもしれない
        cells.sort(key=lambda column: column[0])
        merged[start] = (
            end,
            Line(
                y=group[0].y,
                size=max(line.size for line in group),
                cells=tuple(_join_wrapped(column[1], digits=True) for column in cells),
                xs=tuple(column[0] for column in cells),
                low=group[-1].y,
            ),
        )

    result: list[Line] = []
    i = 0
    while i < len(lines):
        if i in merged:
            end, line = merged[i]
            result.append(line)
            i = end + 1
        else:
            result.append(lines[i])
            i += 1
    return result


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
    gaps = [prev.bottom - cur.y for prev, cur in zip(lines, lines[1:], strict=False) if prev.bottom > cur.y]
    pitch = _line_pitch(gaps)
    has_variation = pitch > 0 and any(gap > pitch * _PARAGRAPH_GAP for gap in gaps)

    blocks: list[list[Line]] = [[lines[0]]]
    for prev, cur in zip(lines, lines[1:], strict=False):
        gap = prev.bottom - cur.y
        if cur.y > prev.y:
            split = True  # 行の並びが上に戻った（2 段組みで、次の段に移った）
        elif has_variation:
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
    page_lines = [_merge_wrapped_rows(list(page.lines)) for page in pages]
    sizes = [line.size for lines in page_lines for line in lines if not _is_table_row(line)]
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

    for page, lines_of_page in zip(pages, page_lines, strict=True):
        if not page.lines:
            flush()
            lines = [line.strip() for line in page.text.splitlines() if line.strip()]
            for group in _split_lines(lines):
                add(page.number, "\n".join(group))
            continue

        body = [line for line in lines_of_page if not _is_page_number(line)]
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
