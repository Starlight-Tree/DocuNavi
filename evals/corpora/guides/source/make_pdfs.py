"""3 冊の Markdown から評価用の PDF を作る（評価用の PDF の生成。アプリの実行には使わない）。

reportlab と、埋め込める日本語の TrueType フォントが要る。アプリの依存（requirements*.txt）には入れていない。
一時的な仮想環境で実行する:

    python3 -m venv /tmp/pdfenv && /tmp/pdfenv/bin/pip install reportlab
    # フォント例: Debian の fonts-ipaexfont-gothic（IPA フォント。ipaexg.ttf）
    /tmp/pdfenv/bin/python evals/corpora/guides/source/make_pdfs.py --font /path/to/ipaexg.ttf

3 冊は、組み方を変えてある（Word や Google Docs の書き出しで見かける形）:

| 資料 | 組み方 | 確かめたいこと |
|---|---|---|
| aoba_aw820_manual | 1 段。表が 2 つ。 | カフェと同じ形（基準） |
| hidamari_waste_guide | **2 段組み**。題名だけが全幅。表は段の中。 | 段をまたぐ読み順、段の中の表（評価セットに入れず、`evals/fixtures/two_column/` に置く） |
| tsubasa_dorm_rules | 1 段。**全ページに同じ見出しと、ページ番号**。**表紙と目次が先頭にあり、PDF のページラベルで本文を 1 から数える**（表紙・i・1〜3）。 | 繰り返しの文が、各ページの先頭と末尾に混ざる。表紙・目次を飛ばした表示番号（p.1）と、PDF の通し番号（3 ページ目）を分けて扱えるか |

Markdown は、見出し（# ##）・段落・表（| 区切り）・脚注（[^n] と [^n]: 本文）だけを扱う（cafe/source/make_pdf.py と同じ）。
出力はバイト単位で再現できる（作成日時を固定している）。質問・正解は直さない（承認済み）。
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
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    FrameBreak,
    NextPageTemplate,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

rl_config.invariant = 1  # 作成日時・ID を固定し、同じ入力から同じバイト列にする

HERE = Path(__file__).resolve().parent
FONT_NAME = "JPFont"
PAGE_W, PAGE_H = A4
MARGIN = 20 * mm


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


def _text_width(text: str) -> float:
    """セルの文字列のおおよその幅（全角を 1、半角を 0.55 として数える）。"""
    return sum(1.0 if ord(c) > 0x2E80 else 0.55 for c in text)


def _column_widths(rows: list[list[str]], total: float, font_size: float) -> list[float]:
    """列ごとの最長の語（最低幅）と全体の長さから、合計が total になる幅を決める。"""
    n = len(rows[0])
    longest = [max(_text_width(row[i]) for row in rows if i < len(row)) for i in range(n)]
    weights = [max(w, 3.0) for w in longest]
    widths = [total * w / sum(weights) for w in weights]
    floor = font_size * 3.2  # 3 文字ぶんは最低でも確保する（見出しの「エリア」などが途中で折り返されない）
    widths = [max(w, floor) for w in widths]
    scale = total / sum(widths)
    return [w * scale for w in widths]


def _table(rows: list[list[str]], cell_style: ParagraphStyle, total: float) -> Table:
    data = [[Paragraph(_escape(c), cell_style) for c in row] for row in rows]
    table = Table(data, colWidths=_column_widths(rows, total, cell_style.fontSize), repeatRows=1)
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#999999")),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 4),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
            ]
        )
    )
    return table


def _styles(size: float, leading: float, cell: float) -> dict[str, ParagraphStyle]:
    body = ParagraphStyle(
        "body", fontName=FONT_NAME, fontSize=size, leading=leading, wordWrap="CJK", alignment=TA_LEFT, spaceAfter=6
    )
    return {
        "body": body,
        "h1": ParagraphStyle("h1", parent=body, fontSize=size + 6, leading=leading + 8, spaceAfter=10),
        "h2": ParagraphStyle("h2", parent=body, fontSize=size + 1.5, leading=leading + 4, spaceBefore=8, spaceAfter=5),
        "cell": ParagraphStyle("cell", parent=body, fontSize=cell, leading=cell + 4, spaceAfter=0),
        "note": ParagraphStyle("note", parent=body, fontSize=size - 1.5, leading=leading - 4, spaceAfter=2),
    }


def _story(blocks, styles, table_width: float, *, title_break: bool) -> tuple[list, str]:
    story: list = []
    footnotes: list[str] = []
    title = ""
    for kind, value in blocks:
        if kind == "h1":
            title = str(value)
            story.append(Paragraph(_escape(title), styles["h1"]))
            if title_break:
                story.append(FrameBreak())
        elif kind in ("h2", "p"):
            story.append(Paragraph(_escape(str(value)), styles["h2" if kind == "h2" else "body"]))
        elif kind == "footnote":
            footnotes.append(str(value))
        elif kind == "table":
            story.append(_table(value, styles["cell"], table_width))  # type: ignore[arg-type]
            story.append(Spacer(1, 8))
    if footnotes:
        story.append(Spacer(1, 6))
        story.append(Paragraph("脚注", styles["note"]))
        for text in footnotes:
            m = re.match(r"^\[\^(\d+)\]:\s*(.*)$", text)
            story.append(Paragraph(_escape(f"{m.group(1)} {m.group(2)}") if m else _escape(text), styles["note"]))
    return story, title


def _numbered_canvas(header: str | None, *, front: bool = False):
    """各ページの下に「n / 全ページ数」を入れる。header があれば、全ページの上にも同じ文を入れる。

    front=True は、先頭に表紙と目次がある本。表紙（見出し・番号なし）、目次（ローマ数字 i）、本文（1 から数え直す）に分け、
    PDF のページラベルも同じに付ける。
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
                if front and self._pageNumber == 1:  # 表紙には、見出しも番号も入れない
                    super().showPage()
                    continue
                if header:
                    self.drawString(MARGIN, PAGE_H - 12 * mm, header)
                    self.line(MARGIN, PAGE_H - 14 * mm, PAGE_W - MARGIN, PAGE_H - 14 * mm)
                if front and self._pageNumber == 2:
                    self.drawCentredString(PAGE_W / 2, 12 * mm, "i")
                elif front:
                    self.drawCentredString(PAGE_W / 2, 12 * mm, f"{self._pageNumber - 2} / {total - 2}")
                else:
                    self.drawCentredString(PAGE_W / 2, 12 * mm, f"{self._pageNumber} / {total}")
                super().showPage()
            super().save()

    return Numbered


