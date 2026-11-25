"""PDF の有無（`/documents/{id}/chunks` の `original_pdf`）を、読むたびに R2 へ聞かず、取り込み側で確かめて DB に記録する（ADR 0017）。

確かめるのは、記録（`documents.original_verified_at`）を書く側（`python -m app.r2_store verify --record` と、取り込みでの消去）と、
読む側（`/chunks` は、記録があれば R2 に聞かない・無ければその場で確かめる）。実 DB・実 R2 は使わない（偽の Worker = 本物の SQLite、偽の S3）。
"""

from types import SimpleNamespace

import pytest

from app import pdf_originals, r2_store
from app.d1 import SyncD1
from app.ingestion.repository import D1Repository
from tests.test_document_file import PDF_A, PDF_B, doc_row, sha
from tests.test_documents import FakePool, chunk_row, use_pool  # noqa: F401
from tests.test_history_pg_backend import PG_TEST_URL, pg_url  # noqa: F401
from tests.test_r2_store import FakeS3, r2, s3  # noqa: F401


def _repo(worker) -> D1Repository:
    return D1Repository(SyncD1("https://worker.test", transport=worker.transport()))


def _verified(worker, source: str = "a.pdf"):
    return worker.rows("SELECT original_verified_at FROM documents WHERE source = ?", (source,))[0][
        "original_verified_at"
    ]


def _found(ok: bool, reason: str | None = None):
    return SimpleNamespace(ok=ok, reason=reason)


# --- 取り込み: 内容のハッシュが変わったら、記録を消す ------------------------------------------------------------


def test_ingestion_keeps_the_record_while_the_content_hash_is_the_same_and_clears_it_when_it_changes(d1_worker):
    repo = _repo(d1_worker)
    repo.save("a.pdf", "h1", 2, [])
    assert _verified(d1_worker) is None  # 新しい資料は、未確認
    d1_worker.run("UPDATE documents SET original_verified_at = 1000 WHERE source = 'a.pdf'")
    repo.save("a.pdf", "h1", 2, [])  # 同じ内容の再取り込み（題名・版の変更など）では、残す
    assert _verified(d1_worker) == 1000
    repo.save("a.pdf", "h2", 2, [])  # PDF が変わった: 古い PDF の確認なので、消す
    assert _verified(d1_worker) is None


def test_a_failed_reingestion_with_a_new_hash_also_clears_the_record(d1_worker):
    repo = _repo(d1_worker)
    repo.save("a.pdf", "h1", 2, [])
    d1_worker.run("UPDATE documents SET original_verified_at = 1000 WHERE source = 'a.pdf'")
    repo.mark_failed("a.pdf", "h1", "same file")  # ハッシュが同じなら、そのまま
    assert _verified(d1_worker) == 1000
    repo.mark_failed("a.pdf", "h2", "new file")
    assert _verified(d1_worker) is None


# --- 書く側: verify --record -----------------------------------------------------------------------------------


def _documents(worker) -> list[dict]:
    return worker.rows("SELECT id, source, content_hash FROM documents ORDER BY id")


def test_record_originals_sets_the_verified_ones_and_clears_the_rest(d1_worker):
    repo = _repo(d1_worker)
    for source, digest in (("a.pdf", "ha"), ("b.pdf", "hb"), ("c.pdf", "hc")):
        repo.save(source, digest, 1, [])
    d1_worker.run(
        "UPDATE documents SET original_verified_at = 5 WHERE source = 'b.pdf'"
    )  # 前に確かめた（いまは R2 に無い）
    a, b, c = _documents(d1_worker)
    checked = [(a, _found(True)), (b, _found(False, "missing")), (c, _found(False, "unavailable"))]
    recorded, cleared = r2_store.record_originals(
        SyncD1("https://worker.test", transport=d1_worker.transport()), checked
    )
    assert (recorded, cleared) == (1, 1)
    assert _verified(d1_worker, "a.pdf") is not None
    assert _verified(d1_worker, "b.pdf") is None
    assert _verified(d1_worker, "c.pdf") is None


def test_record_originals_does_not_touch_a_row_whose_hash_changed_in_the_meantime(d1_worker):
    repo = _repo(d1_worker)
    repo.save("a.pdf", "old", 1, [])
    (a,) = _documents(d1_worker)
    repo.save("a.pdf", "new", 1, [])  # 確かめている間に、取り込みで変わった
    recorded, _ = r2_store.record_originals(
        SyncD1("https://worker.test", transport=d1_worker.transport()), [(a, _found(True))]
    )
    assert recorded == 0 and _verified(d1_worker) is None


