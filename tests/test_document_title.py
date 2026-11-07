"""文書の題名（PDF の /Title → documents.title → 資料一覧）のテスト。実 DB・実 API は使わない。

確かめるのは、題名の読み取りと整形、保存のときの扱い（渡さない = 残す／None = 消す／文字列 = 置き換える）、
失敗した取り込みが題名に触れないこと、D1Repository の題名の扱い（偽の Worker = 本物の SQLite）。
（）。
"""

import pytest

from app.d1 import SyncD1
from app.ingestion import chunker
from app.ingestion.pdf_parser import MAX_TITLE_CHARS, clean_title, extract_title
from app.ingestion.repository import KEEP_TITLE, D1Repository, MemoryRepository
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


# --- D1Repository の題名の扱い（偽の Worker = 本物の SQLite） -------------------------------------------------


def _repo(worker):
    return D1Repository(SyncD1("https://worker.test", transport=worker.transport()))


def _title(worker):
    return worker.rows("SELECT title FROM documents")[0]["title"]


def test_d1_save_keeps_sets_or_clears_the_title(d1_worker):
    repo = _repo(d1_worker)
    repo.save("a.pdf", "h", 2, [], title=TITLE)
    repo.save("a.pdf", "h", 2, [])  # 渡さない → 既存を残す
    assert _title(d1_worker) == TITLE
    repo.save("a.pdf", "h", 2, [], title="別の題名")  # 文字列 → 置き換える
    assert _title(d1_worker) == "別の題名"
    repo.save("a.pdf", "h", 2, [], title=None)  # None → 題名なしにする
    assert _title(d1_worker) is None


def test_d1_mark_failed_keeps_the_stored_title(d1_worker):
    repo = _repo(d1_worker)
    repo.save("a.pdf", "h", 2, [], title=TITLE)
    repo.mark_failed("a.pdf", "h2", "garbled")
    assert _title(d1_worker) == TITLE


def test_the_default_is_the_keep_marker():
    import inspect

    assert inspect.signature(D1Repository.save).parameters["title"].default is KEEP_TITLE
    assert inspect.signature(MemoryRepository.save).parameters["title"].default is KEEP_TITLE


def test_the_migration_has_the_title_column():
    from tests.d1_fakes import MIGRATION

    assert "title          TEXT," in MIGRATION.read_text(encoding="utf-8")
