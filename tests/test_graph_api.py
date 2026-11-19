"""入寮規程の関係図の API（app/api/graph.py）のテスト。実 DB・実 API は使わない（接続プールは偽物）。

資料の行（DB）は偽物で、PDF は `storage/pdf/guides/tsubasa_dorm_rules.pdf` を実際に読む（名前と sha256 が DB の行と合うものだけを返す。
`tests/test_document_file.py` と同じ流儀）。確かめるのは、ノードと辺の数と形、影響のたどり方、対象外の資料・不正な入力・失敗の固定メッセージ、
AUTH_MODE=required での認証、DB に何も書かないこと。
"""

import hashlib
import logging
from collections import Counter
from pathlib import Path

import pytest

from app import pdf_originals
from app.api import graph
from tests.test_documents import SECRET, FakePool, required, summary_row  # noqa: F401

DORM_PDF = Path(pdf_originals.PDF_DIR) / "guides" / "tsubasa_dorm_rules.pdf"
DORM_HASH = hashlib.sha256(DORM_PDF.read_bytes()).hexdigest()
AUTH = {"Authorization": "Bearer good-token"}


def doc_row(doc_id: int = 1, source: str = "tsubasa_dorm_rules.pdf", content_hash: str = DORM_HASH) -> dict:
    return {**summary_row(doc_id, source), "title": "つばさ大学学生寮 入寮規程", "content_hash": content_hash}


@pytest.fixture
def use_pool(monkeypatch):
    def install(pool: FakePool | None) -> FakePool | None:
        monkeypatch.setattr(graph, "get_pool", lambda: pool)
        return pool

    return install


def impact(client, node: str, hops: int | None = None, doc: int = 1):
    params = {"node": node} | ({"hops": hops} if hops is not None else {})
    return client.get(f"/documents/{doc}/impact", params=params)


# --- グラフ -----------------------------------------------------------------------------------------------


