"""ザーハード1〜3.txt から、ザーハード1〜3.pdf を作る（PDF の生成。アプリの実行には使わない）。

元の .txt は evals/fixtures/zahard_txt/（読むだけで、変えない）。段落の分け方は layout.json。語句・数値・設定・文の順序は変えない。
本文に無い文字（見出し・題名の行）は足さない。題名は PDF のメタデータ（/Title）にだけ入れる。
組み方は evals/corpora/cafe/source/make_pdf.py と同じ（A4、余白 20mm、本文 12pt、行間 23pt、ページ番号）。1 つの段落を 2 ページに割らない。

reportlab と、埋め込める日本語の TrueType フォントが要る。アプリの依存（requirements*.txt）には入れていない。
一時的な仮想環境で実行する:

    python3 -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install reportlab
    # フォント: IPAex ゴシック（ipaexg.ttf、Ver.004.01）。IPA フォントライセンス v1.0
    /tmp/pdfenv/bin/python evals/fixtures/zahard_pdf/make_pdf.py --font /path/to/ipaexg.ttf --out /tmp/zahard_pdf

出力は、同じ入力・同じ reportlab・同じフォントなら、バイト単位で再現できる（作成日時を固定している）。
storage/pdf/zahard/ の PDF は、この出力の写し。作り直したら、そちらも更新する（tests/test_zahard_pdf.py が、文面と段落の分け方を確かめる）。
"""

import argparse
import json
import re
import unicodedata
from pathlib import Path

from reportlab import rl_config
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import KeepTogether, Paragraph, SimpleDocTemplate

rl_config.invariant = 1  # 作成日時・ID を固定し、同じ入力から同じバイト列にする

HERE = Path(__file__).resolve().parent
FONT_NAME = "JPFont"


def sentences_of(raw: str) -> list[str]:
    """TXT を文に分ける。区切りは「\\」（文字としての円記号）と改行（検索側の分け方と同じ）。"""
    return [part.strip() for part in re.split(r"[\\\n]+", raw) if part.strip()]


def groups_of(sentences: list[str], groups: dict[str, list[int]]) -> list[list[str]]:
    """文を、layout.json のまとまりに分ける。全文を順に、重複なく覆っていること。"""
    order = [i for indexes in groups.values() for i in indexes]
    if order != list(range(len(sentences))):
        raise ValueError("まとまりが、全ての文を順に覆っていません")
    return [[sentences[i] for i in indexes] for indexes in groups.values()]


class _NumberedCanvas(canvas.Canvas):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._saved: list[dict] = []

    def showPage(self):
        self._saved.append(dict(self.__dict__))
        self._startPage()

    def save(self):
        total = len(self._saved)
        for state in self._saved:
            self.__dict__.update(state)
            self.setFont(FONT_NAME, 9)
            self.drawCentredString(A4[0] / 2, 12 * mm, f"{self._pageNumber} / {total}")
            super().showPage()
        super().save()


def build(txt_path: Path, pdf_path: Path, title: str, groups: dict[str, list[int]]) -> None:
    raw = unicodedata.normalize("NFC", txt_path.read_text(encoding="utf-8"))
    if any(char in raw for char in "&<>"):
        raise ValueError("段落の文字列に、エスケープが要る文字があります")
    body = ParagraphStyle(
        "body", fontName=FONT_NAME, fontSize=12, leading=23, wordWrap="CJK", alignment=TA_LEFT, spaceAfter=7
    )
    story = [
        KeepTogether([Paragraph("".join(group), body)])
        for group in groups_of(sentences_of(raw), groups)
    ]
    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=22 * mm,
        title=title,
        author="",
        invariant=True,
    )
    doc.build(story, canvasmaker=_NumberedCanvas)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--font", type=Path, required=True, help="日本語の TrueType フォント（ipaexg.ttf）")
    parser.add_argument("--txt-dir", type=Path, default=HERE.parent / "zahard_txt")
    parser.add_argument("--out", type=Path, required=True, help="PDF の出力先ディレクトリ")
    args = parser.parse_args()
    layout = json.loads((HERE / "layout.json").read_text(encoding="utf-8"))["files"]
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(args.font)))
    args.out.mkdir(parents=True, exist_ok=True)
    for name, spec in layout.items():
        build(args.txt_dir / f"{name}.txt", args.out / f"{name}.pdf", spec["title"], spec["groups"])
        print(f"-> {args.out / (name + '.pdf')}")


if __name__ == "__main__":
    main()
