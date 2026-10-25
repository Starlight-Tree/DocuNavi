"""PDF の取り込みを、本物の Postgres で検証する（TEST_DATABASE_URL があるときだけ動く）。

テストごとに専用のスキーマを作り（search_path をそこへ向ける）、終わったら DROP SCHEMA ... CASCADE で消す。
実行例: TEST_DATABASE_URL=postgresql://postgres@127.0.0.1:5432/postgres pytest tests/test_ingestion_postgres.py
"""

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

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

    names = {
        r[0]
        for r in _rows(conninfo, "SELECT table_name FROM information_schema.tables WHERE table_schema = %s", (schema,))
    }
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


def _table_pdf(path):
    rows = [
        ("店舗名", "エリア", "座席数"),
        ("港町本店", "港町", "68席"),
        ("港町ベイ店", "港町", "42席"),
        ("湖畔テラス店", "湖畔", "76席"),
    ]
    path.write_bytes(build_pdf([["前の説明です。", "", rows[0], rows[1]], [rows[2], rows[3], "", "後ろの説明です。"]]))
    return path


def test_a_table_across_pages_is_stored_with_its_page_range_and_kind(db, tmp_path):
    conninfo, _ = db
    pdf = _table_pdf(tmp_path / "guide.pdf")
    (report,) = _run(conninfo, [pdf], tmp_path, apply=True)

    assert report.status == "inserted"
    rows = _rows(conninfo, "SELECT kind, page, page_end FROM chunks ORDER BY chunk_index")
    assert rows == [("text", 1, None), ("table", 1, 2), ("text", 2, None)]
    # 同じ PDF をもう一度: 行も範囲も変わらない
    ids = _rows(conninfo, "SELECT id FROM chunks ORDER BY chunk_index")
    (again,) = _run(conninfo, [pdf], tmp_path, apply=True)
    assert again.status == "unchanged"
    assert _rows(conninfo, "SELECT id FROM chunks ORDER BY chunk_index") == ids


def test_tables_created_before_the_new_columns_are_upgraded(db, tmp_path):
    conninfo, _ = db
    with psycopg.connect(conninfo, autocommit=True) as conn:  # page_end・kind が無かった頃の定義
        conn.execute(
            """
            CREATE TABLE documents (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source text NOT NULL UNIQUE,
                content_hash text NOT NULL, status text NOT NULL, failure_reason text,
                pages integer NOT NULL DEFAULT 0, created_at timestamptz NOT NULL DEFAULT now(),
                updated_at timestamptz NOT NULL DEFAULT now());
            CREATE TABLE chunks (
                id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
                document_id bigint NOT NULL REFERENCES documents (id) ON DELETE CASCADE,
                chunk_index integer NOT NULL, page integer NOT NULL, heading text,
                content text NOT NULL, content_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
                UNIQUE (document_id, chunk_index) DEFERRABLE INITIALLY DEFERRED);
            """
        )
    pdf = _table_pdf(tmp_path / "guide.pdf")
    _run(conninfo, [pdf], tmp_path, apply=True)

    assert _rows(conninfo, "SELECT kind, page_end FROM chunks WHERE kind = 'table'") == [("table", 2)]


def test_an_unchanged_table_that_moves_to_another_page_keeps_its_row_and_gets_new_pages(db, tmp_path):
    conninfo, _ = db
    header, a, b, c = (
        ("店舗名", "エリア", "座席数"),
        ("港町本店", "港町", "68席"),
        ("港町ベイ店", "港町", "42席"),
        ("湖畔テラス店", "湖畔", "76席"),
    )
    pdf = tmp_path / "guide.pdf"
    pdf.write_bytes(build_pdf([[header, a], [b, c]]))  # 表は 1〜2 ページ
    _run(conninfo, [pdf], tmp_path, apply=True)
    before = _rows(conninfo, "SELECT id, page, page_end FROM chunks WHERE kind = 'table'")
    assert [(r[1], r[2]) for r in before] == [(1, 2)]

    pdf.write_bytes(build_pdf([["前書きです。"], [header, a, b, c]]))  # 同じ表が、2 ページ目だけに入る
    (report,) = _run(conninfo, [pdf], tmp_path, apply=True)

    after = _rows(conninfo, "SELECT id, page, page_end FROM chunks WHERE kind = 'table'")
    assert (report.new_chunks, report.removed_chunks) == (1, 0)  # 増えたのは前書きだけ
    assert after == [(before[0][0], 2, None)]  # 同じ行で、ページだけが変わる


