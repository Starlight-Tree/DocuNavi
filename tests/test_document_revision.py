"""資料の版（revisions.csv → documents.revision → 資料一覧）のテスト。実 DB・実 API・R2 は使わない。

確かめるのは、一覧の読み取りと検査（形が悪ければ止める）、取り込みでの扱い（一覧がある = 一覧が正・無い = 触れない）、
版だけが変わったときに SQL が 1 本で済むこと、D1Repository の版の扱い（偽の Worker = 本物の SQLite）、API の応答。
"""

import unicodedata

import pytest

from app.d1 import SyncD1
from app.ingestion import chunker
from app.ingestion.repository import KEEP_REVISION, D1Repository, MemoryRepository
from app.ingestion.revisions import (
    MAX_REVISION_CHARS,
    RevisionsError,
    clean_revision,
    load_revisions,
    match_key,
    parse_revisions,
    unmatched,
)
from app.ingestion.service import ingest
from tests.pdf_fixtures import build_pdf, paragraphs

BODY = [f"第{i}段落の本文はここに書かれています。" for i in range(1, 5)]


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 400)


def _pdf(path, body=BODY):
    path.write_bytes(build_pdf([paragraphs(*body)]))
    return path


# --- 一覧の読み取りと検査 -----------------------------------------------------------------------------------


def test_a_valid_list_is_read_with_japanese_values_and_blank_lines():
    data = 'source,revision\na.pdf,初版\n\nsub/b.pdf,改訂 2\n"c,d.pdf",2026年4月1日時点\n'.encode()
    assert parse_revisions(data) == {"a.pdf": "初版", "sub/b.pdf": "改訂 2", "c,d.pdf": "2026年4月1日時点"}


def test_a_utf8_bom_and_crlf_are_accepted():
    assert parse_revisions("﻿source,revision\r\na.pdf,初版\r\n".encode()) == {"a.pdf": "初版"}


def test_surrounding_spaces_are_trimmed():
    assert parse_revisions("source,revision\n a.pdf , 改訂 2 \n".encode()) == {"a.pdf": "改訂 2"}


@pytest.mark.parametrize(
    "text",
    [
        "",  # ヘッダーなし
        "a.pdf,初版\n",  # ヘッダー違い
        "revision,source\na.pdf,初版\n",  # 列の順が違う
        "source,revision\na.pdf\n",  # 列が足りない
        "source,revision\na.pdf,初版,余分\n",  # 列が多い
        "source,revision\n,初版\n",  # source が空
        "source,revision\na.pdf,\n",  # 版が空
        "source,revision\na.pdf,   \n",
        f"source,revision\na.pdf,{'版' * (MAX_REVISION_CHARS + 1)}\n",  # 長すぎる
        'source,revision\na.pdf,"初版\n改訂"\n',  # 改行を含む
        "source,revision\na.pdf,初版\x00\n",  # 制御文字
        "source,revision\na.pdf,初版\na.pdf,改訂 2\n",  # 重複
    ],
)
def test_a_malformed_list_is_rejected(text):
    with pytest.raises(RevisionsError):
        parse_revisions(text.encode())


def test_a_non_utf8_or_oversized_list_is_rejected():
    with pytest.raises(RevisionsError):
        parse_revisions("source,revision\na.pdf,初版\n".encode("shift_jis"))
    with pytest.raises(RevisionsError):
        parse_revisions(b"x" * (256 * 1024 + 1))


def test_the_error_message_never_contains_the_values():
    with pytest.raises(RevisionsError) as info:
        parse_revisions("source,revision\nsecret.pdf,初版\nsecret.pdf,別\n".encode())
    assert "secret.pdf" not in str(info.value)
    assert "2 行目" in str(info.value) or "3 行目" in str(info.value)


def test_the_maximum_length_is_accepted():
    assert clean_revision("版" * MAX_REVISION_CHARS) == "版" * MAX_REVISION_CHARS
    assert clean_revision("版" * (MAX_REVISION_CHARS + 1)) is None


def test_load_returns_none_when_there_is_no_file(tmp_path):
    assert load_revisions(tmp_path) is None
    (tmp_path / "revisions.csv").write_text("source,revision\na.pdf,初版\n", encoding="utf-8")
    assert load_revisions(tmp_path) == {"a.pdf": "初版"}


def test_unmatched_lists_the_sources_without_a_pdf():
    assert unmatched({"a.pdf": "初版", "typo.pdf": "初版", "b.pdf": "x"}, {"a.pdf", "b.pdf"}) == ["typo.pdf"]


