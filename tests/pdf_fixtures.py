"""テスト用の最小の PDF を組み立てる（抽出のテスト専用。画面では読めない）。

Type0 フォント（Identity-H）の 2 バイトの文字コードを Unicode のコードポイントにそろえ、ToUnicode で恒等に戻す。
フォントは埋め込まないので、PDF ビューアーでは文字が出ない。pypdf の抽出は、実物の PDF（CID フォント + ToUnicode）と同じ経路を通る。
実物の PDF（Google Docs などの書き出し）の抽出は、別に確かめる（evals/corpora/）。
"""

LINE_HEIGHT = 18
TOP = 800
BODY_SIZE = 11
BIG_SIZE = 16
CELL_WIDTH = 120  # 表のセルの横の間隔


class Big(str):
    """大きな文字の行（見出し）。"""


def paragraphs(*texts: str) -> list[str]:
    """段落ごとに、1 行分の空きを挟んだ行のリスト（縦の間隔で段落が分かれる）。"""
    lines: list[str] = []
    for text in texts:
        if lines:
            lines.append("")
        lines.append(text)
    return lines


_TO_UNICODE = b"""/CIDInit /ProcSet findresource begin
12 dict begin
begincmap
/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS) /Supplement 0 >> def
/CMapName /Adobe-Identity-UCS def
/CMapType 2 def
1 begincodespacerange
<0000> <FFFF>
endcodespacerange
1 beginbfrange
<0000> <FFFF> <0000>
endbfrange
endcmap
CMapName currentdict /CMap defineresource pop
end
end"""


def _hex(text: str) -> str:
    for ch in text:
        if ord(ch) > 0xFFFF:
            raise ValueError("BMP の外の文字は使えない")
    return "".join(f"{ord(ch):04X}" for ch in text)


def _content(lines: list) -> bytes:
    """行ごとに絶対位置で置く。str は 1 行、Big は大きな文字の 1 行、tuple は 1 行に並ぶセル。"""
    ops: list[str] = []
    y = TOP
    for line in lines:
        if isinstance(line, tuple):
            for i, cell in enumerate(line):
                ops.append(f"BT /F1 {BODY_SIZE} Tf 1 0 0 1 {50 + i * CELL_WIDTH} {y} Tm <{_hex(cell)}> Tj ET")
        elif line:
            size = BIG_SIZE if isinstance(line, Big) else BODY_SIZE
            ops.append(f"BT /F1 {size} Tf 1 0 0 1 50 {y} Tm <{_hex(str(line))}> Tj ET")
        y -= LINE_HEIGHT
    return "\n".join(ops).encode("ascii")


def _label_range(start: int, style: str | None = None, prefix: str | None = None, first: int | None = None) -> bytes:
    body = ""
    if style:
        body += f"/S {style} "
    if prefix is not None:
        body += "/P <FEFF%s> " % prefix.encode("utf-16-be").hex().upper()  # 日本語は UTF-16BE（BOM つき）で書く
    if first is not None:
        body += f"/St {first} "
    return b"%d << %s>>" % (start, body.encode("ascii"))


def build_pdf(pages: list[list], title: str | None = None, page_labels: list[tuple] | None = None) -> bytes:
    """pages: ページごとの行のリスト。空のリストは、文字の無いページになる。title: メタデータ /Title（None なら Info 辞書を付けない）。
    page_labels: /PageLabels の範囲。（先頭ページの位置〔0 始まり〕, スタイル, 接頭辞, 開始番号）。スタイルは `/D`（1, 2, …）・`/r`（i, ii, …）・None（番号なし）。
    接頭辞と開始番号は省略できる。例: `(0, None, "表紙")`、`(1, "/r")`、`(5, "/D", "A-")`。None なら付けない。

    行は str（1 行）・Big（見出し）・tuple（表の 1 行。セルを横に並べる）・""（1 行分の空き）。
    """
    objects: list[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    labels = b""
    if page_labels:
        nums = b" ".join(_label_range(*spec) for spec in page_labels)
        labels = b" /PageLabels << /Nums [%s] >>" % nums
    add(b"<< /Type /Catalog /Pages 2 0 R%s >>" % labels)
    add(b"")  # 2 = Pages。ページを足し終えてから埋める
    font = add(
        b"<< /Type /Font /Subtype /Type0 /BaseFont /TestFont /Encoding /Identity-H "
        b"/DescendantFonts [4 0 R] /ToUnicode 6 0 R >>"
    )
    add(
        b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /TestFont "
        b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> "
        b"/FontDescriptor 5 0 R /DW 1000 /CIDToGIDMap /Identity >>"
    )
    add(
        b"<< /Type /FontDescriptor /FontName /TestFont /Flags 4 /FontBBox [0 -200 1000 900] "
        b"/ItalicAngle 0 /Ascent 880 /Descent -120 /CapHeight 700 /StemV 80 >>"
    )
    add(b"<< /Length %d >>\nstream\n" % len(_TO_UNICODE) + _TO_UNICODE + b"\nendstream")
    assert font == 3  # 上のオブジェクト番号（3 = Font, 4 = CIDFont, 5 = Descriptor, 6 = ToUnicode）を前提にしている

    page_ids: list[int] = []
    for lines in pages:
        content = _content(lines)
        content_id = len(objects) + 2  # ページの次に追加する内容ストリームの番号
        page_id = add(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 595 842] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>" % content_id
        )
        add(b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream")
        page_ids.append(page_id)

    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    objects[1] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids))

    info = ""
    if title is not None:
        info_id = add(b"<< /Title <FEFF%s> >>" % title.encode("utf-16-be").hex().upper().encode("ascii"))
        info = f" /Info {info_id} 0 R"

    out = bytearray(b"%PDF-1.5\n")
    offsets: list[int] = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += b"%010d 00000 n \n" % offset
    out += b"trailer\n<< /Size %d /Root 1 0 R%s >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        info.encode("ascii"),
        xref,
    )
    return bytes(out)
