"""資料群（documents.collection）のテスト: 取り込みでの決め方と保存、一覧 API での返し方。実 DB は使わない。"""

import pytest

from app.api import documents
from app.ingestion import __main__ as cli
from app.ingestion.repository import MemoryRepository, PostgresRepository
from app.ingestion.service import collection_name, ingest
from tests.pdf_fixtures import build_pdf, paragraphs
from tests.test_document_title import _FakeConn, _upsert_params
from tests.test_documents import FakePool, summary_row

BODY = [f"第{i}段落の本文はここに書かれています。" for i in range(1, 5)]


@pytest.fixture(autouse=True)
def _no_dotenv(monkeypatch):
    monkeypatch.setattr(cli, "load_dotenv", lambda *a, **k: False)
    monkeypatch.setenv("DATABASE_URL", "")


@pytest.fixture
def use_pool(monkeypatch):
    def install(pool):
        monkeypatch.setattr(documents, "get_pool", lambda: pool)
        return pool

    return install


def _pdf(path):
    path.write_bytes(build_pdf([paragraphs(*BODY)]))
    return path


# --- 資料群の名前の決め方 -------------------------------------------------------------------------------


def test_the_collection_defaults_to_the_input_directory_name(tmp_path):
    folder = tmp_path / "guides"
    folder.mkdir()
    assert collection_name(folder) == "guides"
    assert collection_name(_pdf(folder / "x.pdf")) == "guides"  # ファイル 1 つなら、置いてあるディレクトリの名前


@pytest.mark.parametrize("name", ["with space", "日本語", "a" * 41, "x.y"])
def test_a_directory_name_of_the_wrong_shape_is_not_a_collection(tmp_path, name):
    folder = tmp_path / name
    folder.mkdir()
    assert collection_name(folder) is None


def test_ingest_saves_the_directory_name_and_an_explicit_name_wins(tmp_path):
    folder = tmp_path / "cafe"
    folder.mkdir()
    pdf = _pdf(folder / "menu.pdf")
    repo = MemoryRepository()

    (report,) = ingest([pdf], folder, repo, apply=True)
    assert report.collection == "cafe" and repo.documents["menu.pdf"]["collection"] == "cafe"

    ingest([pdf], folder, repo, apply=True, collection="shops")
    assert repo.documents["menu.pdf"]["collection"] == "shops"


def test_dry_run_shows_the_collection_and_saves_nothing(tmp_path):
    folder = tmp_path / "zahard"
    folder.mkdir()
    repo = MemoryRepository()
    (report,) = ingest([_pdf(folder / "a.pdf")], folder, repo, apply=False)
    assert report.collection == "zahard"
    assert repo.documents == {}


def test_a_name_that_cannot_be_decided_keeps_the_stored_one(tmp_path):
    good = tmp_path / "guides"
    good.mkdir()
    pdf = _pdf(good / "a.pdf")
    repo = MemoryRepository()
    ingest([pdf], good, repo, apply=True)

    odd = tmp_path / "odd name"
    odd.mkdir()
    ingest([_pdf(odd / "a.pdf")], odd, repo, apply=True)  # 名前の形が合わず、決まらない（消さない）

    assert repo.documents["a.pdf"]["collection"] == "guides"


# --- Postgres への保存 ----------------------------------------------------------------------------------


def test_postgres_save_passes_the_collection_and_keeps_the_stored_one_when_none():
    conn = _FakeConn()
    PostgresRepository(conn, create_tables=False).save("a.pdf", "h", 2, [], collection="guides")
    sql, params = _upsert_params(conn)
    assert params[6] == "guides"
    assert "collection = COALESCE(EXCLUDED.collection, documents.collection)" in sql

    conn = _FakeConn()
    PostgresRepository(conn, create_tables=False).save("a.pdf", "h", 2, [])
    assert _upsert_params(conn)[1][6] is None  # None は「上書きしない」（SQL 側が保存済みを残す）


def test_a_failure_record_does_not_touch_the_collection():
    conn = _FakeConn()
    PostgresRepository(conn, create_tables=False).mark_failed("a.pdf", "h", "garbled")
    sql, params = _upsert_params(conn)
    assert params[6] is None
    assert "COALESCE(EXCLUDED.collection, documents.collection)" in sql


# --- コマンド --------------------------------------------------------------------------------------------


def test_cli_prints_the_collection_and_accepts_an_override(tmp_path, capsys):
    folder = tmp_path / "guides"
    folder.mkdir()
    _pdf(folder / "a.pdf")

    assert cli.main(["--input", str(folder)]) == 0
    assert "collection=guides" in capsys.readouterr().out
    assert cli.main(["--input", str(folder), "--collection", "manuals"]) == 0
    assert "collection=manuals" in capsys.readouterr().out


@pytest.mark.parametrize("name", ["", "has space", "日本語", "x" * 41])
def test_cli_rejects_a_collection_of_the_wrong_shape(tmp_path, capsys, name):
    _pdf(tmp_path / "a.pdf")
    assert cli.main(["--input", str(tmp_path), "--collection", name]) == 2
    assert "--collection" in capsys.readouterr().err


# --- 一覧 API ---------------------------------------------------------------------------------------------


def test_the_list_and_the_detail_return_the_collection(client, use_pool):
    rows = [
        {**summary_row(1), "collection": "guides"},
        {**summary_row(2, "no_collection.pdf"), "collection": None},
        {**summary_row(3, "no_column.pdf")},  # 行に collection が無い
        {**summary_row(4, "bad.pdf"), "collection": "bad name!"},  # 形が合わない値は返さない
        {**summary_row(5, "number.pdf"), "collection": 7},
    ]
    use_pool(FakePool(summaries=rows, document=[{**summary_row(1), "collection": "guides"}], chunks=[]))

    listed = [d["collection"] for d in client.get("/documents").json()["documents"]]
    assert listed == ["guides", None, None, None, None]
    assert client.get("/documents/1/chunks").json()["document"]["collection"] == "guides"
    assert "d.collection AS collection" in documents._LIST_SQL
    assert "d.collection AS collection" in documents._DOCUMENT_SQL
