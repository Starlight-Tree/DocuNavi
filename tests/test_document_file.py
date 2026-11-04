"""元 PDF の配信（GET /documents/{id}/file）と、その判定（app/pdf_originals.py）のテスト。実 DB・実 API は使わない。

確かめるのは、名前とハッシュが DB と合う PDF だけを返すこと（同名で中身が違うファイルは返さない）、検証したバイト列をそのまま返すこと、
`storage/pdf/` の外へ出る参照（`../`・絶対パス・シンボリックリンク）を拒むこと、認証、エラーの固定メッセージ、
`/chunks` の `original_pdf`。
"""

import hashlib
import logging
import os
from pathlib import Path
from urllib.parse import quote

import pytest

from app import pdf_originals
from app.api import documents
from tests.test_documents import SECRET, FakePool, chunk_row, required, summary_row, use_pool  # noqa: F401

PDF_A = b"%PDF-1.4\n% original A\n"
PDF_B = b"%PDF-1.4\n% another version B\n"


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def doc_row(doc_id: int = 1, source: str = "tsubasa_dorm_rules.pdf", content_hash: str | None = None) -> dict:
    return {**summary_row(doc_id, source), "content_hash": content_hash}


@pytest.fixture
def store(tmp_path, monkeypatch) -> Path:
    """storage/pdf/ を一時ディレクトリに差し替える（cafe と guides の 2 つの資料群を置いてある）。"""
    root = tmp_path / "pdf"
    (root / "cafe").mkdir(parents=True)
    (root / "guides").mkdir()
    monkeypatch.setattr(pdf_originals, "PDF_DIR", root)
    return root


def put(store: Path, collection: str, name: str, data: bytes) -> Path:
    path = store / collection / name
    path.write_bytes(data)
    return path


# --- 返す PDF ---------------------------------------------------------------------------------------------


def test_serves_the_pdf_whose_name_and_hash_match(client, use_pool, store):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_A)
    pool = use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 200
    assert res.content == PDF_A
    assert res.headers["content-type"] == "application/pdf"
    assert res.headers["cache-control"] == "no-store"
    assert res.headers["x-content-type-options"] == "nosniff"
    assert res.headers["content-disposition"].startswith("inline;")
    assert [params["id"] for _, params in pool.calls] == [1]  # チャンクは読まない