class _RecordingDoc(BaseDocTemplate):
    """章の見出し（h2）が、本文の何ページ目に載ったかを控える（目次に印字する番号を、同じ組版から取る）。"""

    chapters: list[tuple[str, int]]

    def afterFlowable(self, flowable):
        if not hasattr(self, "chapters"):
            self.chapters = []
        style = getattr(flowable, "style", None)
        if isinstance(flowable, Paragraph) and style is not None and style.name == "h2":
            self.chapters.append((flowable.getPlainText(), self.page))


def _front_story(title: str, chapters: list[tuple[str, int]], styles: dict[str, ParagraphStyle], width: float) -> list:
    """表紙と目次。目次の番号は、本文の印字番号（表紙・目次を数えない）。"""
    body = styles["body"]
    cover_title = ParagraphStyle("cover_title", parent=body, fontSize=26, leading=38, spaceAfter=14)
    cover_sub = ParagraphStyle("cover_sub", parent=body, fontSize=13, leading=22)
    toc_row = ParagraphStyle("toc_row", parent=body, spaceAfter=0)
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
        Paragraph("つばさ大学 学生支援課", cover_sub),
        PageBreak(),
        Paragraph("目次", styles["h1"]),
        toc,
        PageBreak(),
    ]


def build_single(
    md: Path, out: Path, *, header: str | None, size: float, leading: float, cell: float, front_matter: bool = False
) -> None:
    top = MARGIN + (6 * mm if header else 0)
    width = PAGE_W - 2 * MARGIN
    blocks = parse_markdown(md.read_text(encoding="utf-8"))
    styles = _styles(size, leading, cell)

    def frame() -> Frame:
        return Frame(MARGIN, 22 * mm, width, PAGE_H - top - 22 * mm, id="body", leftPadding=0, rightPadding=0)

    story, title = _story(blocks, styles, width, title_break=False)
    if not front_matter:
        doc = BaseDocTemplate(str(out), pagesize=A4, title="", author="", invariant=True)
        doc.title = title
        doc.addPageTemplates([PageTemplate(id="main", frames=[frame()])])
        doc.build(story, canvasmaker=_numbered_canvas(header))
        return

    # 1 回目: 本文だけを組んで、章の見出しが載るページを控える。2 回目: 表紙・目次を前に付けて組む（本文のページ分けは変わらない）
    probe = _RecordingDoc(io.BytesIO(), pagesize=A4, title="", author="", invariant=True)
    probe.addPageTemplates([PageTemplate(id="main", frames=[frame()])])
    probe.build(_story(blocks, styles, width, title_break=False)[0], canvasmaker=_numbered_canvas(header))
    doc = BaseDocTemplate(str(out), pagesize=A4, title="", author="", invariant=True)
    doc.title = title
    doc.addPageTemplates([PageTemplate(id="main", frames=[frame()])])
    doc.build(
        _front_story(title, probe.chapters, styles, width) + story, canvasmaker=_numbered_canvas(header, front=True)
    )


