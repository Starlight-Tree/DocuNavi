"""PDF の取り込みを、本物の Postgres で検証する（TEST_DATABASE_URL があるときだけ動く）。

テストごとに専用のスキーマを作り（search_path をそこへ向ける）、終わったら DROP SCHEMA ... CASCADE で消す。
実行例: TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres pytest tests/test_ingestion_postgres.py
"""

import os
import uuid

import psycopg
import pytest
from psycopg.conninfo import make_conninfo

from app.ingestion import __main__ as cli
from app.ingestion import chunker
from app.ingestion.repository import PostgresRepository
from app.ingestion.service import ingest
from tests.pdf_fixtures import build_pdf, paragraphs

TEST_DATABASE_URL = os.environ.get("TEST_DATABASE_URL", "").strip()

pytestmark = pytest.mark.skipif(not TEST_DATABASE_URL, reason="TEST_DATABASE_URL が未設定")

BODY = [f"第{i}段落の本文はここに書かれています。" for i in range(1, 9)]


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 400)
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)


@pytest.fixture
def db(monkeypatch):
    """専用スキーマを作り、DATABASE_URL をそこへ向ける。(接続情報, スキーマ名) を返す。"""
    schema = f"zingest_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
        admin.execute(f'CREATE SCHEMA "{schema}"')
    conninfo = make_conninfo(TEST_DATABASE_URL, options=f"-c search_path={schema}", application_name=schema)
    monkeypatch.setenv("DATABASE_URL", conninfo)
    try:
        yield conninfo, schema
    finally:
        with psycopg.connect(TEST_DATABASE_URL, autocommit=True) as admin:
            admin.execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')


def _pdf(path, pages):
    """pages: ページごとの段落（文字列）のリスト。段落の間に 1 行分の空きを入れる。"""
    path.write_bytes(build_pdf([paragraphs(*page) for page in pages]))
    return path


def _rows(conninfo, sql, params=()):
    with psycopg.connect(conninfo) as conn:
        return conn.execute(sql, params).fetchall()


def _run(conninfo, pdfs, root, *, apply):
    with psycopg.connect(conninfo, prepare_threshold=None) as conn:
        repo = PostgresRepository(conn, create_tables=apply)
        return ingest(pdfs, root, repo, apply=apply)


def test_dry_run_creates_no_tables(db, tmp_path):
    conninfo, schema = db
    pdf = _pdf(tmp_path / "guide.pdf", [BODY[:4]])
    (report,) = _run(conninfo, [pdf], tmp_path, apply=False)

    assert report.status == "would_insert"
    assert _rows(conninfo, "SELECT 1 FROM information_schema.tables WHERE table_schema = %s", (schema,)) == []


def test_apply_creates_the_tables_only_in_the_target_schema(db, tmp_path):
    conninfo, schema = db
    pdf = _pdf(tmp_path / "guide.pdf", [BODY[:4]])
    _run(conninfo, [pdf], tmp_path, apply=True)

    names = {r[0] for r in _rows(conninfo, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))}
    assert names == {"documents", "chunks"}


def test_applying_twice_keeps_every_chunk_row(db, tmp_path):
    conninfo, _ = db
    pdf = _pdf(tmp_path / "guide.pdf", [BODY[:4], BODY[4:]])
    (first,) = _run(conninfo, [pdf], tmp_path, apply=True)
    ids_before = _rows(conninfo, "SELECT id FROM chunks ORDER BY chunk_index")
    (second,) = _run(conninfo, [pdf], tmp_path, apply=True)

    assert (first.status, second.status) == ("inserted", "unchanged")
    assert second.new_chunks == 0
    assert _rows(conninfo, "SELECT id FROM chunks ORDER BY chunk_index") == ids_before
    assert _rows(conninfo, "SELECT count(*) FROM documents") == [(1,)]


