"""検証済みの本文（Markdown の小集合）を、テスト用の PDF にする。

reportlab と、埋め込める日本語の TrueType フォント（通常と太字の静的な TTF。既定は Noto Sans JP の Regular / Bold。
`python -m evals.synth.make_fonts` で作る）が要る。アプリの依存には入れていない（`evals/synth/requirements.txt`）。
全ページの上に「テスト用の架空資料」の帯を入れ、PDF のメタデータ（Subject・Keywords）にも印を入れる。
同じ入力・同じ reportlab・同じフォントなら、同じバイト列になる（作成日時を固定する）。
reportlab は呼び出し時に読み込む（無い環境でも、このモジュールの定数と、他のモジュールは使える）。
"""

from __future__ import annotations

import re
from pathlib import Path

from evals.synth import markdown_subset

MARKER = "DOCUNAVI-SYNTHETIC-TEST"
NOTICE = "テスト用の架空資料"
BANNER_LINES = (
    f"【{NOTICE}】",
    "検索の評価のために作った架空の内容です。実在の人物・団体・製品・数値とは関係ありません。",
)
FONT_DIR = Path(__file__).resolve().parent / "fonts"  # Git 管理外（OFL のフォント。make_fonts.py が作る）
NOTO_REGULAR = FONT_DIR / "NotoSansJP-Regular.ttf"
NOTO_BOLD = FONT_DIR / "NotoSansJP-Bold.ttf"
IPA_FONT = Path("/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf")  # Debian の fonts-ipafont-gothic。太字は無い
DEFAULT_FONT = NOTO_REGULAR if NOTO_REGULAR.exists() else IPA_FONT
DEFAULT_BOLD_FONT = NOTO_BOLD if NOTO_BOLD.exists() else None
_FONT_NAME = "SynthJP"
_BOLD_NAME = "SynthJP-Bold"


class RenderUnavailable(RuntimeError):
    """reportlab が無い、またはフォントが無い。"""