def build_two_columns(md: Path, out: Path, *, size: float, leading: float, cell: float) -> None:
    gap = 8 * mm
    col_w = (PAGE_W - 2 * MARGIN - gap) / 2
    bottom = 22 * mm
    title_h = 22 * mm
    full_h = PAGE_H - MARGIN - bottom
    xs = [MARGIN, MARGIN + col_w + gap]

    def column(x: float, y: float, h: float, name: str) -> Frame:
        return Frame(x, y, col_w, h, id=name, leftPadding=0, rightPadding=0, topPadding=0, bottomPadding=0)

    first = PageTemplate(
        id="first",
        frames=[
            Frame(
                MARGIN,
                PAGE_H - MARGIN - title_h,
                PAGE_W - 2 * MARGIN,
                title_h,
                id="title",
                leftPadding=0,
                rightPadding=0,
            ),
            column(xs[0], bottom, full_h - title_h, "c1"),
            column(xs[1], bottom, full_h - title_h, "c2"),
        ],
    )
    later = PageTemplate(id="later", frames=[column(xs[0], bottom, full_h, "l1"), column(xs[1], bottom, full_h, "l2")])
    story, title = _story(
        parse_markdown(md.read_text(encoding="utf-8")), _styles(size, leading, cell), col_w, title_break=True
    )
    story.insert(0, NextPageTemplate("later"))
    doc = BaseDocTemplate(str(out), pagesize=A4, title="", author="", invariant=True)
    doc.title = title
    doc.addPageTemplates([first, later])
    doc.build(story, canvasmaker=_numbered_canvas(None))


def main() -> None:
    parser = argparse.ArgumentParser(description="Markdown から評価用の PDF を作る")
    parser.add_argument("--font", type=Path, required=True, help="埋め込む日本語の TrueType フォント（.ttf）")
    parser.add_argument("--out-dir", type=Path, default=HERE.parent, help="aoba・tsubasa の PDF の出力先")
    parser.add_argument(
        "--fixture-dir",
        type=Path,
        default=HERE.parents[2] / "fixtures" / "two_column",
        help="hidamari（2 段組み）の PDF の出力先。評価セット（evals/corpora/）の外",
    )
    args = parser.parse_args()
    pdfmetrics.registerFont(TTFont(FONT_NAME, str(args.font)))

    build_single(
        HERE / "aoba_aw820_manual.md",
        args.out_dir / "aoba_aw820_manual.pdf",
        header=None,
        size=11.5,
        leading=21,
        cell=10,
    )
    build_two_columns(
        HERE / "hidamari_waste_guide.md", args.fixture_dir / "hidamari_waste_guide.pdf", size=10.5, leading=18, cell=8.5
    )
    build_single(
        HERE / "tsubasa_dorm_rules.md",
        args.out_dir / "tsubasa_dorm_rules.pdf",
        header="つばさ大学 学生寮 入寮規程",
        size=11.5,
        leading=21,
        cell=10,
        front_matter=True,
    )
    for directory, name in (
        (args.out_dir, "aoba_aw820_manual"),
        (args.fixture_dir, "hidamari_waste_guide"),
        (args.out_dir, "tsubasa_dorm_rules"),
    ):
        print("->", directory / f"{name}.pdf")


if __name__ == "__main__":
    main()
