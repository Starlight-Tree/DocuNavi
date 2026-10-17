"""PDF の取り込み（app/ingestion）の、DB を使わない部分。

本物の Postgres での保存・差分は tests/test_ingestion_postgres.py（TEST_DATABASE_URL があるときだけ動く）。
PDF は tests/pdf_fixtures.py が組み立てる最小のもの（抽出専用。実物の PDF での確認は evals/corpora/ で別に行う）。
"""

import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.ingestion import __main__ as cli
from app.ingestion import chunker, pdf_parser
from app.ingestion.chunker import Chunk, chunk_pages, content_hash
from app.ingestion.pdf_parser import IngestionError, Line, Page, extract_pages
from app.ingestion.repository import MemoryRepository, diff_counts
from app.ingestion.service import find_pdfs, ingest, source_name
from tests.pdf_fixtures import Big, build_pdf, paragraphs

BODY = [f"第{i}段落の本文はここに書かれています。" for i in range(1, 9)]


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch):
    # 長すぎる行の分割を、短い入力で確かめられるようにする
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 400)


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    # .env の DATABASE_URL を読み込まないようにする（テストが本物の DB に触れないため）
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", "")


def _write(path, pages):
    """pages: ページごとの段落（文字列）のリスト。段落の間に 1 行分の空きを入れる。"""
    path.write_bytes(build_pdf([paragraphs(*page) for page in pages]))
    return path


# ---- 抽出 ----


def test_extract_pages_keeps_japanese_text_and_page_numbers(tmp_path):
    pdf = _write(tmp_path / "a.pdf", [["見出し", "2011年4月に創業しました。"], [], ["三ページ目です。"]])
    pages = extract_pages(pdf)
    assert [p.number for p in pages] == [1, 2, 3]
    assert "2011年4月に創業しました。" in pages[0].text
    assert pages[1].text.strip() == ""
    assert pages[2].text.strip() == "三ページ目です。"


def test_a_pdf_without_text_is_reported_as_empty(tmp_path):
    pdf = _write(tmp_path / "blank.pdf", [[], []])
    with pytest.raises(IngestionError) as exc:
        extract_pages(pdf)
    assert exc.value.reason == "empty"


def test_something_that_is_not_a_pdf_is_unreadable(tmp_path):
    path = tmp_path / "fake.pdf"
    path.write_bytes(b"this is not a pdf at all")
    with pytest.raises(IngestionError) as exc:
        extract_pages(path)
    assert exc.value.reason == "unreadable"
    # 理由に、パスや PDF の中身を含めない
    assert str(path) not in str(exc.value)


def test_garbled_text_is_reported(tmp_path):
    # フォントに文字の対応表が無いとき、私用領域の文字が出る
    pdf = _write(tmp_path / "garbled.pdf", [["  本文"]])
    with pytest.raises(IngestionError) as exc:
        extract_pages(pdf)
    assert exc.value.reason == "garbled"


def test_a_password_protected_pdf_is_reported(tmp_path):
    reader = PdfReader(io.BytesIO(build_pdf([["秘密の本文です。"]])))
    writer = PdfWriter()
    writer.append_pages_from_reader(reader)
    writer.encrypt("secret", algorithm="RC4-128")
    path = tmp_path / "locked.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    with pytest.raises(IngestionError) as exc:
        extract_pages(path)
    assert exc.value.reason == "encrypted"


# ---- チャンク化 ----


def test_chunks_stay_within_a_page_and_are_numbered_in_order():
    pages = [Page(1, "\n".join(BODY[:4])), Page(2, "\n".join(BODY[4:]))]
    chunks = chunk_pages(pages)
    assert [c.index for c in chunks] == list(range(len(chunks)))
    assert [c.page for c in chunks] == sorted(c.page for c in chunks)
    assert {c.page for c in chunks} == {1, 2}
    # 行は言い換えず、そのまま残る
    assert "\n".join(c.text for c in chunks).split("\n") == BODY


def test_chunking_is_deterministic():
    pages = [Page(1, "\n".join(BODY))]
    assert chunk_pages(pages) == chunk_pages(pages)


def test_blank_pages_make_no_chunks():
    assert chunk_pages([Page(1, "  \n\n"), Page(2, "")]) == []