# --- 取り込みでの扱い（メモリ）--------------------------------------------------------------------------------


def test_a_list_sets_the_listed_revision_and_clears_the_unlisted(tmp_path):
    a, b = _pdf(tmp_path / "a.pdf"), _pdf(tmp_path / "b.pdf", BODY[:3])
    repo = MemoryRepository()
    ingest([a, b], tmp_path, repo, apply=True, revisions={"a.pdf": "初版"})
    assert repo.documents["a.pdf"]["revision"] == "初版"
    assert repo.documents["b.pdf"]["revision"] is None
    ingest([a, b], tmp_path, repo, apply=True, revisions={"b.pdf": "改訂 2"})  # 一覧が正: a の版は消える
    assert repo.documents["a.pdf"]["revision"] is None
    assert repo.documents["b.pdf"]["revision"] == "改訂 2"


def test_no_list_leaves_the_stored_revision_alone(tmp_path):
    a = _pdf(tmp_path / "a.pdf")
    repo = MemoryRepository()
    ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "初版"})
    (report,) = ingest([a], tmp_path, repo, apply=True)  # 一覧なし（None）
    assert repo.documents["a.pdf"]["revision"] == "初版"
    assert report.revision is None and report.saved is False


def test_a_dry_run_saves_nothing(tmp_path):
    a = _pdf(tmp_path / "a.pdf")
    repo = MemoryRepository()
    (report,) = ingest([a], tmp_path, repo, apply=False, revisions={"a.pdf": "初版"})
    assert report.revision == "初版"
    assert repo.documents == {}


def test_a_changed_pdf_keeps_the_listed_revision(tmp_path):
    a = _pdf(tmp_path / "a.pdf")
    repo = MemoryRepository()
    ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "初版"})
    _pdf(a, BODY[:2])
    ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "改訂 2"})
    assert repo.documents["a.pdf"]["revision"] == "改訂 2"
    assert len(repo.documents["a.pdf"]["chunks"]) >= 1


def test_a_failed_pdf_does_not_touch_the_stored_revision(tmp_path):
    a = _pdf(tmp_path / "a.pdf")
    repo = MemoryRepository()
    ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "初版"})
    a.write_bytes(b"broken")
    (report,) = ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "改訂 2"})
    assert report.status == "failed"
    assert repo.documents["a.pdf"]["revision"] == "初版"


def test_the_default_is_the_keep_marker():
    import inspect

    assert inspect.signature(D1Repository.save).parameters["revision"].default is KEEP_REVISION
    assert inspect.signature(MemoryRepository.save).parameters["revision"].default is KEEP_REVISION


# --- D1Repository（偽の Worker = 本物の SQLite）--------------------------------------------------------------


def _repo(worker):
    return D1Repository(SyncD1("https://worker.test", transport=worker.transport()))


def _revision(worker):
    return worker.rows("SELECT revision FROM documents")[0]["revision"]


def test_d1_save_keeps_sets_or_clears_the_revision(d1_worker):
    repo = _repo(d1_worker)
    repo.save("a.pdf", "h", 2, [], revision="初版")
    assert _revision(d1_worker) == "初版"
    repo.save("a.pdf", "h", 2, [])  # 渡さない → 残す
    assert _revision(d1_worker) == "初版"
    repo.save("a.pdf", "h", 2, [], revision="改訂 2")
    assert _revision(d1_worker) == "改訂 2"
    repo.save("a.pdf", "h", 2, [], revision=None)  # None → 版なし
    assert _revision(d1_worker) is None


def test_d1_mark_failed_keeps_the_stored_revision(d1_worker):
    repo = _repo(d1_worker)
    repo.save("a.pdf", "h", 2, [], revision="初版")
    repo.mark_failed("a.pdf", "h2", "garbled")
    assert _revision(d1_worker) == "初版"


def test_d1_set_revision_changes_only_the_revision(d1_worker, tmp_path):
    repo = _repo(d1_worker)
    a = _pdf(tmp_path / "a.pdf")
    ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "初版"})
    before = d1_worker.rows("SELECT id, chunk_index FROM chunks ORDER BY id")
    repo.set_revision("a.pdf", "改訂 2")
    assert _revision(d1_worker) == "改訂 2"
    assert d1_worker.rows("SELECT id, chunk_index FROM chunks ORDER BY id") == before
    repo.set_revision("missing.pdf", "x")  # 文書が無ければ何もしない
    assert len(d1_worker.rows("SELECT id FROM documents")) == 1