def test_editing_one_paragraph_replaces_only_that_chunk_row(db, tmp_path):
    conninfo, _ = db
    pdf = tmp_path / "guide.pdf"
    _pdf(pdf, [BODY[:6], BODY[6:]])
    (first,) = _run(conninfo, [pdf], tmp_path, apply=True)
    ids_before = {r[0] for r in _rows(conninfo, "SELECT id FROM chunks")}

    edited = list(BODY)
    edited[2] = "第3段落は、書き換えられました。"
    _pdf(pdf, [edited[:6], edited[6:]])
    (second,) = _run(conninfo, [pdf], tmp_path, apply=True)

    ids_after = {r[0] for r in _rows(conninfo, "SELECT id FROM chunks")}
    assert second.status == "updated"
    assert (second.new_chunks, second.removed_chunks) == (1, 1)
    assert len(ids_before & ids_after) == first.chunks - 1
    texts = [r[0] for r in _rows(conninfo, "SELECT content FROM chunks")]
    assert any("書き換えられました" in t for t in texts)
    assert not any("第3段落の本文" in t for t in texts)


def test_a_paragraph_inserted_on_page_one_shifts_indexes_without_a_unique_violation(db, tmp_path):
    conninfo, _ = db
    pdf = tmp_path / "guide.pdf"
    _pdf(pdf, [BODY[:4], BODY[4:]])
    _run(conninfo, [pdf], tmp_path, apply=True)
    page2_ids = {r[0] for r in _rows(conninfo, "SELECT id FROM chunks WHERE page = 2")}

    _pdf(pdf, [["追加された段落です。", *BODY[:4]], BODY[4:]])
    (report,) = _run(conninfo, [pdf], tmp_path, apply=True)

    assert report.status == "updated"
    assert (report.new_chunks, report.removed_chunks) == (1, 0)
    indexes = [r[0] for r in _rows(conninfo, "SELECT chunk_index FROM chunks ORDER BY chunk_index")]
    assert indexes == list(range(len(indexes)))
    # 2 ページ目のチャンクは、番号が 1 つずれても行（id）は同じ
    assert {r[0] for r in _rows(conninfo, "SELECT id FROM chunks WHERE page = 2")} == page2_ids


def test_a_failed_pdf_is_recorded_and_old_chunks_remain(db, tmp_path):
    conninfo, _ = db
    pdf = tmp_path / "guide.pdf"
    _pdf(pdf, [BODY[:4]])
    (first,) = _run(conninfo, [pdf], tmp_path, apply=True)

    _pdf(pdf, [[]])
    (report,) = _run(conninfo, [pdf], tmp_path, apply=True)

    assert (report.status, report.reason) == ("failed", "empty")
    assert _rows(conninfo, "SELECT status, failure_reason FROM documents") == [("failed", "empty")]
    assert _rows(conninfo, "SELECT count(*) FROM chunks") == [(first.chunks,)]

    # 直ったら ready に戻る
    _pdf(pdf, [BODY[:4]])
    (again,) = _run(conninfo, [pdf], tmp_path, apply=True)
    assert again.status == "unchanged"
    assert _rows(conninfo, "SELECT status, failure_reason FROM documents") == [("ready", None)]


def test_a_new_pdf_that_fails_leaves_a_failed_document_row(db, tmp_path):
    conninfo, _ = db
    bad = tmp_path / "bad.pdf"
    bad.write_bytes(b"nope")
    (report,) = _run(conninfo, [bad], tmp_path, apply=True)

    assert report.status == "failed"
    assert _rows(conninfo, "SELECT source, status, failure_reason FROM documents") == [
        ("bad.pdf", "failed", "unreadable")
    ]
    assert _rows(conninfo, "SELECT count(*) FROM chunks") == [(0,)]


def test_cli_apply_uses_database_url(db, tmp_path, capsys):
    conninfo, _ = db
    _pdf(tmp_path / "guide.pdf", [BODY[:4], BODY[4:]])

    assert cli.main(["--input", str(tmp_path), "--apply"]) == 0
    assert "status=inserted" in capsys.readouterr().out
    assert cli.main(["--input", str(tmp_path), "--apply"]) == 0
    assert "status=unchanged" in capsys.readouterr().out
    # 確認だけのときは、保存済みと比べる
    assert cli.main(["--input", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "status=unchanged" in out
    assert "new_chunks=0" in out
    assert _rows(conninfo, "SELECT count(*) FROM documents") == [(1,)]


def test_a_database_error_does_not_leak_the_url(monkeypatch, tmp_path, capsys):
    _pdf(tmp_path / "guide.pdf", [BODY[:2]])
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:s3cretpw@127.0.0.1:1/none?connect_timeout=2")
    assert cli.main(["--input", str(tmp_path), "--apply"]) == 1
    err = capsys.readouterr().err
    assert "s3cretpw" not in err
    assert "DB の操作に失敗しました" in err