def test_a_japanese_file_name_is_sent_as_an_encoded_filename_star(client, use_pool, store):
    name = "ザーハード設定資料集.pdf"
    put(store, "cafe", name, PDF_A)
    use_pool(FakePool(document=[doc_row(source=name, content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 200
    disposition = res.headers["content-disposition"]
    assert f"filename*=UTF-8''{quote(name, safe='')}" in disposition
    assert 'filename="document-1.pdf"' in disposition
    assert disposition.isascii()


@pytest.mark.parametrize("matching_collection", ["cafe", "guides"])
def test_with_the_same_name_in_two_collections_the_one_with_the_matching_hash_is_served(
    client, use_pool, store, matching_collection
):
    other = "guides" if matching_collection == "cafe" else "cafe"
    put(store, matching_collection, "same.pdf", PDF_A)
    put(store, other, "same.pdf", PDF_B)
    use_pool(FakePool(document=[doc_row(source="same.pdf", content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 200
    assert res.content == PDF_A


def test_identical_copies_in_two_collections_are_served(client, use_pool, store):
    put(store, "cafe", "same.pdf", PDF_A)
    put(store, "guides", "same.pdf", PDF_A)
    use_pool(FakePool(document=[doc_row(source="same.pdf", content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 200
    assert res.content == PDF_A


def test_the_hash_is_compared_without_regard_to_case(client, use_pool, store):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_A)
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A).upper())]))
    assert client.get("/documents/1/file").status_code == 200


def test_the_served_bytes_are_the_ones_that_were_hashed(store, monkeypatch):
    # 検証のあとで読み直さない: 実在する候補（guides/a.pdf）を開くのは 1 回だけ（cafe/ には無いので、開こうとして失敗する）
    put(store, "guides", "a.pdf", PDF_A)
    opened: list[str] = []
    real_open = os.open

    def counting_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(pdf_originals.os, "open", counting_open)
    result = pdf_originals.find_original("a.pdf", sha(PDF_A))
    assert result.ok and result.data == PDF_A
    assert hashlib.sha256(result.data).hexdigest() == sha(PDF_A)
    assert [Path(p).parent.name for p in opened].count("guides") == 1


# --- 返さない場合 -----------------------------------------------------------------------------------------


def test_a_file_with_the_same_name_but_other_content_is_not_served(client, use_pool, store, caplog):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_B)  # DB が知っているのは PDF_A
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    with caplog.at_level(logging.WARNING):
        res = client.get("/documents/1/file")
    assert res.status_code == 409
    assert res.json() == {"detail": documents._DETAIL_ORIGINAL_MISMATCH}
    assert PDF_B not in res.content


def test_without_a_stored_hash_nothing_is_served(client, use_pool, store):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_A)
    use_pool(FakePool(document=[doc_row(content_hash=None)]))
    assert client.get("/documents/1/file").status_code == 409


def test_a_missing_file_is_404_with_a_fixed_message(client, use_pool, store):
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 404
    assert res.json() == {"detail": documents._DETAIL_NO_ORIGINAL}


def test_a_missing_pdf_directory_is_404(client, use_pool, tmp_path, monkeypatch):
    monkeypatch.setattr(pdf_originals, "PDF_DIR", tmp_path / "no-such-dir")
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404


def test_unknown_document_is_404(client, use_pool, store):
    use_pool(FakePool(document=[]))
    res = client.get("/documents/99/file")
    assert res.status_code == 404
    assert res.json() == {"detail": documents._DETAIL_NOT_FOUND}


def test_a_file_over_the_size_limit_is_not_read(client, use_pool, store, monkeypatch):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_A)
    monkeypatch.setattr(pdf_originals, "MAX_ORIGINAL_BYTES", len(PDF_A) - 1)
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404


def test_a_directory_with_the_file_name_is_not_served(client, use_pool, store):
    (store / "guides" / "tsubasa_dorm_rules.pdf").mkdir()
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404


# --- storage/pdf/ の外を参照させない ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "source",
    [
        "../outside.pdf",
        "guides/../../outside.pdf",
        "sub/../a.pdf",
        "./a.pdf",
        "sub//a.pdf",
        "/sub/a.pdf",
        "sub/a.pdf/",
        "a/b/c/d.pdf",
        "..",
        ".",
        "",
        "sub\\a.pdf",
        "a\x00.pdf",
        "sub/a\n.pdf",
        "x" * 300,
    ],
)
def test_a_source_that_is_not_a_plain_file_name_is_never_looked_up(client, use_pool, store, source):
    outside = store.parent / "outside.pdf"
    outside.write_bytes(PDF_A)
    (store / "guides" / "sub").mkdir()
    (store / "guides" / "sub" / "a.pdf").write_bytes(PDF_A)
    use_pool(FakePool(document=[doc_row(source=source, content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404


def test_a_relative_path_under_a_collection_is_served_when_the_hash_matches(client, use_pool, store):
    # 合成資料の source は `<サブディレクトリ>/<ファイル>.pdf`（collection の直下の 1 段下）
    (store / "guides" / "sub").mkdir()
    (store / "guides" / "sub" / "a.pdf").write_bytes(PDF_A)
    use_pool(FakePool(document=[doc_row(source="sub/a.pdf", content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 200
    assert res.content == PDF_A


def test_a_relative_path_with_a_different_hash_is_a_409(client, use_pool, store):
    (store / "guides" / "sub").mkdir()
    (store / "guides" / "sub" / "a.pdf").write_bytes(PDF_A)
    use_pool(FakePool(document=[doc_row(source="sub/a.pdf", content_hash=sha(PDF_B))]))
    assert client.get("/documents/1/file").status_code == 409


def test_a_symlinked_subdirectory_is_not_followed_even_if_the_hash_matches(client, use_pool, store):
    elsewhere = store.parent / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "a.pdf").write_bytes(PDF_A)
    (store / "guides" / "sub").symlink_to(elsewhere, target_is_directory=True)
    use_pool(FakePool(document=[doc_row(source="sub/a.pdf", content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 404
    assert res.content != PDF_A


def test_an_absolute_path_as_source_is_never_read(client, use_pool, store):
    outside = store.parent / "outside.pdf"
    outside.write_bytes(PDF_A)
    use_pool(FakePool(document=[doc_row(source=str(outside), content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404


def test_a_symlinked_file_pointing_outside_is_not_served_even_if_its_hash_matches(client, use_pool, store):
    outside = store.parent / "outside.pdf"
    outside.write_bytes(PDF_A)
    (store / "guides" / "tsubasa_dorm_rules.pdf").symlink_to(outside)
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file")
    assert res.status_code == 404
    assert res.content != PDF_A


def test_a_symlinked_file_pointing_inside_is_not_served_either(client, use_pool, store):
    real = put(store, "cafe", "real.pdf", PDF_A)
    (store / "guides" / "tsubasa_dorm_rules.pdf").symlink_to(real)
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404


def test_a_symlinked_collection_directory_leading_outside_is_not_searched(client, use_pool, tmp_path, monkeypatch):
    root = tmp_path / "pdf"
    root.mkdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "tsubasa_dorm_rules.pdf").write_bytes(PDF_A)
    (root / "linked").symlink_to(elsewhere, target_is_directory=True)
    monkeypatch.setattr(pdf_originals, "PDF_DIR", root)
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404


def test_pdfs_directly_under_the_root_are_not_a_collection(client, use_pool, store):
    # 資料群は storage/pdf/<名前>/ の直下だけ。置き場の直下や、その下の下は見ない
    (store / "tsubasa_dorm_rules.pdf").write_bytes(PDF_A)
    (store / "guides" / "deeper").mkdir()
    (store / "guides" / "deeper" / "tsubasa_dorm_rules.pdf").write_bytes(PDF_A)
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    assert client.get("/documents/1/file").status_code == 404


# --- DB・認証・メソッド -----------------------------------------------------------------------------------


def test_without_a_database_it_is_a_fixed_503(client, use_pool, store):
    use_pool(None)
    res = client.get("/documents/1/file")
    assert res.status_code == 503
    assert res.json() == {"detail": documents._DETAIL_NOT_CONFIGURED}


def test_a_database_failure_is_a_fixed_503_without_the_exception_text(client, use_pool, store):
    use_pool(FakePool(error=ConnectionError(SECRET)))
    res = client.get("/documents/1/file")
    assert res.status_code == 503
    assert res.json() == {"detail": documents._DETAIL_UNAVAILABLE}
    assert SECRET not in res.text


@pytest.mark.parametrize("bad", ["0", "-1", "abc", "9223372036854775808", "1.5"])
def test_document_id_is_validated_before_the_database(client, use_pool, store, bad):
    pool = use_pool(FakePool(document=[doc_row()]))
    assert client.get(f"/documents/{bad}/file").status_code == 422
    assert pool.calls == []


def test_the_file_api_is_read_only(client):
    for method in ("post", "put", "patch", "delete"):
        assert getattr(client, method)("/documents/1/file").status_code == 405


def test_required_rejects_requests_without_a_valid_token_before_reading_anything(client, use_pool, store, required):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_A)
    pool = use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    for headers in ({}, {"Authorization": "Bearer bad-token"}, {"Authorization": "Basic good-token"}):
        res = client.get("/documents/1/file", headers=headers)
        assert res.status_code == 401
        assert res.headers["www-authenticate"] == "Bearer"
        assert PDF_A not in res.content
    assert pool.calls == []


def test_required_accepts_a_valid_token(client, use_pool, store, required):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_A)
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))]))
    res = client.get("/documents/1/file", headers={"Authorization": "Bearer good-token"})
    assert res.status_code == 200
    assert res.content == PDF_A


# --- /chunks の original_pdf ------------------------------------------------------------------------------


def test_chunks_tells_whether_the_original_pdf_can_be_served(client, use_pool, store):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_A)
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))], chunks=[chunk_row(0)]))
    body = client.get("/documents/1/chunks").json()
    assert body["original_pdf"] is True
    assert "content_hash" not in str(body)  # ハッシュは返さない


@pytest.mark.parametrize("stored_hash", [sha(PDF_B), None])
def test_chunks_says_false_when_the_file_does_not_match_or_cannot_be_checked(client, use_pool, store, stored_hash):
    put(store, "guides", "tsubasa_dorm_rules.pdf", PDF_A)
    use_pool(FakePool(document=[doc_row(content_hash=stored_hash)], chunks=[chunk_row(0)]))
    res = client.get("/documents/1/chunks")
    assert res.status_code == 200
    assert res.json()["original_pdf"] is False


def test_chunks_says_false_when_there_is_no_file(client, use_pool, store):
    use_pool(FakePool(document=[doc_row(content_hash=sha(PDF_A))], chunks=[chunk_row(0)]))
    assert client.get("/documents/1/chunks").json()["original_pdf"] is False


def test_the_list_does_not_select_the_hash(client):
    assert "content_hash" not in documents._LIST_SQL
    assert "content_hash" in documents._DOCUMENT_SQL


# --- アプリに置いてある PDF -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("collection", "name"),
    [
        ("cafe", "cafe_store_guide.pdf"),
        ("guides", "aoba_aw820_manual.pdf"),
        ("guides", "tsubasa_dorm_rules.pdf"),
    ],
)
def test_the_pdfs_shipped_in_storage_are_found_by_name_and_hash(collection, name):
    real = Path(__file__).resolve().parents[1] / "storage" / "pdf" / collection / name
    data = real.read_bytes()
    found = pdf_originals.find_original(name, sha(data))
    assert found.ok and found.data == data
    assert pdf_originals.find_original(name, sha(data + b"x")).reason == "mismatch"