def test_graph_has_chapters_articles_paragraphs_and_the_footnote(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    res = client.get("/documents/1/graph")
    assert res.status_code == 200
    body = res.json()
    assert body["document"]["id"] == 1 and body["document"]["source"] == "tsubasa_dorm_rules.pdf"
    assert body["title"] == "大学学生寮 入寮規程"
    assert Counter(n["type"] for n in body["nodes"]) == {"chapter": 5, "article": 29, "paragraph": 50, "footnote": 1}
    ids = [n["id"] for n in body["nodes"]]
    assert len(ids) == len(set(ids))
    nodes = {n["id"]: n for n in body["nodes"]}
    assert nodes["a13p2"]["parent"] == "a13" and nodes["a13"]["parent"] == "ch3" and nodes["ch3"]["parent"] is None
    assert nodes["a13p2"]["label"] == "第13条 第2項" and nodes["a13p2"]["text"]
    assert nodes["a13"]["text"] is None and nodes["a13"]["page"] >= 1
    assert nodes["fn1"]["type"] == "footnote" and nodes["fn1"]["text"]


def test_graph_edges_carry_type_direction_evidence_and_metadata(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    body = client.get("/documents/1/graph").json()
    assert Counter(e["type"] for e in body["edges"]) == {
        "same_chapter": 29,
        "uses_term": 9,
        "exception_of": 6,
        "references": 4,
    }
    node_ids = {n["id"] for n in body["nodes"]}
    for edge in body["edges"]:
        assert {edge["src"], edge["dst"]} <= node_ids  # 端点は必ずノードにある
        assert edge["evidence"] and edge["page"] >= 1
        assert edge["extracted_by"] == "rule" and edge["updated_at"]
    keys = {(e["type"], e["src"], e["dst"]) for e in body["edges"]}
    assert ("exception_of", "a4p2", "a4p1") in keys  # src が dst に依存する向き
    assert ("uses_term", "a15p1", "a13p1") in keys
    assert ("references", "a12p2", "fn1") in keys
    assert ("same_chapter", "a13", "ch3") in keys


def test_graph_is_computed_from_the_pdf_and_reads_only_the_document_row(client, use_pool):
    pool = use_pool(FakePool(document=[doc_row(7)]))
    res = client.get("/documents/7/graph")
    assert res.status_code == 200 and res.headers["cache-control"] == "no-store"
    assert [params["id"] for _, params in pool.calls] == [7]  # 読むのは文書の行 1 回だけ。チャンクも、書き込みも無い
    assert all(sql.lstrip().upper().startswith("SELECT") for sql, _ in pool.calls)


# --- 影響 -------------------------------------------------------------------------------------------------


def test_changing_article_13_paragraph_1_reaches_12_15_17_18_within_two_hops(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    body = impact(client, "a13p1").json()  # hops の既定は 2
    assert body["node"] == "a13p1" and body["hops"] == 2 and body["start"] == ["a13p1"]
    assert {(i["id"], i["hops"]) for i in body["impacted"]} == {
        ("a15p1", 1),
        ("a17p1", 1),
        ("a18p1", 1),
        ("fn1", 1),
        ("a12p2", 2),  # 脚注 1 を参照する項。脚注を経由して 2 つ先
    }
    assert body["articles"] == ["a12", "a15", "a17", "a18"]
    assert {(e["type"], e["src"], e["dst"]) for e in body["edges"]} >= {
        ("uses_term", "a15p1", "a13p1"),
        ("references", "a12p2", "fn1"),
    }
    assert all(e["type"] != "same_chapter" for e in body["edges"])  # 所属は依存ではない


def test_hops_limits_how_far_the_impact_goes(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    one = impact(client, "a13p1", 1).json()
    assert {i["id"] for i in one["impacted"]} == {"a15p1", "a17p1", "a18p1", "fn1"}
    assert one["articles"] == ["a15", "a17", "a18"]


def test_an_article_starts_from_all_its_paragraphs(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    body = impact(client, "a13").json()
    assert body["start"] == ["a13p1", "a13p2", "a13p3"]
    assert "a13p1" not in {i["id"] for i in body["impacted"]}  # 起点は影響先に数えない
    assert "a13" not in body["articles"]
    assert body["articles"] == ["a12", "a15", "a17", "a18"]


def test_a_node_nobody_depends_on_has_no_impact(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    body = impact(client, "a1p1").json()
    assert body["impacted"] == [] and body["articles"] == [] and body["edges"] == []


def test_a_footnote_can_be_the_source(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    body = impact(client, "fn1", 3).json()
    assert [(i["id"], i["hops"]) for i in body["impacted"]] == [("a12p2", 1)]


def test_unknown_node_is_404_and_a_chapter_is_422(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    res = impact(client, "a99p1")
    assert res.status_code == 404 and res.json() == {"detail": graph._DETAIL_UNKNOWN_NODE}
    res = impact(client, "ch3")
    assert res.status_code == 422 and res.json() == {"detail": graph._DETAIL_CHAPTER}


@pytest.mark.parametrize("node", ["", "../x", "a13p1;drop", "A13P1", "p1", "a" + "1" * 10, "a13 "])
def test_malformed_node_ids_are_rejected_before_the_document_is_read(client, use_pool, node):
    pool = use_pool(FakePool(document=[doc_row()]))
    assert impact(client, node).status_code == 422
    assert pool.calls == []


@pytest.mark.parametrize("hops", [0, -1, graph.MAX_HOPS + 1, "x"])
def test_hops_outside_the_range_is_422(client, use_pool, hops):
    use_pool(FakePool(document=[doc_row()]))
    assert impact(client, "a13p1", hops).status_code == 422


def test_node_is_required(client, use_pool):
    use_pool(FakePool(document=[doc_row()]))
    assert client.get("/documents/1/impact").status_code == 422


# --- 対象外・失敗 -----------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/documents/1/graph", "/documents/1/impact?node=a13p1"])
def test_other_documents_are_not_supported(client, use_pool, path):
    use_pool(FakePool(document=[doc_row(source="cafe_store_guide.pdf")]))
    res = client.get(path)
    assert res.status_code == 404 and res.json() == {"detail": graph._DETAIL_UNSUPPORTED}


@pytest.mark.parametrize("path", ["/documents/1/graph", "/documents/1/impact?node=a13p1"])
def test_missing_document_is_404(client, use_pool, path):
    use_pool(FakePool(document=[]))
    res = client.get(path)
    assert res.status_code == 404 and res.json() == {"detail": graph._DETAIL_NOT_FOUND}


@pytest.mark.parametrize("path", ["/documents/0/graph", "/documents/9223372036854775808/graph", "/documents/x/graph"])
def test_invalid_document_id_is_422(client, use_pool, path):
    pool = use_pool(FakePool(document=[doc_row()]))
    assert client.get(path).status_code == 422
    assert pool.calls == []


def test_db_not_configured_is_503(client, use_pool):
    use_pool(None)
    res = client.get("/documents/1/graph")
    assert res.status_code == 503 and res.json() == {"detail": graph._DETAIL_NOT_CONFIGURED}


def test_db_failure_is_a_fixed_503_without_the_exception_text(client, use_pool, caplog):
    use_pool(FakePool(error=RuntimeError(SECRET)))
    with caplog.at_level(logging.WARNING):
        res = client.get("/documents/1/graph")
    assert res.status_code == 503
    assert SECRET not in res.text and "AIza" not in res.text and SECRET not in caplog.text


def test_a_pdf_with_a_different_hash_is_409(client, use_pool):
    use_pool(FakePool(document=[doc_row(content_hash="0" * 64)]))
    res = client.get("/documents/1/graph")
    assert res.status_code == 409 and res.json() == {"detail": graph._DETAIL_ORIGINAL_MISMATCH}


def test_a_missing_pdf_is_404(client, use_pool, tmp_path, monkeypatch):
    (tmp_path / "guides").mkdir()
    monkeypatch.setattr(pdf_originals, "PDF_DIR", tmp_path)
    use_pool(FakePool(document=[doc_row()]))
    res = client.get("/documents/1/graph")
    assert res.status_code == 404 and res.json() == {"detail": graph._DETAIL_NO_ORIGINAL}


def test_a_parse_failure_is_a_fixed_500_without_the_exception_text(client, use_pool, monkeypatch, caplog):
    def broken(_data: bytes):
        raise RuntimeError(SECRET)

    monkeypatch.setattr(graph, "analyze_dorm_rules", broken)
    use_pool(FakePool(document=[doc_row()]))
    with caplog.at_level(logging.WARNING):
        res = client.get("/documents/1/graph")
    assert res.status_code == 500 and res.json() == {"detail": graph._DETAIL_UNREADABLE}
    assert SECRET not in res.text and SECRET not in caplog.text and "RuntimeError" in caplog.text


# --- 認証 -------------------------------------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/documents/1/graph", "/documents/1/impact?node=a13p1"])
def test_required_rejects_requests_without_a_valid_token(client, use_pool, required, path):
    pool = use_pool(FakePool(document=[doc_row()]))
    for headers in ({}, {"Authorization": "Bearer bad-token"}, {"Authorization": "Basic good-token"}):
        res = client.get(path, headers=headers)
        assert res.status_code == 401
        assert res.headers["www-authenticate"] == "Bearer"
        assert "good-token" not in res.text
    assert pool.calls == []  # 認証の前に DB を読まない


@pytest.mark.parametrize("path", ["/documents/1/graph", "/documents/1/impact?node=a13p1"])
def test_required_accepts_a_valid_token(client, use_pool, required, path):
    use_pool(FakePool(document=[doc_row()]))
    assert client.get(path, headers=AUTH).status_code == 200


def test_off_needs_no_token(client, use_pool, monkeypatch):
    monkeypatch.setenv("AUTH_MODE", "off")
    use_pool(FakePool(document=[doc_row()]))
    assert client.get("/documents/1/graph").status_code == 200
