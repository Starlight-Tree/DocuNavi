"""文書の題名（PDF の /Title → documents.title → 資料一覧）のテスト。実 DB・実 API は使わない。

確かめるのは、題名の読み取りと整形、保存のときの扱い（渡さない = 残す／None = 消す／文字列 = 置き換える）、
失敗した取り込みが題名に触れないこと、PostgresRepository が出す SQL の引数。
本物の Postgres での確認は tests/test_ingestion_postgres.py（TEST_DATABASE_URL があるときだけ）。
"""

from contextlib import contextmanager

import pytest

from app.ingestion import chunker
from app.ingestion.pdf_parser import MAX_TITLE_CHARS, clean_title, extract_title
from app.ingestion.repository import _SETUP_SQL, KEEP_TITLE, MemoryRepository, PostgresRepository
from app.ingestion.service import ingest
from tests.pdf_fixtures import build_pdf, paragraphs

BODY = [f"第{i}段落の本文はここに書かれています。" for i in range(1, 5)]
TITLE = "つばさ大学学生寮 入寮規程"


@pytest.fixture(autouse=True)
def _small_chunks(monkeypatch):
    monkeypatch.setattr(chunker, "HARD_LIMIT_CHARS", 400)


def _pdf(path, title=None, body=BODY):
    path.write_bytes(build_pdf([paragraphs(*body)], title=title))
    return path


# --- 読み取りと整形 ---------------------------------------------------------------------------------------


def test_the_japanese_title_in_the_metadata_is_read(tmp_path):
    assert extract_title(_pdf(tmp_path / "a.pdf", TITLE)) == TITLE


def test_a_pdf_without_a_title_gives_none(tmp_path):
    assert extract_title(_pdf(tmp_path / "a.pdf")) is None


def test_something_that_is_not_a_pdf_gives_none_without_raising(tmp_path):
    bad = tmp_path / "a.pdf"
    bad.write_bytes(b"not a pdf")
    assert extract_title(bad) is None
    assert extract_title(tmp_path / "missing.pdf") is None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("  みなとコーヒー 店舗案内  ", "みなとコーヒー 店舗案内"),
        ("あおば電機\n全自動洗濯乾燥機\t AW-820", "あおば電機 全自動洗濯乾燥機 AW-820"),
        ("", None),
        ("   \n\t ", None),
        (None, None),
        (123, None),
        (b"bytes", None),
        ("題" * MAX_TITLE_CHARS, "題" * MAX_TITLE_CHARS),
        ("題" * (MAX_TITLE_CHARS + 1), None),  # 切り詰めず、題名なしにする
        ("���題名", None),  # 文字化け
    ],
)
def test_clean_title(raw, expected):
    assert clean_title(raw) == expected


# --- 保存（メモリ）。渡さない = 残す / None = 消す / 文字列 = 置き換える ------------------------------------


def _chunks(tmp_path):
    from app.ingestion.chunker import chunk_pages
    from app.ingestion.pdf_parser import extract_pages

    return chunk_pages(extract_pages(_pdf(tmp_path / "x.pdf")))


def test_memory_save_without_a_title_keeps_the_stored_one(tmp_path):
    repo, chunks = MemoryRepository(), _chunks(tmp_path)
    repo.save("a.pdf", "h1", 1, chunks, title=TITLE)
    repo.save("a.pdf", "h2", 1, chunks)  # 題名を渡さない呼び出し元
    assert repo.documents["a.pdf"]["title"] == TITLE


def test_memory_save_with_none_clears_it_and_with_a_string_replaces_it(tmp_path):
    repo, chunks = MemoryRepository(), _chunks(tmp_path)
    repo.save("a.pdf", "h1", 1, chunks, title=TITLE)
    repo.save("a.pdf", "h1", 1, chunks, title="新しい題名")
    assert repo.documents["a.pdf"]["title"] == "新しい題名"
    repo.save("a.pdf", "h1", 1, chunks, title=None)
    assert repo.documents["a.pdf"]["title"] is None


# --- 取り込み（service） ----------------------------------------------------------------------------------