def test_a_revision_only_change_writes_one_statement_and_not_the_chunks(d1_worker, tmp_path):
    repo = _repo(d1_worker)
    a = _pdf(tmp_path / "a.pdf")
    ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "初版"})
    before = d1_worker.rows("SELECT id, chunk_index, content_hash FROM chunks ORDER BY id")

    (same,) = ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "初版"})  # 何も変わらない
    assert same.saved is False

    sent: list[str] = []
    original_batch = repo._db.batch  # execute() も batch() を通るので、batch だけを見れば、流した SQL が全部入る
    repo._db.batch = lambda statements: (sent.extend(q for q, _ in statements), original_batch(statements))[1]
    (changed,) = ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "改訂 2"})

    assert changed.saved is True and changed.status == "unchanged"
    assert _revision(d1_worker) == "改訂 2"
    assert d1_worker.rows("SELECT id, chunk_index, content_hash FROM chunks ORDER BY id") == before
    writes = [q for q in sent if not q.lstrip().upper().startswith("SELECT")]
    assert len(writes) == 1 and writes[0].startswith("UPDATE documents SET revision")  # 書き込みは 1 本だけ


def test_the_migration_files_have_the_revision_column():
    from pathlib import Path

    from tests.d1_fakes import MIGRATION

    assert "revision       TEXT" in MIGRATION.read_text(encoding="utf-8")
    pg = Path(__file__).resolve().parent.parent / "db" / "migrations" / "0006_document_revision.sql"
    assert "ADD COLUMN revision text" in pg.read_text(encoding="utf-8")


# --- 取り込みコマンド -----------------------------------------------------------------------------------------


def test_the_command_stops_with_2_on_a_malformed_list_and_saves_nothing(tmp_path, monkeypatch, capsys):
    from app.ingestion.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("D1_API_URL", raising=False)
    _pdf(tmp_path / "a.pdf")
    (tmp_path / "revisions.csv").write_text("source,revision\na.pdf,\n", encoding="utf-8")
    assert main(["--input", str(tmp_path), "--apply"]) == 2
    err = capsys.readouterr().err
    assert "何も保存していません" in err and "2 行目" in err


def test_the_command_shows_the_revision_and_warns_about_a_misspelled_source(tmp_path, monkeypatch, capsys):
    from app.ingestion.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("D1_API_URL", raising=False)
    _pdf(tmp_path / "a.pdf")
    (tmp_path / "revisions.csv").write_text("source,revision\na.pdf,改訂 2\ntypo.pdf,初版\n", encoding="utf-8")
    assert main(["--input", str(tmp_path)]) == 0
    captured = capsys.readouterr()
    assert "revision=改訂 2" in captured.out
    assert "typo.pdf" in captured.err


def test_the_command_without_a_list_shows_no_revision(tmp_path, monkeypatch, capsys):
    from app.ingestion.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("D1_API_URL", raising=False)
    _pdf(tmp_path / "a.pdf")
    assert main(["--input", str(tmp_path)]) == 0
    assert "revision=(unchanged)" in capsys.readouterr().out


def test_the_r2_fetch_also_downloads_the_list_beside_the_pdfs(tmp_path, monkeypatch):
    from app import r2_store
    from app.ingestion.__main__ import _fetch_from_r2
    from tests.test_r2_store import PDF_A, FakeS3

    s3 = FakeS3({"cafe/a.pdf": PDF_A, "cafe/revisions.csv": b"source,revision\na.pdf,x\n"})
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(s3, "b"))
    root = _fetch_from_r2("r2://cafe", tmp_path)
    assert root == tmp_path / "cafe"
    assert load_revisions(root) == {"a.pdf": "x"}
    one = tmp_path / "one"
    root_one = _fetch_from_r2("r2://cafe/a.pdf", one)  # 1 ファイルだけのときも、一覧は同じ場所に取る
    assert load_revisions(root_one.parent) == {"a.pdf": "x"}


def test_the_r2_fetch_without_a_list_leaves_none(tmp_path, monkeypatch):
    from app import r2_store
    from app.ingestion.__main__ import _fetch_from_r2
    from tests.test_r2_store import PDF_A, FakeS3

    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(FakeS3({"cafe/a.pdf": PDF_A}), "b"))
    assert load_revisions(_fetch_from_r2("r2://cafe", tmp_path)) is None


