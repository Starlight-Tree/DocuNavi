"""PDF からページごとのテキストを取り出す（pypdf）。

取り込めない PDF は、理由コード付きの `IngestionError` にする。黙って空として扱わない。
例外の文字列やファイルのパス、PDF の中身は、理由に含めない（固定の文だけを返す）。
"""

import math
import statistics
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

# 理由コード -> 利用者に見せる固定の説明
REASONS = {
    "unreadable": "PDF として読めません。",
    "encrypted": "パスワードが付いていて読めません。",
    "empty": "抽出できる文字がありません（画像だけのスキャン PDF の可能性があります。OCR が要ります）。",
    "garbled": "文字化けしています（フォントに文字の対応表が無い可能性があります）。",
}

# 抽出した文字のうち、これ以上が「読めない文字」なら文字化けとみなす割合
GARBLED_RATIO = 0.1

# 題名として保存する長さの上限（超えたら題名なし。メタデータに本文が入っている PDF を弾く）
MAX_TITLE_CHARS = 200


class IngestionError(Exception):
    """取り込めない PDF。`reason` は REASONS のキー。"""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Line:
    """位置から組み直した 1 行。cells は、表のセルのように間が空いた文字列の並び（段落の行は 1 つ）。"""

    y: float  # 行の縦位置（大きいほど上）
    size: float  # 文字の大きさ（行の中の最大）
    cells: tuple[str, ...]
    xs: tuple[float, ...] = ()  # 各セルの左端の横位置（cells と同じ並び。折り返したセルを列に戻すのに使う）
    low: float | None = None  # 折り返したセルを 1 行に組み直したときの、いちばん下の断片の縦位置（普通の行は None）

    @property
    def text(self) -> str:
        return " ".join(self.cells)

    @property
    def bottom(self) -> float:
        return self.y if self.low is None else self.low


@dataclass(frozen=True)
class Page:
    number: int  # 1 始まり
    text: str  # pypdf の通常の抽出（空・文字化けの判定と、語順の正しい全文として使う）
    lines: tuple[Line, ...] = ()  # 座標から組み直した行（上から下へ）。無ければ text の行を使う


def _is_unreadable_char(ch: str) -> bool:
    if ch in "\n\t":
        return False
    # 置換文字、私用領域（フォントの対応表が無いと入りやすい）、制御文字、未割り当て
    return ch == "�" or unicodedata.category(ch) in {"Co", "Cc", "Cn", "Cs"}


def _looks_garbled(texts: list[str]) -> bool:
    chars = [ch for text in texts for ch in text if not ch.isspace()]
    if not chars:
        return False
    bad = sum(1 for ch in chars if _is_unreadable_char(ch))
    return bad / len(chars) >= GARBLED_RATIO


# 同じ行とみなす縦のずれ（文字の大きさに対する比）。上付き文字（脚注の番号）も同じ行に入る
_LINE_TOLERANCE = 0.55
# 同じ行の中で、前の文字列の終わりとの間がこれ（文字の大きさに対する比）を超えて空いたら、別のセルとみなす
_CELL_GAP = 0.9


def _estimated_width(text: str, size: float) -> float:
    """文字列の幅の見積もり。全角は 1 em、半角は 0.5 em とする（セルの区切りの判定にだけ使う）。"""
    return sum(size if ord(ch) >= 0x2E80 else size * 0.5 for ch in text)


Run = tuple[float, float, float, str]  # (y, x, size, text)

# 2 段組みとみなす条件
_MIN_WIDE_ROWS = 6  # 各段にある、段の幅の大半に届く長い行の数の下限（セルの短い表だけのページを、段と見なさない）
_WIDE_RATIO = 0.75  # 「長い行」とみなす、段の幅に対する比
_COLUMN_CROSSING_RATIO = 0.10  # 段の間をまたぐ行（題名など全幅の行）の割合の上限
_COLUMN_OVERLAP = 0.6  # 左右の段が縦に重なる割合の下限（片方だけが縦に短いなら、段ではなく表や囲み）


def _run_end(run: Run) -> float:
    return run[1] + _estimated_width(run[3], run[2])