def test_text_without_sentence_ends_is_still_split_at_the_hard_limit():
    pages = [Page(1, "\n".join("見出しだけの行" * 5 for _ in range(30)))]
    chunks = chunk_pages(pages)
    assert len(chunks) > 1
    assert all(len(c.text) <= chunker.HARD_LIMIT_CHARS + len("見出しだけの行" * 5) for c in chunks)


def test_hash_ignores_width_and_spacing_but_not_content():
    assert content_hash("湖畔テラス店  76席") == content_hash("湖畔テラス店 ７６席")
    assert content_hash("湖畔テラス店 76席") != content_hash("湖畔テラス店 77席")


def test_editing_one_paragraph_changes_only_the_chunk_that_holds_it():
    before = chunk_pages([Page(1, "\n".join(BODY[:6])), Page(2, "\n".join(BODY[6:]))])
    edited = list(BODY)
    edited[2] = "第3段落は、書き換えられました。"
    after = chunk_pages([Page(1, "\n".join(edited[:6])), Page(2, "\n".join(edited[6:]))])

    assert len(before) == len(after)
    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a.content_hash != b.content_hash]
    assert len(changed) == 1
    assert "第3段落は、書き換えられました。" in after[changed[0]].text


def test_inserting_a_paragraph_adds_only_its_own_chunk():
    added = "追加された段落です。"
    before = chunk_pages([Page(1, "\n".join(BODY[:4])), Page(2, "\n".join(BODY[4:]))])
    after = chunk_pages([Page(1, "\n".join([added, *BODY[:4]])), Page(2, "\n".join(BODY[4:]))])

    assert len(after) == len(before) + 1
    assert [c.content_hash for c in before] == [c.content_hash for c in after if c.text != added]


# ---- 差分の数え方 ----


@pytest.mark.parametrize(
    ("stored", "new", "expected"),
    [
        (None, ["a", "b"], (2, 0, 0)),
        (["a", "b"], ["a", "b"], (0, 2, 0)),
        (["a", "b", "c"], ["a", "x", "c"], (1, 2, 1)),
        (["a", "b"], ["b", "a"], (0, 2, 0)),  # 順序だけの違い
        (["a", "a"], ["a"], (0, 1, 1)),  # 同じ内容が複数あっても、数で数える
        (["a"], ["a", "a"], (1, 1, 0)),
    ],
)
def test_diff_counts(stored, new, expected):
    assert diff_counts(stored, new) == expected


# ---- サービス（メモリの保存先） ----


def test_dry_run_counts_everything_as_new_and_writes_nothing(tmp_path):
    pdf = _write(tmp_path / "guide.pdf", [BODY[:4], BODY[4:]])
    repo = MemoryRepository()
    (report,) = ingest([pdf], tmp_path, repo, apply=False)

    assert report.source == "guide.pdf"
    assert report.status == "would_insert"
    assert report.pages == 2
    assert report.chunks == report.new_chunks > 0
    assert report.unchanged_chunks == report.removed_chunks == 0
    assert repo.documents == {}


def test_applying_the_same_pdf_twice_adds_no_chunks(tmp_path):
    pdf = _write(tmp_path / "guide.pdf", [BODY[:4], BODY[4:]])
    repo = MemoryRepository()
    (first,) = ingest([pdf], tmp_path, repo, apply=True)
    (second,) = ingest([pdf], tmp_path, repo, apply=True)

    assert first.status == "inserted"
    assert second.status == "unchanged"
    assert second.new_chunks == 0
    assert second.unchanged_chunks == first.chunks
    assert len(repo.documents["guide.pdf"]["chunks"]) == first.chunks


def test_changing_one_paragraph_reports_one_new_and_one_removed_chunk(tmp_path):
    pdf = tmp_path / "guide.pdf"
    repo = MemoryRepository()
    _write(pdf, [BODY[:6], BODY[6:]])
    (first,) = ingest([pdf], tmp_path, repo, apply=True)

    edited = list(BODY)
    edited[2] = "第3段落は、書き換えられました。"
    _write(pdf, [edited[:6], edited[6:]])
    (preview,) = ingest([pdf], tmp_path, repo, apply=False)
    assert preview.status == "would_update"
    assert (preview.new_chunks, preview.removed_chunks) == (1, 1)
    assert preview.unchanged_chunks == first.chunks - 1
    # 確認だけでは、保存済みは変わらない
    assert len(repo.documents["guide.pdf"]["chunks"]) == first.chunks

    (applied,) = ingest([pdf], tmp_path, repo, apply=True)
    assert applied.status == "updated"
    assert "第3段落は、書き換えられました。" in "".join(c.text for c in repo.documents["guide.pdf"]["chunks"])