def _escape(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    text = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", text)
    return re.sub(r"\[\^(\d+)\]", r"<super>\1</super>", text)


COVER_COLOPHON = "初版｜2026年11月"


def render_pdf(
    md: str,
    out_path: Path,
    *,
    font_path: Path,
    doc_id: str,
    title: str,
    bold_font_path: Path | None = None,
    cover_image: Path | None = None,
) -> None:
    """`cover_image` があれば、先頭に表紙を 1 ページ足す（題名・画像（角を 1.5mm 丸く）・発行情報。全ページの帯は表紙にも入る）。"""
    try:
        from reportlab import rl_config
        from reportlab.lib import colors
        from reportlab.lib.fonts import addMapping
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.lib.utils import ImageReader
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
        from reportlab.platypus import Flowable, PageBreak, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise RenderUnavailable(
            "reportlab がありません（evals/synth/requirements.txt を一時的な仮想環境へ入れる）"
        ) from exc
    if not font_path.exists():
        raise RenderUnavailable(f"フォントがありません: {font_path}")

    rl_config.invariant = 1  # 作成日時・ID を固定する
    if bold_font_path is not None and not bold_font_path.exists():
        raise RenderUnavailable(f"太字のフォントがありません: {bold_font_path}")
    pdfmetrics.registerFont(TTFont(_FONT_NAME, str(font_path)))
    bold_name = _FONT_NAME
    if bold_font_path is not None:  # 太字の字体が無ければ、`<b>` も通常の字体で描く（記号は出さない）
        pdfmetrics.registerFont(TTFont(_BOLD_NAME, str(bold_font_path)))
        bold_name = _BOLD_NAME
    pdfmetrics.registerFontFamily(
        _FONT_NAME, normal=_FONT_NAME, bold=bold_name, italic=_FONT_NAME, boldItalic=bold_name
    )
    addMapping(_FONT_NAME, 0, 0, _FONT_NAME)
    addMapping(_FONT_NAME, 1, 0, bold_name)

    def style(name: str, size: float, leading: float, **kw) -> ParagraphStyle:
        kw.setdefault("fontName", _FONT_NAME)
        return ParagraphStyle(name, fontSize=size, leading=leading, wordWrap="CJK", bulletFontName=_FONT_NAME, **kw)

    body = style("body", 10.5, 18, spaceAfter=6)
    h1 = style("h1", 17, 24, spaceAfter=10, keepWithNext=1, fontName=bold_name)
    h2 = style("h2", 13, 20, spaceBefore=10, spaceAfter=5, keepWithNext=1, fontName=bold_name)
    h3 = ParagraphStyle(
        "h3", parent=body, fontName=bold_name, fontSize=11.5, leading=18, spaceBefore=6, spaceAfter=3, keepWithNext=1
    )
    li = ParagraphStyle("li", parent=body, leftIndent=16, bulletIndent=5, spaceAfter=3)
    li2 = ParagraphStyle("li2", parent=li, leftIndent=32, bulletIndent=21)
    cont = ParagraphStyle("cont", parent=body, leftIndent=16, spaceAfter=3)
    lead = ParagraphStyle(
        "lead", parent=body, keepWithNext=1, spaceAfter=3
    )  # 太字だけの行（小見出し）は、次の段落と離さない
    cell = style("cell", 9, 14)
    note = style("note", 8.5, 13, spaceAfter=2)

    class _Cover(Flowable):
        """表紙。枠いっぱいの 1 つの部品で、ページの絶対位置に描く（枠の内側の余白で左端がずれるのを避ける）。"""

        def wrap(self, avail_width, avail_height):
            return avail_width, avail_height

        def draw(self):
            page_w, page_h = A4
            content_w = page_w - 40 * mm
            canv = self.canv
            canv.saveState()
            canv.translate(-(20 * mm + 6), -(18 * mm + 6))  # 枠の左下（余白の内側）から、ページの左下へ
            cover_title = style("cover_title", 22, 32)
            cover_note = style("cover_note", 10, 16, textColor=colors.HexColor("#555555"))
            # 題名の末尾の（…）は、副題として別の行にする（長い題名が、括弧の途中で折り返されないように）
            matched = re.fullmatch(r"(.+?)\s*(（.*）)", title)
            main, sub = (matched.group(1), matched.group(2)) if matched else (title, "")
            y = page_h - 42 * mm
            para = Paragraph(_escape(main), cover_title)
            _, h = para.wrap(content_w, page_h)
            para.drawOn(canv, 20 * mm, y - h)
            y -= h
            if sub:
                sub_para = Paragraph(_escape(sub), style("cover_sub", 14, 22))
                _, sh = sub_para.wrap(content_w, page_h)
                sub_para.drawOn(canv, 20 * mm, y - 3 * mm - sh)
                y -= 3 * mm + sh
            y -= 14 * mm
            img_w, img_h = ImageReader(str(cover_image)).getSize()
            ih = content_w * img_h / img_w
            clip = canv.beginPath()
            clip.roundRect(20 * mm, y - ih, content_w, ih, 1.5 * mm)
            canv.clipPath(clip, stroke=0, fill=0)
            canv.drawImage(str(cover_image), 20 * mm, y - ih, width=content_w, height=ih)
            canv.restoreState()
            canv.saveState()
            canv.translate(-(20 * mm + 6), -(18 * mm + 6))
            note = Paragraph(_escape(COVER_COLOPHON), cover_note)
            note.wrap(content_w, page_h)
            note.drawOn(canv, 20 * mm, 24 * mm)
            canv.restoreState()

    story: list = [_Cover(), PageBreak()] if cover_image is not None else []
    for kind, value in markdown_subset.parse(md):
        if kind == "h1":
            story.append(Paragraph(_escape(str(value)), h1))
        elif kind == "h2":
            story.append(Paragraph(_escape(str(value)), h2))
        elif kind == "h3":
            story.append(Paragraph(_escape(str(value)), h3))
        elif kind in ("li", "li2"):
            story.append(Paragraph(_escape(str(value)), li if kind == "li" else li2, bulletText="•"))
        elif kind == "cont":
            story.append(Paragraph(_escape(str(value)), cont))
        elif kind == "p":
            heading_like = re.fullmatch(r"(\d+\.\s*)?\*\*[^*]+\*\*", str(value)) is not None
            story.append(Paragraph(_escape(str(value)), lead if heading_like else body))
        elif kind == "footnote":
            story.append(Paragraph(_escape(str(value)), note))
        elif kind == "table":
            rows = [[Paragraph(_escape(c), cell) for c in row] for row in value]  # type: ignore[union-attr]
            table = Table(rows, repeatRows=1, hAlign="LEFT")
            table.setStyle(
                TableStyle(
                    [
                        ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#eeeeee")),
                        ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ]
                )
            )
            story.extend([table, Spacer(1, 6)])

    width, height = A4

    class _Canvas(canvas.Canvas):
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
                self.setFont(_FONT_NAME, 8)
                if cover_image is None or self._pageNumber > 1:  # 表紙には、ページ番号を入れない
                    self.drawRightString(width - 20 * mm, 10 * mm, f"{doc_id}  {self._pageNumber} / {total}")
                super().showPage()
            super().save()

    def decorate(c: canvas.Canvas, _doc) -> None:
        c.saveState()
        c.setFillColor(colors.HexColor("#b00020"))
        c.setFont(_FONT_NAME, 10)
        c.drawString(20 * mm, height - 10 * mm, BANNER_LINES[0])
        c.setFont(_FONT_NAME, 8)
        c.drawString(20 * mm, height - 14 * mm, BANNER_LINES[1])
        c.setStrokeColor(colors.HexColor("#b00020"))
        c.line(20 * mm, height - 16 * mm, width - 20 * mm, height - 16 * mm)
        c.restoreState()

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(
        str(out_path),
        pagesize=A4,
        leftMargin=20 * mm,
        rightMargin=20 * mm,
        topMargin=22 * mm,
        bottomMargin=18 * mm,
        title=title,
        author="docunavi synthetic test data",
        subject=f"{MARKER} {NOTICE}",
        keywords=MARKER,
    )
    doc.build(story, onFirstPage=decorate, onLaterPages=decorate, canvasmaker=_Canvas)