def _group_rows(runs: list[Run]) -> list[list[Run]]:
    """縦の位置が近い文字列を、行にまとめる（上から下へ）。"""
    rows: list[list[Run]] = []
    for run in sorted(runs, key=lambda r: -r[0]):
        if rows and abs(rows[-1][0][0] - run[0]) <= _LINE_TOLERANCE * max(run[2], rows[-1][0][2]):
            rows[-1].append(run)
        else:
            rows.append([run])
    return rows


def _split_columns(runs: list[Run]) -> list[list[Run]]:
    """2 段組みのページなら、文字列を読み順（全幅の行、左の段、右の段）のまとまりに分ける。そうでなければ、そのまま 1 つ。

    段の間の隙間を、ほとんどの行がまたがない位置（候補は、文字列の左端の少し手前）を探す。次のすべてを満たすときだけ、
    2 段組みとみなす。左右それぞれに、段の幅の大半に届く長い行が `_MIN_WIDE_ROWS` 本以上ある（短いセルだけの表は段ではない）。
    左右が縦に同じ範囲に広がっている（右にだけ縦に短い表や囲みは、段ではない）。隙間をまたぐ行は 1 割以下（またぐのは、題名など全幅の行）。
    全幅の行で帯に区切り、帯ごとに左、右の順に並べる。
    """
    if len(runs) < 2 * _MIN_WIDE_ROWS:
        return [runs]
    rows = _group_rows(runs)
    size = statistics.median(r[2] for r in runs)
    x_min, x_max = min(r[1] for r in runs), max(_run_end(r) for r in runs)
    span = x_max - x_min
    if span <= 0:
        return [runs]

    best: tuple[int, float] | None = None
    for start in sorted({round(r[1], 1) for r in runs if x_min + 0.25 * span <= r[1] <= x_min + 0.75 * span}):
        gutter = start - 0.5 * size
        crossing = sum(1 for row in rows if any(r[1] < gutter < _run_end(r) for r in row))
        if crossing > _COLUMN_CROSSING_RATIO * len(rows):
            continue
        if best is None or (crossing, -gutter) < (best[0], -best[1]):
            best = (crossing, gutter)
    if best is None:
        return [runs]
    gutter = best[1]

    left = [r for r in runs if _run_end(r) <= gutter]
    right = [r for r in runs if r[1] >= gutter]
    full = [r for r in runs if r[1] < gutter < _run_end(r)]
    if not left or not right:
        return [runs]

    def wide_rows(column: list[Run]) -> int:
        lo, hi = min(r[1] for r in column), max(_run_end(r) for r in column)
        # 行の中の文字列の幅の合計（隙間は数えない）。セルの間が空いた表の行は、広がっていても短い
        return sum(1 for row in _group_rows(column) if sum(_run_end(r) - r[1] for r in row) >= _WIDE_RATIO * (hi - lo))

    if wide_rows(left) < _MIN_WIDE_ROWS or wide_rows(right) < _MIN_WIDE_ROWS:
        return [runs]
    top_l, bottom_l = max(r[0] for r in left), min(r[0] for r in left)
    top_r, bottom_r = max(r[0] for r in right), min(r[0] for r in right)
    if min(top_l, top_r) - max(bottom_l, bottom_r) < _COLUMN_OVERLAP * max(top_l - bottom_l, top_r - bottom_r):
        return [runs]

    full.sort(key=lambda r: -r[0])
    groups: list[list[Run]] = []
    for band in range(len(full) + 1):
        above = full[band - 1][0] if band else math.inf
        below = full[band][0] if band < len(full) else -math.inf
        for column in (left, right):
            part = [r for r in column if below < r[0] < above]
            if part:
                groups.append(part)
        if band < len(full):
            groups.append([full[band]])
    return groups