def test_dry_run_reports_the_title_and_writes_nothing(tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", TITLE)
    repo = MemoryRepository()
    (report,) = ingest([pdf], tmp_path, repo, apply=False)
    assert report.title == TITLE
    assert repo.documents == {}


def test_apply_stores_the_title_and_reapplying_the_same_pdf_keeps_it(tmp_path):
    pdf = _pdf(tmp_path / "a.pdf", TITLE)
    repo = MemoryRepository()
    ingest([pdf], tmp_path, repo, apply=True)
    (again,) = ingest([pdf], tmp_path, repo, apply=True)
    assert again.status == "unchanged"
    assert repo.documents["a.pdf"]["title"] == TITLE


def test_reingesting_a_pdf_that_lost_its_title_clears_the_stored_title(tmp_path):
    """PDF の /Title を正とする。題名の無い PDF を取り込み直すと、保存済みの題名も消える（意図した動き）。"""
    repo = MemoryRepository()
    ingest([_pdf(tmp_path / "a.pdf", TITLE)], tmp_path, repo, apply=True)
    ingest([_pdf(tmp_path / "a.pdf", None)], tmp_path, repo, apply=True)
    assert repo.documents["a.pdf"]["title"] is None


def test_reingesting_with_a_changed_title_replaces_it(tmp_path):
    repo = MemoryRepository()
    ingest([_pdf(tmp_path / "a.pdf", TITLE)], tmp_path, repo, apply=True)
    ingest([_pdf(tmp_path / "a.pdf", "改訂版 入寮規程")], tmp_path, repo, apply=True)
    assert repo.documents["a.pdf"]["title"] == "改訂版 入寮規程"


def test_a_pdf_that_fails_to_ingest_does_not_touch_the_stored_title(tmp_path):
    repo = MemoryRepository()
    ingest([_pdf(tmp_path / "a.pdf", TITLE)], tmp_path, repo, apply=True)
    (tmp_path / "a.pdf").write_bytes(b"broken")
    (report,) = ingest([tmp_path / "a.pdf"], tmp_path, repo, apply=True)
    assert report.status == "failed"
    assert repo.documents["a.pdf"]["title"] == TITLE


# --- PostgresRepository が出す SQL の引数（接続は偽物） -------------------------------------------------------


class _Cursor:
    def __init__(self, rows):
        self._rows = rows

    def fetchone(self):
        return self._rows[0] if self._rows else None

    def fetchall(self):
        return self._rows


class _FakeConn:
    def __init__(self):
        self.calls: list[tuple[str, tuple]] = []

    @contextmanager
    def transaction(self):
        yield

    def execute(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if "INSERT INTO documents" in sql:
            return _Cursor([(1,)])
        return _Cursor([])


def _upsert_params(conn):
    (call,) = [c for c in conn.calls if "INSERT INTO documents" in c[0]]
    return call


# 引数の並び: source, hash, status, reason, pages, title, collection, pages, keep
@pytest.mark.parametrize(
    ("kwargs", "title_param", "keep_param"),
    [
        ({}, None, True),  # 渡さない → 既存を残す
        ({"title": None}, None, False),  # None → 題名なしにする
        ({"title": TITLE}, TITLE, False),  # 文字列 → 置き換える
    ],
)
def test_postgres_save_passes_the_title_and_the_keep_flag(kwargs, title_param, keep_param):
    conn = _FakeConn()
    PostgresRepository(conn, create_tables=False).save("a.pdf", "h", 2, [], **kwargs)
    sql, params = _upsert_params(conn)
    assert params[5] == title_param
    assert params[8] is keep_param
    assert "title = CASE WHEN %s THEN documents.title ELSE EXCLUDED.title END" in sql


def test_postgres_mark_failed_keeps_the_stored_title():
    conn = _FakeConn()
    PostgresRepository(conn, create_tables=False).mark_failed("a.pdf", "h", "garbled")
    _, params = _upsert_params(conn)
    assert params[8] is True


def test_the_default_is_the_keep_marker():
    import inspect

    assert inspect.signature(PostgresRepository.save).parameters["title"].default is KEEP_TITLE
    assert inspect.signature(MemoryRepository.save).parameters["title"].default is KEEP_TITLE


def test_the_setup_sql_has_the_title_column_for_new_and_existing_tables():
    assert "title          text," in _SETUP_SQL
    assert "ALTER TABLE documents ADD COLUMN IF NOT EXISTS title text;" in _SETUP_SQL