# --- 題名（documents.title）。実 SQL での「渡さない = 残す／None = 消す／文字列 = 置き換える」 ---------------------


def _titled(path, title, pages=None):
    path.write_bytes(build_pdf([paragraphs(*(pages or BODY[:4]))], title=title))
    return path


def _title_of(conninfo):
    return _rows(conninfo, "SELECT title FROM documents")[0][0]


def test_apply_stores_the_pdf_title_and_a_plain_save_without_title_keeps_it(db, tmp_path):
    conninfo, _ = db
    pdf = _titled(tmp_path / "guide.pdf", "つばさ大学学生寮 入寮規程")
    _run(conninfo, [pdf], tmp_path, apply=True)
    assert _title_of(conninfo) == "つばさ大学学生寮 入寮規程"

    with psycopg.connect(conninfo, prepare_threshold=None) as conn:
        PostgresRepository(conn, create_tables=False).save("guide.pdf", "other-hash", 1, [])  # title を渡さない
    assert _title_of(conninfo) == "つばさ大学学生寮 入寮規程"


def test_reingesting_a_pdf_without_a_title_clears_it_and_a_changed_title_replaces_it(db, tmp_path):
    conninfo, _ = db
    pdf = tmp_path / "guide.pdf"
    _titled(pdf, "旧題名")
    _run(conninfo, [pdf], tmp_path, apply=True)
    _titled(pdf, "新題名")
    _run(conninfo, [pdf], tmp_path, apply=True)
    assert _title_of(conninfo) == "新題名"
    _titled(pdf, None)
    _run(conninfo, [pdf], tmp_path, apply=True)
    assert _title_of(conninfo) is None


def test_a_failed_pdf_keeps_the_stored_title(db, tmp_path):
    conninfo, _ = db
    pdf = _titled(tmp_path / "guide.pdf", "題名")
    _run(conninfo, [pdf], tmp_path, apply=True)
    pdf.write_bytes(b"broken")
    _run(conninfo, [pdf], tmp_path, apply=True)
    assert _rows(conninfo, "SELECT status, title FROM documents") == [("failed", "題名")]


def test_apply_adds_the_title_column_to_a_table_made_before_it_existed(db, tmp_path):
    conninfo, _ = db
    with psycopg.connect(conninfo, autocommit=True) as conn:
        conn.execute(
            "CREATE TABLE documents (id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY, source text NOT NULL UNIQUE, "
            "content_hash text NOT NULL, status text NOT NULL CHECK (status IN ('ready', 'failed')), "
            "failure_reason text, pages integer NOT NULL DEFAULT 0, "
            "created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now())"
        )
        conn.execute("INSERT INTO documents (source, content_hash, status) VALUES ('old.pdf', 'h', 'ready')")
    pdf = _titled(tmp_path / "guide.pdf", "題名")
    _run(conninfo, [pdf], tmp_path, apply=True)
    assert _rows(conninfo, "SELECT source, title FROM documents ORDER BY source") == [
        ("guide.pdf", "題名"),
        ("old.pdf", None),
    ]


# --- 同じ PDF の同時保存（Queue の Job と毎時の突き合わせ、重複した通知が重なる場合）------------------------------------