def test_a_failed_pdf_is_reported_and_recorded_without_dropping_old_chunks(tmp_path):
    good = _write(tmp_path / "guide.pdf", [BODY[:4]])
    repo = MemoryRepository()
    (first,) = ingest([good], tmp_path, repo, apply=True)

    _write(good, [[]])  # 同じ名前で、文字の無い PDF に差し替わった
    (report,) = ingest([good], tmp_path, repo, apply=True)

    assert report.status == "failed"
    assert report.reason == "empty"
    doc = repo.documents["guide.pdf"]
    assert doc["status"] == "failed"
    assert doc["failure_reason"] == "empty"
    assert len(doc["chunks"]) == first.chunks


def test_a_failed_pdf_in_dry_run_records_nothing(tmp_path):
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    repo = MemoryRepository()
    (report,) = ingest([bad], tmp_path, repo, apply=False)
    assert report.status == "failed"
    assert repo.documents == {}


def test_find_pdfs_and_source_names(tmp_path):
    (tmp_path / "sub").mkdir()
    _write(tmp_path / "b.pdf", [["本文です。"]])
    _write(tmp_path / "sub" / "a.PDF", [["本文です。"]])
    (tmp_path / "notes.txt").write_text("x", encoding="utf-8")

    found = find_pdfs(tmp_path)
    assert [source_name(p, tmp_path) for p in found] == ["b.pdf", "sub/a.PDF"]
    assert find_pdfs(tmp_path / "b.pdf") == [tmp_path / "b.pdf"]
    assert source_name(tmp_path / "b.pdf", tmp_path / "b.pdf") == "b.pdf"
    assert find_pdfs(tmp_path / "missing") == []


# ---- コマンド ----


