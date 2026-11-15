"""ザーハード1〜3.txt から、ザーハード1〜3.pdf を作る（PDF の生成。アプリの実行には使わない）。

元の .txt は evals/fixtures/zahard_txt/（読むだけで、変えない）。段落の分け方は layout.json。語句・数値・設定・文の順序は変えない。
文（本文）の文面と順序は変えない。2026-11-15 から、先頭に表紙と目次を付け、各まとまりに見出し（layout.json のまとまりの名前）と
題名の行を付けた。PDF のページラベルで、本文を 1 から数える（表紙・i・1）。題名は PDF のメタデータ（/Title）にも入れる。
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
import io
import json
import re
import unicodedata
from pathlib import Path

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.platypus import KeepTogether, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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


def _numbered_canvas():
    """表紙（番号なし）・目次（ローマ数字 i）・本文（1 から数え直す）に分け、PDF のページラベルも同じに付ける。"""

    class Numbered(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved: list[dict] = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            self.addPageLabel(0, prefix="表紙")
            self.addPageLabel(1, style="ROMAN_LOWER")
            self.addPageLabel(2, style="ARABIC", start=1)
            for state in self._saved:
                self.__dict__.update(state)
                self.setFont(FONT_NAME, 9)
                if self._pageNumber == 2:
                    self.drawCentredString(A4[0] / 2, 12 * mm, "i")
                elif self._pageNumber > 2:
                    self.drawCentredString(A4[0] / 2, 12 * mm, f"{self._pageNumber - 2} / {total - 2}")
                super().showPage()  # 表紙（1 ページ目）には番号を入れない
            super().save()

    return Numbered


class _RecordingDoc(SimpleDocTemplate):
    """まとまりの見出し（h2）が、本文の何ページ目に載ったかを控える（目次に印字する番号を、同じ組版から取る）。"""

    chapters: list[tuple[str, int]]

    def afterFlowable(self, flowable):
        if not hasattr(self, "chapters"):
            self.chapters = []
        style = getattr(flowable, "style", None)
        if isinstance(flowable, Paragraph) and style is not None and style.name == "h2":
            self.chapters.append((flowable.getPlainText(), self.page))


def build(txt_path: Path, pdf_path: Path, title: str, series: str, groups: dict[str, list[int]]) -> None:
    raw = unicodedata.normalize("NFC", txt_path.read_text(encoding="utf-8"))
    if any(char in raw for char in "&<>"):
        raise ValueError("段落の文字列に、エスケープが要る文字があります")
    body = ParagraphStyle(
        "body", fontName=FONT_NAME, fontSize=12, leading=23, wordWrap="CJK", alignment=TA_LEFT, spaceAfter=7
    )
    h1 = ParagraphStyle("h1", parent=body, fontSize=18, leading=26, spaceAfter=12)
    h2 = ParagraphStyle("h2", parent=body, fontSize=13, leading=20, spaceBefore=10, spaceAfter=6)
    cover_title = ParagraphStyle("cover_title", parent=body, fontSize=26, leading=38, spaceAfter=14)
    cover_sub = ParagraphStyle("cover_sub", parent=body, fontSize=13, leading=22)
    toc_row = ParagraphStyle("toc_row", parent=body, spaceAfter=0)
    paragraphs = groups_of(sentences_of(raw), groups)

    def make_story() -> list:
        """本文の組版部品。組むたびに作り直す（部品は組版で状態が変わるので、2 回の組版で使い回さない）。"""
        story: list = [Paragraph(title, h1)]
        for name, group in zip(groups, paragraphs, strict=True):
            story.append(KeepTogether([Paragraph(name, h2), Paragraph("".join(group), body)]))
        return story

    margins = {"leftMargin": 20 * mm, "rightMargin": 20 * mm, "topMargin": 20 * mm, "bottomMargin": 22 * mm}
    # 1 回目: 本文だけを組んで、見出しが載るページを控える。2 回目: 表紙・目次を前に付けて組む（本文のページ分けは変わらない）
    probe = _RecordingDoc(io.BytesIO(), pagesize=A4, title=title, author="", invariant=True, **margins)
    probe.build(make_story(), canvasmaker=_numbered_canvas())
    width = A4[0] - 40 * mm
    rows = [[Paragraph(name, toc_row), Paragraph(str(page), toc_row)] for name, page in probe.chapters]
    toc = Table(rows, colWidths=[width - 20 * mm, 20 * mm])
    toc.setStyle(
        TableStyle(
            [
                ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("LINEBELOW", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("TOPPADDING", (0, 0), (-1, -1), 5),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]
        )
    )
    front = [
        Spacer(1, 70 * mm),
        Paragraph(title, cover_title),
        Spacer(1, 8 * mm),
        Paragraph(series, cover_sub),
        PageBreak(),
        Paragraph("目次", h1),
        toc,
        PageBreak(),
    ]
    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, title=title, author="", invariant=True, **margins)
    doc.build(front + make_story(), canvasmaker=_numbered_canvas())


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
        build(args.txt_dir / f"{name}.txt", args.out / f"{name}.pdf", spec["title"], spec["series"], spec["groups"])
        print(f"-> {args.out / (name + '.pdf')}")


if __name__ == "__main__":
    main()