class HoldingRepository(PostgresRepository):
    """文書の行ロックを取ったあと、トランザクションの中で hold 秒止まる。別の保存が重なる窓を、確実に作る。"""

    def __init__(self, conn, *, hold, entered, fail=False):
        super().__init__(conn, create_tables=False)
        self._hold, self._entered, self._fail = hold, entered, fail

    def _upsert_document(self, *args, **kwargs):
        doc_id = super()._upsert_document(*args, **kwargs)
        self._entered.set()
        time.sleep(self._hold)
        if self._fail:
            raise RuntimeError("処理の途中で落ちた")
        return doc_id


def _chunks(*texts):
    return [chunker.Chunk(i, 1, None, text, chunker.content_hash(text)) for i, text in enumerate(texts)]


def _stored(conninfo):
    return _rows(conninfo, "SELECT chunk_index, content_hash FROM chunks ORDER BY chunk_index")


def _save_concurrently(conninfo, first, second, *, hold=0.5, first_fails=False):
    """first を保存中（ロックを持ったまま）に second を始める。(second の待ち秒数, first の例外) を返す。"""
    entered = threading.Event()

    def run_first():
        with psycopg.connect(conninfo, prepare_threshold=None) as conn:
            repo = HoldingRepository(conn, hold=hold, entered=entered, fail=first_fails)
            repo.save("guide.pdf", "hash-first", 1, first)

    def run_second():
        assert entered.wait(10)
        with psycopg.connect(conninfo, prepare_threshold=None) as conn:
            started = time.monotonic()
            PostgresRepository(conn, create_tables=False).save("guide.pdf", "hash-second", 1, second)
            return time.monotonic() - started

    with ThreadPoolExecutor(max_workers=2) as pool:
        future_first, future_second = pool.submit(run_first), pool.submit(run_second)
        error = future_first.exception(timeout=30)
        return future_second.result(timeout=30), error


@pytest.fixture
def seeded(db):
    """guide.pdf が 1 件（4 チャンク）保存済みの状態。"""
    conninfo, _ = db
    with psycopg.connect(conninfo, prepare_threshold=None) as conn:
        PostgresRepository(conn, create_tables=True).save("guide.pdf", "hash-0", 1, _chunks("A", "B", "C", "D"))
    return conninfo


def test_two_saves_of_different_content_for_one_pdf_run_one_after_the_other(seeded):
    # 先の保存が文書の行ロックを持つ間、あとの保存は待つ。混ざらず、あとに終わった方の内容がそのまま残る
    waited, error = _save_concurrently(seeded, _chunks("A", "X", "C"), _chunks("D", "C", "B", "Y"))
    assert error is None
    assert waited >= 0.3  # 本当に待った（重なりが起きた）ことの確認
    assert _stored(seeded) == [(i, chunker.content_hash(t)) for i, t in enumerate(("D", "C", "B", "Y"))]
    assert _rows(seeded, "SELECT count(*) FROM documents")[0][0] == 1
    assert _rows(seeded, "SELECT content_hash FROM documents")[0][0] == "hash-second"


def test_two_saves_of_the_same_content_keep_one_row_per_chunk(seeded):
    before = _rows(seeded, "SELECT id FROM chunks ORDER BY chunk_index")
    waited, error = _save_concurrently(seeded, _chunks("A", "B", "C", "D"), _chunks("A", "B", "C", "D"))
    assert error is None
    assert waited >= 0.3
    assert _rows(seeded, "SELECT id FROM chunks ORDER BY chunk_index") == before  # 行は増えず、入れ替わらない


def test_a_save_that_dies_midway_leaves_the_previous_state_and_the_next_save_succeeds(seeded):
    waited, error = _save_concurrently(seeded, _chunks("Z"), _chunks("A", "B", "C", "D", "E"), first_fails=True)
    assert isinstance(error, RuntimeError)  # 先の保存は落ちた（ロールバックされる）
    assert waited >= 0.3
    # 先の保存の途中の状態（Z だけ）は残らず、あとの保存が正しく反映される
    assert _stored(seeded) == [(i, chunker.content_hash(t)) for i, t in enumerate(("A", "B", "C", "D", "E"))]