def test_verify_record_writes_only_with_the_flag(d1_worker, monkeypatch, r2, capsys):
    repo = _repo(d1_worker)
    repo.save("z.pdf", sha(PDF_A), 1, [])  # R2 の zahard/z.pdf（PDF_A）と一致する
    repo.save("nope.pdf", sha(PDF_A), 1, [])  # R2 に無い
    monkeypatch.setattr("app.history.database_url", lambda: "postgresql://unused")
    monkeypatch.setattr(r2_store, "_ready_documents", lambda: _documents(d1_worker))
    monkeypatch.setattr(
        "app.pg.sync_client", lambda d1_class, url=None: SyncD1("https://worker.test", transport=d1_worker.transport())
    )
    assert r2_store.main(["verify"]) == 1  # 1 件が一致しない。DB には書かない
    assert _verified(d1_worker, "z.pdf") is None
    assert r2_store.main(["verify", "--record"]) == 1  # 終了コードは、書いても同じ（一致しないものがある）
    assert _verified(d1_worker, "z.pdf") is not None and _verified(d1_worker, "nope.pdf") is None
    assert "記録 1 件" in capsys.readouterr().out


def test_record_is_only_for_verify():
    with pytest.raises(SystemExit):
        r2_store.main(["upload", "--record"])


# --- 読む側: /chunks ---------------------------------------------------------------------------------------------


def _chunks_row(**extra) -> dict:
    return {**doc_row(source="z.pdf", content_hash=sha(PDF_A)), **extra}


def test_chunks_trusts_a_recorded_verification_without_asking_r2(client, use_pool, r2):  # noqa: F811
    use_pool(FakePool(document=[_chunks_row(original_verified=True)], chunks=[chunk_row(0)]))
    r2.error = RuntimeError("R2 must not be asked when the verification is recorded")
    res = client.get("/documents/1/chunks")
    assert res.status_code == 200 and res.json()["original_pdf"] is True


@pytest.mark.parametrize("recorded", [None, False])
def test_chunks_checks_the_store_when_nothing_is_recorded(client, use_pool, r2, recorded):  # noqa: F811
    use_pool(FakePool(document=[_chunks_row(original_verified=recorded)], chunks=[chunk_row(0)]))
    assert client.get("/documents/1/chunks").json()["original_pdf"] is True
    use_pool(
        FakePool(
            document=[{**_chunks_row(original_verified=recorded), "content_hash": sha(PDF_B)}], chunks=[chunk_row(0)]
        )
    )
    pdf_originals.clear_original_cache()
    assert client.get("/documents/1/chunks").json()["original_pdf"] is False  # 名前は同じで中身が違う


def test_file_still_verifies_every_time_even_when_the_verification_is_recorded(client, use_pool, r2):  # noqa: F811
    use_pool(FakePool(document=[_chunks_row(original_verified=True, content_hash=sha(PDF_B))]))
    assert client.get("/documents/1/file").status_code == 409  # 記録があっても、内容が合わなければ返さない


# --- 実 Postgres（PG_TEST_URL があるときだけ）--------------------------------------------------------------------


@pytest.mark.skipif(not PG_TEST_URL, reason="PG_TEST_URL が無いので、実 Postgres の検査は飛ばします")
def test_record_and_reset_on_postgres_and_chunks_sql_reads_the_flag(pg_url):  # noqa: F811
    from app.pg import SyncPg

    with SyncPg(pg_url) as db:
        repo = D1Repository(db)
        repo.save("a.pdf", "h1", 1, [])
        repo.save("b.pdf", "hb", 1, [])
        docs = db.execute("SELECT id, source, content_hash FROM documents ORDER BY id")
        recorded, cleared = r2_store.record_originals(
            db, [(docs[0], _found(True)), (docs[1], _found(False, "missing"))]
        )
        assert (recorded, cleared) == (1, 0)
        flag = "SELECT d.original_verified_at IS NOT NULL AS original_verified FROM documents d WHERE d.source = ?"
        assert db.execute(flag, ("a.pdf",))[0]["original_verified"] is True
        assert db.execute(flag, ("b.pdf",))[0]["original_verified"] is False
        repo.save("a.pdf", "h1", 1, [])  # 同じ内容: 残る
        assert db.execute(flag, ("a.pdf",))[0]["original_verified"] is True
        repo.save("a.pdf", "h2", 1, [])  # 内容が変わった: 消える
        assert db.execute(flag, ("a.pdf",))[0]["original_verified"] is False
        recorded, cleared = r2_store.record_originals(
            db, [(docs[0], _found(True))]
        )  # 古いハッシュ（h1）の確認は、もう書かない
        assert (recorded, cleared) == (0, 0)
