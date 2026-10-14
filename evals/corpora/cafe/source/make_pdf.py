"""cafe_store_guide.md から cafe_store_guide.pdf を作る（評価用の PDF の生成。アプリの実行には使わない）。

reportlab と、埋め込める日本語の TrueType フォントが要る。アプリの依存（requirements*.txt）には入れていない。
一時的な仮想環境で実行する:

    python3 -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install reportlab
    # フォント例: Debian の fonts-ipaexfont-gothic（IPA フォント。ipaexg.ttf）
    /tmp/pdfenv/bin/python evals/corpora/cafe/source/make_pdf.py --font /path/to/ipaexg.ttf

Markdown は、見出し（# ##）・段落・表（| 区切り）・脚注（[^n] と [^n]: 本文）だけを扱う。
出力はバイト単位で再現できる（作成日時を固定している）。レイアウト（A4、余白 20mm、表の見出し行を次ページでも繰り返す）は、
Word や Google Docs の書き出しに近い形にしてある。質問・正解は直さない（承認済み）。
文字の大きさ（本文 12pt・表 10pt）と行間（23pt）は、表が 4 ページ構成の 3〜4 ページ目に 5 行と 7 行で、ページをまたぐように選んだ（内容は変えていない）。
"""

import argparse
import re
from pathlib import Path

from reportlab import rl_config
from reportlab.lib import colors
from reportlab.lib.enums import TA_LEFT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.pdfgen import canvas

rl_config.invariant = 1  # 作成日時・ID を固定し、同じ入力から同じバイト列にする

HERE = Path(__file__).resolve().parent
FONT_NAME = "JPFont"


def _escape(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\[\^(\d+)\]", r"<super>\1</super>", text)


def parse_markdown(md: str) -> list[tuple[str, object]]:
    """('h1'|'h2'|'p'|'table'|'footnote', 値) の列にする。表の値は行（セルの文字列の列）のリスト。"""
    blocks: list[tuple[str, object]] = []
    table: list[list[str]] = []
    for raw in md.splitlines():
        line = raw.rstrip()
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if not set("".join(cells)) <= set("-: "):  # 区切り行（|---|---|）は捨てる
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
        elif re.match(r"^\[\^\d+\]:", line):
            blocks.append(("footnote", line))
        else:
            blocks.append(("p", line))
    if table:
        blocks.append(("table", table))
    return blocks


class _NumberedCanvas(canvas.Canvas):
    """各ページの下に「n / 全ページ数」を入れる。"""

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


def build(
    md_path: Path, pdf_path: Path, font_path: Path, *, font_size: float, leading: float, cell_size: float = 10.0
) -> None:
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path)))

    body = ParagraphStyle(
        "body", fontName=FONT_NAME, fontSize=font_size, leading=leading, wordWrap="CJK", alignment=TA_LEFT, spaceAfter=7
    )
    h1 = ParagraphStyle("h1", parent=body, fontSize=18, leading=26, spaceAfter=12)
    h2 = ParagraphStyle("h2", parent=body, fontSize=13, leading=20, spaceBefore=10, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=body, fontSize=cell_size, leading=cell_size + 4, spaceAfter=0)
    note = ParagraphStyle("note", parent=body, fontSize=font_size - 1.5, leading=leading - 4, spaceAfter=2)

    story: list = []
    footnotes: list[str] = []
    for kind, value in parse_markdown(md_path.read_text(encoding="utf-8")):
        if kind == "h1":
            story.append(Paragraph(_escape(str(value)), h1))
        elif kind == "h2":
            story.append(Paragraph(_escape(str(value)), h2))
        elif kind == "p":
            story.append(Paragraph(_escape(str(value)), body))
        elif kind == "footnote":
            footnotes.append(str(value))
        elif kind == "table":
            rows = [[Paragraph(_escape(c), cell) for c in row] for row in value]  # type: ignore[union-attr]
            # 短い語（「エリア」「定休日」、店名、時刻）が、セルの中で途中で折り返されない幅にする
            widths = [34 * mm, 16 * mm, 48 * mm, 30 * mm, 20 * mm, 22 * mm]
            table = Table(rows, colWidths=widths, repeatRows=1)
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                        ("TOPPADDING", (0, 0), (-1, -1), 5),
                        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ]
                )
            )
            story.append(table)
            story.append(Spacer(1, 8))
    if footnotes:
        story.append(Spacer(1, 6))
        story.append(Paragraph("脚注", note))
        for text in footnotes:
            m = re.match(r"^\[\^(\d+)\]:\s*(.*)$", text)
            story.append(Paragraph(_escape(f"{m.group(1)} {m.group(2)}") if m else _escape(text), note))

    doc = SimpleDocTemplate(
        str(pdf_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=20 * mm,
        bottomMargin=22 * mm,
        title="みなとコーヒー 店舗案内",
        author="",
        invariant=True,
    )
    doc.build(story, canvasmaker=_NumberedCanvas)


def main() -> None:
    parser = argparse.ArgumentParser(description="Markdown から評価用の PDF を作る")
    parser.add_argument("--font", type=Path, required=True, help="埋め込む日本語の TrueType フォント（.ttf）")
    parser.add_argument("--md", type=Path, default=HERE / "cafe_store_guide.md")
    parser.add_argument("--out", type=Path, default=HERE.parent / "cafe_store_guide.pdf")
    parser.add_argument("--font-size", type=float, default=12.0)
    parser.add_argument("--leading", type=float, default=23.0)
    parser.add_argument("--cell-size", type=float, default=10.0, help="表の文字の大きさ")
    args = parser.parse_args()
    build(args.md, args.out, args.font, font_size=args.font_size, leading=args.leading, cell_size=args.cell_size)
    print(f"-> {args.out}")


if __name__ == "__main__":
    main()