def _lines_of(runs: list[Run]) -> list[Line]:
    """同じ高さの文字列を 1 行にまとめ、間が空いた文字列を別のセルにする（上から下へ）。"""
    runs = sorted(runs, key=lambda r: -r[0])
    rows: list[list[Run]] = []
    for run in runs:
        if rows and abs(rows[-1][0][0] - run[0]) <= _LINE_TOLERANCE * max(run[2], rows[-1][0][2]):
            rows[-1].append(run)
        else:
            rows.append([run])

    lines: list[Line] = []
    for row in rows:
        row.sort(key=lambda r: r[1])
        cells: list[str] = []
        starts: list[float] = []
        end_x = None
        for _, x, size, text in row:
            if end_x is not None and cells and x - end_x <= _CELL_GAP * size:
                cells[-1] += text  # 同じセル（書式の途中の切れ目など）
            else:
                cells.append(text)
                starts.append(x)
            end_x = x + _estimated_width(text, size)
        kept = [(c.strip(), x) for c, x in zip(cells, starts, strict=True) if c.strip()]
        lines.append(
            Line(
                y=max(r[0] for r in row),
                size=max(r[2] for r in row),
                cells=tuple(c for c, _ in kept),
                xs=tuple(x for _, x in kept),
            )
        )
    return [line for line in lines if line.cells]


def _layout_lines(page) -> tuple[Line, ...]:
    """pypdf の visitor で、文字列ごとの位置を取り、同じ高さの文字列を 1 行にまとめる（2 段組みなら、段ごとに読み順で）。"""
    runs: list[Run] = []

    def visitor(text, cm, tm, font_dict, font_size):  # noqa: ARG001
        if not text or not text.strip():
            return
        x = tm[4] * cm[0] + tm[5] * cm[2] + cm[4]
        y = tm[4] * cm[1] + tm[5] * cm[3] + cm[5]
        scale = math.hypot(tm[2] * cm[0] + tm[3] * cm[2], tm[2] * cm[1] + tm[3] * cm[3])
        runs.append((y, x, abs(font_size * scale) or 1.0, text.replace("\n", "")))

    page.extract_text(visitor_text=visitor)
    if not runs:
        return ()
    return tuple(line for group in _split_columns(runs) for line in _lines_of(group))


def clean_title(raw: object) -> str | None:
    """PDF のメタデータの題名を、保存・表示できる形にする。使えなければ None。

    文字列でなければ None。空白の並び（改行を含む）は 1 つの空白にして、前後を落とす。空・文字化け・長すぎる
    （`MAX_TITLE_CHARS` 超）ものは None（切り詰めない。途中で切れた題名を、正しいものとして保存しない）。
    本文から題名を推測はしない。
    """
    if not isinstance(raw, str):
        return None
    title = " ".join(raw.split())
    if not title or len(title) > MAX_TITLE_CHARS or _looks_garbled([title]):
        return None
    return title


def extract_title(path: Path) -> str | None:
    """PDF のメタデータ `/Title` を返す。無い・読めない・使えないときは None（例外は出さない）。"""
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            return None
        metadata = reader.metadata
        return clean_title(metadata.title if metadata is not None else None)
    except Exception:  # noqa: BLE001 - 題名は付随情報。読めなくても取り込み全体は止めない
        return None


def extract_pages(path: Path | BinaryIO) -> list[Page]:
    """全ページのテキストを返す（文字の無いページは空文字のまま残す）。`path` はファイルか、読み込み済みのバイト列（`io.BytesIO`）。

    全ページに文字が無ければ `empty`、文字化けなら `garbled`、読めなければ `unreadable` / `encrypted`。
    """
    from pypdf import PdfReader  # 取り込みのコマンドだけが使う。アプリ本体の起動では読み込まない

    try:
        reader = PdfReader(path)
        if reader.is_encrypted and not reader.decrypt(""):
            raise IngestionError("encrypted")
        texts = [(page.extract_text() or "") for page in reader.pages]
        structure = [_layout_lines(page) for page in reader.pages]
    except IngestionError:
        raise
    except Exception as exc:  # pypdf は壊れた入力でさまざまな例外を出す。境界で理由コードに変える
        raise IngestionError("unreadable") from exc

    if not any(text.strip() for text in texts):
        raise IngestionError("empty")
    if _looks_garbled(texts):
        raise IngestionError("garbled")
    return [
        Page(number=i, text=text, lines=lines)
        for i, (text, lines) in enumerate(zip(texts, structure, strict=True), start=1)
    ]
