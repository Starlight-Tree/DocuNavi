"""PDF からページごとのテキストを取り出す（pypdf）。

取り込めない PDF は、理由コード付きの `IngestionError` にする。黙って空として扱わない。
例外の文字列やファイルのパス、PDF の中身は、理由に含めない（固定の文だけを返す）。
"""

import math
import unicodedata
from dataclasses import dataclass
from pathlib import Path

# 理由コード -> 利用者に見せる固定の説明
REASONS = {
    "unreadable": "PDF として読めません。",
    "encrypted": "パスワードが付いていて読めません。",
    "empty": "抽出できる文字がありません（画像だけのスキャン PDF の可能性があります。OCR が要ります）。",
    "garbled": "文字化けしています（フォントに文字の対応表が無い可能性があります）。",
}

# 抽出した文字のうち、これ以上が「読めない文字」なら文字化けとみなす割合
GARBLED_RATIO = 0.1


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

    @property
    def text(self) -> str:
        return " ".join(self.cells)


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


def _layout_lines(page) -> tuple[Line, ...]:
    """pypdf の visitor で、文字列ごとの位置を取り、同じ高さの文字列を 1 行にまとめる。"""
    runs: list[tuple[float, float, float, str]] = []  # (y, x, size, text)

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

    runs.sort(key=lambda r: -r[0])
    rows: list[list[tuple[float, float, float, str]]] = []
    for run in runs:
        if rows and abs(rows[-1][0][0] - run[0]) <= _LINE_TOLERANCE * max(run[2], rows[-1][0][2]):
            rows[-1].append(run)
        else:
            rows.append([run])

    lines: list[Line] = []
    for row in rows:
        row.sort(key=lambda r: r[1])
        cells: list[str] = []
        end_x = None
        for _, x, size, text in row:
            if end_x is not None and cells and x - end_x <= _CELL_GAP * size:
                cells[-1] += text  # 同じセル（書式の途中の切れ目など）
            else:
                cells.append(text)
            end_x = x + _estimated_width(text, size)
        lines.append(
            Line(
                y=max(r[0] for r in row),
                size=max(r[2] for r in row),
                cells=tuple(c.strip() for c in cells if c.strip()),
            )
        )
    return tuple(line for line in lines if line.cells)


def extract_pages(path: Path) -> list[Page]:
    """全ページのテキストを返す（文字の無いページは空文字のまま残す）。

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
