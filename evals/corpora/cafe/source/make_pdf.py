"""cafe_store_guide.md から cafe_store_guide.pdf を作る（評価用の PDF の生成。アプリの実行には使わない）。

reportlab と、埋め込める日本語の TrueType フォントが要る。アプリの依存（requirements*.txt）には入れていない。
一時的な仮想環境で実行する:

    python3 -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install reportlab
    # フォント例: Debian の fonts-ipaexfont-gothic（IPA フォント。ipaexg.ttf）
    /tmp/pdfenv/bin/python evals/corpora/cafe/source/make_pdf.py --font /path/to/ipaexg.ttf

Markdown は、見出し（# ##）・段落・表（| 区切り）・脚注（[^n] と [^n]: 本文）だけを扱う。
出力はバイト単位で再現できる（作成日時を固定している）。2026-11-15 から、先頭に表紙・目次を付け、PDF のページラベルで本文を 1 から数える（表紙・i・1〜）。レイアウト（A4、余白 20mm、表の見出し行を次ページでも繰り返す）は、
Word や Google Docs の書き出しに近い形にしてある。質問・正解は直さない（承認済み）。
文字の大きさ（本文 12pt・表 10pt）と行間（23pt）は、表が 4 ページ構成の 3〜4 ページ目にまたがるように選んだ（内容は変えていない）。
2026-11-18 に、惑星ザーハードの店に書き換えた（原稿・質問の名前と言葉の置き換え）。表は 3 ページ目に 2 行、4 ページ目に 10 行（元は 5 行と 7 行）。
"""

import argparse
import io
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
from reportlab.pdfgen import canvas
from reportlab.platypus import PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

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


def _numbered_canvas(*, front: bool = False):
    """各ページの下に「n / 全ページ数」を入れる。

    front=True は、先頭に表紙と目次がある本（2026-11-15 から）。表紙（番号なし）、目次（ローマ数字 i）、本文（1 から数え直す）に分け、
    PDF のページラベル（表紙・i・1…）も同じに付ける。組み方は guides/source/make_pdfs.py と同じ。
    """

    class Numbered(canvas.Canvas):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self._saved: list[dict] = []

        def showPage(self):
            self._saved.append(dict(self.__dict__))
            self._startPage()

        def save(self):
            total = len(self._saved)
            if front:
                self.addPageLabel(0, prefix="表紙")
                self.addPageLabel(1, style="ROMAN_LOWER")
                self.addPageLabel(2, style="ARABIC", start=1)
            for state in self._saved:
                self.__dict__.update(state)
                self.setFont(FONT_NAME, 9)
                if front and self._pageNumber == 1:  # 表紙には番号を入れない
                    super().showPage()
                    continue
                if front and self._pageNumber == 2:
                    self.drawCentredString(A4[0] / 2, 12 * mm, "i")
                elif front:
                    self.drawCentredString(A4[0] / 2, 12 * mm, f"{self._pageNumber - 2} / {total - 2}")
                else:
                    self.drawCentredString(A4[0] / 2, 12 * mm, f"{self._pageNumber} / {total}")
                super().showPage()
            super().save()

    return Numbered


class _RecordingDoc(SimpleDocTemplate):
    """章の見出し（h2）が、本文の何ページ目に載ったかを控える（目次に印字する番号を、同じ組版から取る）。"""

    chapters: list[tuple[str, int]]

    def afterFlowable(self, flowable):
        if not hasattr(self, "chapters"):
            self.chapters = []
        style = getattr(flowable, "style", None)
        if isinstance(flowable, Paragraph) and style is not None and style.name == "h2":
            self.chapters.append((flowable.getPlainText(), self.page))


def _front_story(
    title: str, chapters: list[tuple[str, int]], body: ParagraphStyle, h1: ParagraphStyle, publisher: str
) -> list:
    """表紙と目次。目次の番号は、本文の印字番号（表紙・目次を数えない）。"""
    cover_title = ParagraphStyle("cover_title", parent=body, fontSize=26, leading=38, spaceAfter=14)
    cover_sub = ParagraphStyle("cover_sub", parent=body, fontSize=13, leading=22)
    toc_row = ParagraphStyle("toc_row", parent=body, spaceAfter=0)
    width = A4[0] - 40 * mm
    rows = [[Paragraph(_escape(name), toc_row), Paragraph(str(page), toc_row)] for name, page in chapters]
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
    return [
        Spacer(1, 70 * mm),
        Paragraph(_escape(title), cover_title),
        Spacer(1, 8 * mm),
        Paragraph(_escape(publisher), cover_sub),
        PageBreak(),
        Paragraph("目次", h1),
        toc,
        PageBreak(),
    ]


def build(
    md_path: Path,
    pdf_path: Path,
    font_path: Path,
    *,
    font_size: float,
    leading: float,
    cell_size: float = 10.0,
    front_matter: bool = True,
) -> None:
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(font_path)))

    body = ParagraphStyle(
        "body", fontName=FONT_NAME, fontSize=font_size, leading=leading, wordWrap="CJK", alignment=TA_LEFT, spaceAfter=7
    )
    h1 = ParagraphStyle("h1", parent=body, fontSize=18, leading=26, spaceAfter=12)
    h2 = ParagraphStyle("h2", parent=body, fontSize=13, leading=20, spaceBefore=10, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=body, fontSize=cell_size, leading=cell_size + 4, spaceAfter=0)
    note = ParagraphStyle("note", parent=body, fontSize=font_size - 1.5, leading=leading - 4, spaceAfter=2)

    def make_story() -> list:
        """本文の組版部品。組むたびに作り直す（部品は組版で状態が変わるので、2 回の組版で使い回さない）。"""
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

        return story

    margins = {"leftMargin": 20 * mm, "rightMargin": 20 * mm, "topMargin": 20 * mm, "bottomMargin": 22 * mm}
    title = "オアシス珈琲 店舗案内"
    if not front_matter:
        doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, title=title, author="", invariant=True, **margins)
        doc.build(make_story(), canvasmaker=_numbered_canvas())
        return
    # 1 回目: 本文だけを組んで、章の見出しが載るページを控える。2 回目: 表紙・目次を前に付けて組む（本文のページ分けは変わらない）
    probe = _RecordingDoc(io.BytesIO(), pagesize=A4, title=title, author="", invariant=True, **margins)
    probe.build(make_story(), canvasmaker=_numbered_canvas())
    doc = SimpleDocTemplate(str(pdf_path), pagesize=A4, title=title, author="", invariant=True, **margins)
    front = _front_story(title, probe.chapters, body, h1, "オアシス珈琲 お客様相談室")
    doc.build(front + make_story(), canvasmaker=_numbered_canvas(front=True))


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
