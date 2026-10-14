"""PDF の取り込み（app/ingestion）の、DB を使わない部分。

本物の Postgres での保存・差分は tests/test_ingestion_postgres.py（TEST_DATABASE_URL があるときだけ動く）。
PDF は tests/pdf_fixtures.py が組み立てる最小のもの（抽出専用。実物の PDF での確認は evals/corpora/ で別に行う）。
"""

import io
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter

from app.ingestion import __main__ as cli
from app.ingestion import chunker
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


def test_a_table_that_continues_on_the_next_page_gets_the_header_back():
    chunks = _layout([[ROW_HEADER, ROWS[0]], [ROWS[1], ROWS[2]]])
    assert [c.page for c in chunks] == [1, 2]
    assert chunks[1].text.split("\n") == ["店舗名 エリア 座席数", "港町ベイ店 港町 42席", "湖畔テラス店 湖畔 76席"]


def test_a_repeated_header_on_the_next_page_is_not_doubled():
    chunks = _layout([[ROW_HEADER, ROWS[0]], [ROW_HEADER, ROWS[1], ROWS[2]]])
    assert chunks[1].text.split("\n")[:2] == ["店舗名 エリア 座席数", "港町ベイ店 港町 42席"]
    assert chunks[1].text.count("店舗名") == 1


def test_a_long_table_is_split_by_rows_and_each_part_repeats_the_header(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 60)
    rows = [(f"店舗{i}", "港町", f"{i}席") for i in range(1, 9)]
    chunks = _layout([[ROW_HEADER, *rows]])

    assert len(chunks) > 1
    assert all(c.text.split("\n")[0] == "店舗名 エリア 座席数" for c in chunks)
    body = [line for c in chunks for line in c.text.split("\n")[1:]]
    assert body == [f"店舗{i} 港町 {i}席" for i in range(1, 9)]  # 欠けも重複もない


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
def test_the_cafe_pdf_keeps_its_table_across_the_page_break():
    pages = extract_pages(CAFE_PDF)
    chunks = chunk_pages(pages)

    tables = [c for c in chunks if c.text.startswith("店舗名 エリア 住所")]
    assert [c.page for c in tables] == [3, 4]  # 表は 2 ページにまたがる
    store_rows = [line for c in tables for line in c.text.split("\n")[1:]]
    assert len(store_rows) == 12
    assert any(row.startswith("湖畔テラス店 湖畔 みなと市湖畔1-3-7 9:00〜20:00 なし 76席") for row in store_rows)
    assert {c.heading for c in tables} == {"店舗一覧"}
    # ページ番号は本文に入らない
    assert not any("3 / 4" in c.text or "4 / 4" in c.text for c in chunks)
    # 2 回読んでも、同じチャンク・同じハッシュ
    assert [c.content_hash for c in chunks] == [c.content_hash for c in chunk_pages(extract_pages(CAFE_PDF))]
