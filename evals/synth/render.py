"""検証済みの本文（Markdown の小集合）を、テスト用の PDF にする。

reportlab と、埋め込める日本語の TrueType フォントが要る。アプリの依存には入れていない（`evals/synth/requirements.txt`）。
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
DEFAULT_FONT = Path(
    "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf"
)  # Debian の fonts-ipafont-gothic（IPA フォント）
_FONT_NAME = "SynthJP"


class RenderUnavailable(RuntimeError):
    """reportlab が無い、またはフォントが無い。"""


def _escape(text: str) -> str:
    text = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return re.sub(r"\[\^(\d+)\]", r"<super>\1</super>", text)


def render_pdf(md: str, out_path: Path, *, font_path: Path, doc_id: str, title: str) -> None:
    try:
        from reportlab import rl_config
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.pdfgen import canvas
        from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
    except ImportError as exc:
        raise RenderUnavailable(
            "reportlab がありません（evals/synth/requirements.txt を一時的な仮想環境へ入れる）"
        ) from exc
    if not font_path.exists():
        raise RenderUnavailable(f"フォントがありません: {font_path}")

    rl_config.invariant = 1  # 作成日時・ID を固定する
    pdfmetrics.registerFont(TTFont(_FONT_NAME, str(font_path)))

    def style(name: str, size: float, leading: float, **kw) -> ParagraphStyle:
        return ParagraphStyle(name, fontName=_FONT_NAME, fontSize=size, leading=leading, wordWrap="CJK", **kw)

    body = style("body", 10.5, 18, spaceAfter=6)
    h1 = style("h1", 17, 24, spaceAfter=10)
    h2 = style("h2", 13, 20, spaceBefore=10, spaceAfter=5)
    cell = style("cell", 9, 14)
    note = style("note", 8.5, 13, spaceAfter=2)

    story: list = []
    for kind, value in markdown_subset.parse(md):
        if kind == "h1":
            story.append(Paragraph(_escape(str(value)), h1))
        elif kind == "h2":
            story.append(Paragraph(_escape(str(value)), h2))
        elif kind == "p":
            story.append(Paragraph(_escape(str(value)), body))
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