# --- レビューで見つかった事故の再発防止 --------------------------------------------------------------------------


def test_a_header_only_list_clears_every_stored_revision_unlike_a_missing_file(tmp_path):
    a = _pdf(tmp_path / "a.pdf")
    repo = MemoryRepository()
    ingest([a], tmp_path, repo, apply=True, revisions={"a.pdf": "初版"})
    (tmp_path / "revisions.csv").write_text("source,revision\n", encoding="utf-8")
    assert load_revisions(tmp_path) == {}  # 一覧はある（None ではない）
    ingest([a], tmp_path, repo, apply=True, revisions=load_revisions(tmp_path))
    assert repo.documents["a.pdf"]["revision"] is None


def test_the_list_matches_a_filename_whose_dakuten_is_written_differently(tmp_path):
    nfc, nfd = "ガイド.pdf", unicodedata.normalize("NFD", "ガイド.pdf")
    assert nfc != nfd
    revisions = parse_revisions(f"source,revision\n{nfc},改訂 2\n".encode())
    repo = MemoryRepository()
    (report,) = ingest([_pdf(tmp_path / nfd)], tmp_path, repo, apply=True, revisions=revisions)
    assert report.revision == "改訂 2"
    assert next(iter(repo.documents)) == nfd  # 保存する source は変えない
    assert unmatched(revisions, [nfd]) == []
    assert match_key(nfd) == nfc


def test_nfc_and_nfd_spellings_of_one_source_count_as_a_duplicate():
    nfc, nfd = "ガイド.pdf", unicodedata.normalize("NFD", "ガイド.pdf")
    with pytest.raises(RevisionsError):
        parse_revisions(f"source,revision\n{nfc},初版\n{nfd},改訂 2\n".encode())


def test_an_unclosed_quote_is_a_revisions_error_not_a_csv_error():
    with pytest.raises(RevisionsError):
        parse_revisions(b'source,revision\na.pdf,"' + b"a" * 140000)


def test_the_command_stops_with_2_when_no_source_in_the_list_matches(tmp_path, monkeypatch, capsys):
    from app.ingestion.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("D1_API_URL", raising=False)
    _pdf(tmp_path / "a.pdf")
    (tmp_path / "revisions.csv").write_text(
        "source,revision\ntmp/a.pdf,初版\n", encoding="utf-8"
    )  # 資料群の名前を付けてしまった
    assert main(["--input", str(tmp_path), "--apply"]) == 2
    assert "何も保存していません" in capsys.readouterr().err


def test_the_command_shows_unchanged_when_there_is_no_list(tmp_path, monkeypatch, capsys):
    from app.ingestion.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("D1_API_URL", raising=False)
    _pdf(tmp_path / "a.pdf")
    assert main(["--input", str(tmp_path)]) == 0
    assert "revision=(unchanged)" in capsys.readouterr().out


def test_a_partly_unmatched_list_warns_and_the_unlisted_pdf_gets_no_revision(tmp_path, monkeypatch, capsys):
    from app.ingestion.__main__ import main

    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("D1_API_URL", raising=False)
    _pdf(tmp_path / "a.pdf")
    _pdf(tmp_path / "b.pdf", BODY[:3])
    (tmp_path / "revisions.csv").write_text("source,revision\na.pdf,改訂 2\nz.pdf,初版\n", encoding="utf-8")
    assert main(["--input", str(tmp_path)]) == 0  # 一部だけ合わない: 警告して進む
    out = capsys.readouterr()
    assert "revision=改訂 2" in out.out and "revision=(none)" in out.out  # b.pdf は一覧に無いので「版なし」
    assert "z.pdf" in out.err


def test_an_oversized_list_in_r2_stops_with_2_instead_of_being_ignored(tmp_path, monkeypatch, capsys):
    from app import r2_store
    from app.ingestion.__main__ import main
    from tests.test_r2_store import PDF_A, FakeS3

    big = b"source,revision\n" + b"x" * (256 * 1024 + 10)
    s3 = FakeS3({"cafe/a.pdf": PDF_A, "cafe/revisions.csv": big})
    monkeypatch.setattr(r2_store, "default_store", lambda: r2_store.R2Store(s3, "b"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("D1_API_URL", raising=False)
    assert main(["--input", "r2://cafe", "--apply"]) == 2
    assert "何も保存していません" in capsys.readouterr().err