def test_cli_dry_run_prints_the_counts(tmp_path, capsys):
    _write(tmp_path / "guide.pdf", [BODY[:4], BODY[4:]])
    assert cli.main(["--input", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "guide.pdf" in out
    assert "status=would_insert" in out
    assert "pages=2" in out
    assert "何も保存していません" in out
    assert str(tmp_path) not in out  # 相対名だけを出す


def test_cli_exit_code_is_3_when_a_pdf_fails_but_others_are_processed(tmp_path, capsys):
    _write(tmp_path / "good.pdf", [BODY[:2]])
    (tmp_path / "bad.pdf").write_bytes(b"nope")
    assert cli.main(["--input", str(tmp_path)]) == 3
    out = capsys.readouterr().out
    assert "status=failed reason=unreadable" in out
    assert "status=would_insert" in out


def test_cli_without_pdfs_is_a_usage_error(tmp_path, capsys):
    assert cli.main(["--input", str(tmp_path)]) == 2
    assert "PDF がありません" in capsys.readouterr().err


def test_cli_apply_needs_database_url(tmp_path, capsys):
    _write(tmp_path / "guide.pdf", [BODY[:2]])
    assert cli.main(["--input", str(tmp_path), "--apply"]) == 2
    assert "DATABASE_URL" in capsys.readouterr().err


def test_cli_dry_run_and_apply_are_exclusive(tmp_path):
    with pytest.raises(SystemExit) as exc:
        cli.main(["--input", str(tmp_path), "--dry-run", "--apply"])
    assert exc.value.code == 2


def test_chunk_is_a_plain_value():
    chunk = Chunk(index=0, page=1, heading=None, text="本文です。", content_hash=content_hash("本文です。"))
    assert chunk == Chunk(0, 1, None, "本文です。", content_hash("本文です。"))


# ---- 座標からの組み直し（行・段落・見出し・表） ----


def _layout(pages):
    """PDF を組み立てて読み、チャンクにする（pages は build_pdf と同じ書式）。"""
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "t.pdf"
        path.write_bytes(build_pdf(pages))
        return chunk_pages(extract_pages(path))


ROW_HEADER = ("店舗名", "エリア", "座席数")
ROWS = [("港町本店", "港町", "68席"), ("港町ベイ店", "港町", "42席"), ("湖畔テラス店", "湖畔", "76席")]


def test_lines_are_rebuilt_from_positions(tmp_path):
    pdf = tmp_path / "t.pdf"
    pdf.write_bytes(build_pdf([[Big("見出し"), "本文の 1 行目です。", "", ROW_HEADER, ROWS[0]]]))
    (page,) = extract_pages(pdf)

    assert [line.cells for line in page.lines] == [
        ("見出し",),
        ("本文の 1 行目です。",),
        ROW_HEADER,
        ROWS[0],
    ]
    assert page.lines[0].size > page.lines[1].size  # 見出しは大きい
    assert [line.y for line in page.lines] == sorted((line.y for line in page.lines), reverse=True)


def test_wrapped_lines_become_one_paragraph_split_into_sentences():
    chunks = _layout(
        [
            [
                "みなとコーヒーは港町で",
                "創業しました。2026年に",
                "15年になります。",
                "",
                "別の段落です。",
            ]
        ]
    )
    assert [c.text for c in chunks] == [
        "みなとコーヒーは港町で創業しました。\n2026年に15年になります。",
        "別の段落です。",
    ]


def test_paragraph_breaks_are_found_even_when_most_gaps_are_wide():
    # 1 行の段落が多く、広い間隔が多数派になるページ。行の間隔の基準が中央値だと、段落が 1 つに融合する
    chunks = _layout(
        [
            [
                "一つ目の段落です。",
                "",
                "二つ目の段落です。",
                "",
                "三つ目の段落です。",
                "",
                "四つ目の段落の",
                "一行目と二行目です。",
                "",
                "五つ目の段落の",
                "一行目と二行目です。",
            ]
        ]
    )
    assert [c.text for c in chunks] == [
        "一つ目の段落です。",
        "二つ目の段落です。",
        "三つ目の段落です。",
        "四つ目の段落の一行目と二行目です。",
        "五つ目の段落の一行目と二行目です。",
    ]


def test_a_heading_is_not_a_chunk_but_the_heading_of_what_follows():
    chunks = _layout(
        [
            [
                Big("沿革"),
                "2011年に創業しました。",  # 見出しの直後は行の間隔が狭い
                "",
                "2014年に駅前に出店しました。",
                "",
                Big("会社概要"),
                "12 店舗を運営しています。",
            ]
        ]
    )
    assert [(c.heading, c.text) for c in chunks] == [
        ("沿革", "2011年に創業しました。"),
        ("沿革", "2014年に駅前に出店しました。"),
        ("会社概要", "12 店舗を運営しています。"),
    ]
    assert all(c.text not in ("沿革", "会社概要") for c in chunks)


def test_a_short_unpunctuated_line_between_gaps_is_a_heading():
    chunks = _layout([["前の段落です。", "", "営業案内", "", "本文です。"]])
    assert [(c.heading, c.text) for c in chunks] == [(None, "前の段落です。"), ("営業案内", "本文です。")]


def test_page_numbers_are_dropped():
    chunks = _layout([["本文です。", "", "", "", "3 / 4"], ["続きです。", "", "- 2 -"]])
    assert [c.text for c in chunks] == ["本文です。", "続きです。"]


def test_a_table_is_one_chunk_with_its_header_and_one_row_per_line():
    chunks = _layout([["前の説明です。", "", ROW_HEADER, *ROWS, "", "後ろの説明です。"]])

    assert [c.text for c in chunks] == [
        "前の説明です。",
        "店舗名 エリア 座席数\n港町本店 港町 68席\n港町ベイ店 港町 42席\n湖畔テラス店 湖畔 76席",
        "後ろの説明です。",
    ]


def test_a_small_table_across_a_page_break_is_one_chunk_with_a_page_range():
    chunks = _layout([[ROW_HEADER, ROWS[0]], [ROWS[1], ROWS[2]]])

    assert len(chunks) == 1
    (table,) = chunks
    assert (table.kind, table.page, table.page_end) == ("table", 1, 2)
    assert table.text.split("\n") == [
        "店舗名 エリア 座席数",
        "港町本店 港町 68席",
        "港町ベイ店 港町 42席",
        "湖畔テラス店 湖畔 76席",
    ]


def test_a_repeated_header_on_the_next_page_is_not_doubled():
    (table,) = _layout([[ROW_HEADER, ROWS[0]], [ROW_HEADER, ROWS[1], ROWS[2]]])
    assert table.text.count("店舗名") == 1
    assert len(table.text.split("\n")) == 4
    assert (table.page, table.page_end) == (1, 2)


def test_a_single_page_table_has_no_page_end():
    chunks = _layout([["前の説明です。", "", ROW_HEADER, *ROWS]])
    table = next(c for c in chunks if c.kind == "table")
    assert (table.page, table.page_end) == (1, None)
    assert all(c.kind == "text" for c in chunks if c is not table)


def test_tables_are_not_merged_across_a_paragraph_or_with_a_different_column_count():
    # 間に段落がある
    chunks = _layout([[ROW_HEADER, ROWS[0]], ["間の説明です。", "", ROWS[1], ROWS[2]]])
    assert [c.kind for c in chunks] == ["table", "text", "table"]
    # 列数が違う表は、続きではなく別の表
    other = [("名前", "値"), ("a", "1")]
    chunks = _layout([[ROW_HEADER, ROWS[0]], other])
    assert [(c.kind, c.page, c.page_end) for c in chunks] == [("table", 1, None), ("table", 2, None)]


def test_a_table_that_ends_mid_page_is_not_continued_by_a_table_on_the_next_page():
    chunks = _layout([[ROW_HEADER, ROWS[0], "", "表のあとの段落です。"], [ROW_HEADER, ROWS[1]]])
    assert [c.kind for c in chunks] == ["table", "text", "table"]


def test_a_long_table_is_split_by_rows_and_each_part_repeats_the_header(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 60)
    rows = [(f"店舗{i}", "港町", f"{i}席") for i in range(1, 9)]
    chunks = _layout([[ROW_HEADER, *rows]])

    assert len(chunks) > 1
    assert all(c.kind == "table" for c in chunks)
    assert all(c.text.split("\n")[0] == "店舗名 エリア 座席数" for c in chunks)
    body = [line for c in chunks for line in c.text.split("\n")[1:]]
    assert body == [f"店舗{i} 港町 {i}席" for i in range(1, 9)]  # 欠けも重複もない


def test_a_long_table_across_pages_keeps_the_page_range_of_each_part(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 60)
    rows = [(f"店舗{i}", "港町", f"{i}席") for i in range(1, 9)]
    chunks = _layout([[ROW_HEADER, *rows[:4]], rows[4:]])

    assert len(chunks) > 1
    pages = [(c.page, c.page_end) for c in chunks]
    assert pages[0][0] == 1 and pages[-1][1] in (None, 2)
    assert any(page_end == 2 or page == 2 for page, page_end in pages)
    for c in chunks:  # 各部分は、自分の行のページの範囲だけを持つ
        assert c.page <= (c.page_end or c.page)


def test_a_very_long_paragraph_is_split_at_sentence_ends(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 40)
    sentences = [f"これは{i}番目の文です。" for i in range(1, 8)]
    chunks = _layout([["".join(sentences)]])

    assert len(chunks) > 1
    assert [s for c in chunks for s in c.text.split("\n")] == sentences


def test_editing_one_paragraph_of_a_laid_out_page_changes_one_chunk(tmp_path):
    def chunks_of(texts):
        pdf = _write(tmp_path / "doc.pdf", [texts])
        return chunk_pages(extract_pages(pdf))

    texts = [f"第{i}段落の本文はここに書かれています。" for i in range(1, 7)]
    before = chunks_of(texts)
    texts[2] = "第3段落は、書き換えられました。"
    after = chunks_of(texts)

    changed = [i for i, (a, b) in enumerate(zip(before, after, strict=True)) if a.content_hash != b.content_hash]
    assert len(before) == len(after) == 6
    assert changed == [2]


def test_a_page_without_positions_falls_back_to_its_text_lines():
    page = Page(1, "第一文です。\n第二文です。")
    assert page.lines == ()
    assert [c.text for c in chunk_pages([page])] == ["第一文です。", "第二文です。"]
    assert Line(y=1.0, size=1.0, cells=("a", "b")).text == "a b"


CAFE_PDF = Path(__file__).resolve().parents[1] / "evals" / "corpora" / "cafe" / "cafe_store_guide.pdf"


@pytest.mark.skipif(not CAFE_PDF.exists(), reason="評価用の PDF（evals/corpora/cafe/）が無い")
def test_the_cafe_pdf_keeps_its_table_as_one_chunk_across_the_page_break(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 1600)  # 共通設定の小さな上限ではなく、実際の上限で
    pages = extract_pages(CAFE_PDF)
    chunks = chunk_pages(pages)

    (table,) = [c for c in chunks if c.kind == "table"]
    assert (table.page, table.page_end) == (3, 4)  # 表は 2 ページにまたがる
    rows = table.text.split("\n")
    assert rows[0] == "店舗名 エリア 住所 営業時間 定休日 座席数"
    assert len(rows) == 13  # 見出し行は 1 つ、店舗は 12 行
    assert "湖畔テラス店 湖畔 みなと市湖畔1-3-7 9:00〜20:00 なし 76席" in rows
    assert table.heading == "店舗一覧"
    # ページ番号は本文に入らない
    assert not any("3 / 4" in c.text or "4 / 4" in c.text for c in chunks)
    # 2 回読んでも、同じチャンク・同じハッシュ
    assert [c.content_hash for c in chunks] == [c.content_hash for c in chunk_pages(extract_pages(CAFE_PDF))]


# ---- 折り返したセルの組み直し ----


def _cells(y, *cells, size=10.0):
    """(左端の x, 文字列) の並びから Line を作る。"""
    return Line(y=y, size=size, cells=tuple(t for _, t in cells), xs=tuple(x for x, _ in cells))


# 縦の中央揃えで、「第1期」と「ドリンク20%引き」が 2 行に折り返された表（行の間隔 22、断片の間隔 7）
WRAPPED_TABLE = [
    _cells(610, (60, "期"), (100, "期間"), (220, "特典"), (320, "対象")),
    _cells(588, (60, "第1"), (220, "ドリンク20%引")),
    _cells(581, (100, "11月1日から11月10日"), (320, "SサイズとMサイズ")),
    _cells(574, (60, "期"), (220, "き")),
    _cells(552, (60, "第2")),
    _cells(545, (100, "11月11日から11月20日"), (220, "フード15%引き"), (320, "6種類")),
    _cells(538, (60, "期")),
]
MERGED_ROWS = [
    "期 期間 特典 対象",
    "第1期 11月1日から11月10日 ドリンク20%引き SサイズとMサイズ",
    "第2期 11月11日から11月20日 フード15%引き 6種類",
]


def test_wrapped_cells_are_rebuilt_into_one_row_per_table_row():
    rows = chunker._merge_wrapped_rows(WRAPPED_TABLE)
    assert [line.text for line in rows] == MERGED_ROWS
    assert [len(line.cells) for line in rows] == [4, 4, 4]

    (chunk,) = chunk_pages([Page(1, "", tuple(WRAPPED_TABLE))])
    assert (chunk.kind, chunk.text) == ("table", "\n".join(MERGED_ROWS))


def test_a_rebuilt_row_keeps_the_gap_to_the_next_row_measured_from_its_bottom():
    rows = chunker._merge_wrapped_rows(WRAPPED_TABLE)
    assert (rows[1].y, rows[1].bottom, rows[2].y) == (588, 574, 552)
    assert Line(y=5.0, size=1.0, cells=("a",)).bottom == 5.0


def test_a_table_whose_rows_are_as_close_as_its_wrapped_lines_is_left_alone():
    # 行どうしの間隔が断片の間隔と同じ（Word の既定）なら、行の切れ目が位置から分からない。組み直さない
    compact = [
        _cells(100, (60, "店舗"), (200, "席")),
        _cells(93, (60, "港町"), (200, "68")),
        _cells(86, (60, "湖畔"), (200, "76")),
        _cells(79, (200, "30")),
        _cells(72, (60, "鳥栖"), (200, "30")),
    ]
    assert chunker._merge_wrapped_rows(compact) == compact


def test_a_plain_table_with_wide_row_gaps_is_left_alone():
    plain = [_cells(600 - 22 * i, (60, a), (200, b)) for i, (a, b) in enumerate([("店舗", "席"), ("港町", "68"), ("湖畔", "76")])]
    assert chunker._merge_wrapped_rows(plain) == plain


def test_tightly_spaced_paragraph_lines_are_not_merged_even_next_to_a_table():
    table = [_cells(600 - 22 * i, (60, a), (200, b)) for i, (a, b) in enumerate([("店舗", "席"), ("港町", "68")])]
    text = [_cells(540 - 14 * i, (90, f"{i} 行目の本文です")) for i in range(3)]  # 左端が表の列と揃わない
    lines = [*table, *text]
    assert chunker._merge_wrapped_rows(lines) == lines


def test_lines_without_positions_are_returned_unchanged():
    lines = [Line(y=100 - 7 * i, size=10.0, cells=("a", "b") if i % 2 else ("c",)) for i in range(5)]
    assert chunker._merge_wrapped_rows(lines) == lines


MENU_DIR_FOR_WRAP = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "menu"


@pytest.mark.skipif(not (MENU_DIR_FOR_WRAP / "cafe_campaign.pdf").exists(), reason="評価用の PDF（evals/fixtures/menu/）が無い")
def test_the_campaign_pdf_keeps_its_two_wrapped_tables_readable(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 1600)
    tables = [c for c in chunk_pages(extract_pages(MENU_DIR_FOR_WRAP / "cafe_campaign.pdf")) if c.kind == "table"]

    assert [len(c.text.split("\n")) for c in tables] == [4, 4]
    assert "第1期 11月1日から11月10日 ドリンク20%引き SサイズとMサイズ 不要" in tables[0].text
    assert "特典 アプリのクーポン 持ち帰りの10円引き ポイントでの支払い" in tables[1].text
    assert "第2期のフード15%引き 併用できない 併用できる 併用できる" in tables[1].text


def test_a_column_that_exists_only_as_fragments_is_rebuilt():
    # 「エリア」の列は、断片の行（1 セルだけ）にしか無く、表の行にはセルが 1 つも無い（上下中央揃え）
    x = (60, 150, 270)
    lines = [
        _cells(600, (125, "エリ")),
        _cells(594, (x[0], "店名"), (x[1], "住所"), (x[2], "営業時間")),
        _cells(588, (125, "ア")),
        _cells(568, (125, "青波")),
        _cells(562, (x[0], "川沿い店"), (x[1], "青波市川原町2-5-1"), (x[2], "7:00-23:0")),
        _cells(556, (125, "市"), (x[2], "0")),
        _cells(536, (x[0], "公園前店"), (x[1], "青波市緑ヶ丘1-8-3"), (x[2], "6:00-24:00")),
    ]
    assert [line.text for line in chunker._merge_wrapped_rows(lines)] == [
        "店名 エリア 住所 営業時間",
        "川沿い店 青波市 青波市川原町2-5-1 7:00-23:00",
        "公園前店 青波市緑ヶ丘1-8-3 6:00-24:00",
    ]


def test_a_lone_fragment_outside_the_known_columns_is_not_taken_as_a_column():
    lines = [
        _cells(600, (60, "店舗"), (200, "席")),
        _cells(594, (130, "注")),  # 同じ左端の断片が 1 本だけ。別の行かもしれない
        _cells(572, (60, "港町"), (200, "68")),
        _cells(550, (60, "湖畔"), (200, "76")),
    ]
    assert chunker._merge_wrapped_rows(lines) == lines


def test_cell_fragments_are_joined_without_a_space_between_digits_only():
    assert chunker._join_wrapped(["7:00-23:0", "0"], digits=True) == "7:00-23:00"
    assert chunker._join_wrapped(["Wrapped", "cell"], digits=True) == "Wrapped cell"
    assert chunker._join_wrapped(["第1", "期"], digits=True) == "第1期"
    assert chunker._join_wrapped(["7:00-23:0", "0"]) == "7:00-23:0 0"  # 段落のつなぎ方は変えていない


GYM_GUIDE = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "gym" / "gym_guide.pdf"


@pytest.mark.skipif(not GYM_GUIDE.exists(), reason="評価用の PDF（evals/fixtures/gym/）が無い")
def test_the_gym_guide_store_table_is_one_chunk_with_its_wrapped_area_column(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 1600)
    tables = [c for c in chunk_pages(extract_pages(GYM_GUIDE)) if c.kind == "table"]

    (store,) = [c for c in tables if c.heading == "店舗一覧"]
    rows = store.text.split("\n")
    assert rows[0] == "店名 エリア 住所 営業時間 定休日 面積・定員 主な設備"
    assert len(rows) == 9  # 見出し行と 8 店
    assert "川沿い店 青波市 青波市川原町2-5-1 7:00-23:00 毎週水曜 2,400㎡・380名 プール・サウナ" in rows


def test_rebuilding_rows_neither_drops_nor_invents_characters():
    def chars(lines):
        return sorted(ch for line in lines for cell in line.cells for ch in cell if not ch.isspace())

    assert chars(chunker._merge_wrapped_rows(WRAPPED_TABLE)) == chars(WRAPPED_TABLE)


# ---- 2 段組み ----


def _text_runs(x, top, count, *, size=10.0, chars=20, step=18.0):
    """左端が x で、全角 chars 文字の長い行を count 本、上から並べた文字列。"""
    return [(top - step * i, x, size, "あ" * chars) for i in range(count)]


def test_a_two_column_page_is_read_title_then_left_column_then_right_column():
    title = [(800.0, 50.0, 16.0, "題" * 30)]  # 全幅（段の間をまたぐ）
    left, right = _text_runs(50.0, 760.0, 10), _text_runs(300.0, 760.0, 10)

    groups = pdf_parser._split_columns([*title, *right, *left])  # 入力の順は関係ない

    assert [len(g) for g in groups] == [1, 10, 10]
    assert groups[0] == title
    assert sorted(groups[1]) == sorted(left) and sorted(groups[2]) == sorted(right)


def test_a_one_column_page_is_not_split():
    lines = _text_runs(50.0, 760.0, 12, chars=45)  # 段落の行は、ページの幅いっぱい
    assert pdf_parser._split_columns(lines) == [lines]


def test_a_page_of_short_table_cells_is_not_split_into_columns():
    cells = [(760.0 - 18 * i, x, 10.0, "短い") for i in range(12) for x in (50.0, 150.0, 300.0, 400.0)]
    assert pdf_parser._split_columns(cells) == [cells]


def test_a_block_beside_the_text_that_is_much_shorter_is_not_a_column():
    left = _text_runs(50.0, 760.0, 30, chars=20)
    box = _text_runs(300.0, 760.0, 8, chars=20)  # 縦の範囲が左の 3 割未満
    runs = [*left, *box]
    assert pdf_parser._split_columns(runs) == [runs]


HIDAMARI = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "two_column" / "hidamari_waste_guide.pdf"


@pytest.mark.skipif(not HIDAMARI.exists(), reason="評価用の PDF（evals/fixtures/two_column/）が無い")
def test_the_two_column_pdf_keeps_sentences_whole_and_its_wrapped_table_readable(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 1600)
    chunks = chunk_pages(extract_pages(HIDAMARI))
    text = "\n".join(c.text for c in chunks)

    # 左右の段の文が混ざらず、段落の途中で切れない
    assert "ペットボトルは、キャップとラベルを外し、軽くすすいでからつぶします。" in text
    assert "指定袋は、大 (45L) 10 枚入りが 450 円" in text
    assert "年末年始は、12 月 29 日から 1 月 3 日まで、すべての収集を休みます。" in text
    # 段の中の表は、折り返したセルも 1 行になり、2 ページにまたがっても 1 つ
    (table,) = [c for c in chunks if c.kind == "table"]
    assert (table.page, table.page_end) == (1, 2)
    assert table.text.split("\n")[0] == "地区 燃やせるごみ 燃やせないごみ 資源"
    assert "緑ヶ丘 火・金 第1・第3水曜 木" in table.text.split("\n")
    assert "若葉町 火・金 第1・第3月曜 日" in table.text.split("\n")
