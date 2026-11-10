"""取り込みが DB に流す SQL の本数（毎時の突き合わせが、変更のない文書で何本流すか）。

Prisma Postgres の無料枠は operations（SQL 1 本が 1）で数える。変更のない文書は、読むだけで書かない。読み取りも、文書の数に比例させない。
偽の Worker（本物の SQLite）の `sql_log` を数える。
"""

from app.ingestion.embedding import D1EmbeddingStore
from app.ingestion.repository import D1Repository
from app.ingestion.service import ingest
from tests.d1_fakes import FakeWorker
from tests.pdf_fixtures import build_pdf, paragraphs
from tests.test_d1 import sync_for

DOCS = 4
PAGES = 5


def _write_corpus(root, docs: int = DOCS, pages: int = PAGES) -> None:
    root.mkdir(parents=True, exist_ok=True)
    for d in range(docs):
        body = [paragraphs(*[f"文書{d} ページ{p} 段落{k} の本文です。" * 6 for k in range(3)]) for p in range(pages)]
        (root / f"doc{d}.pdf").write_bytes(build_pdf(body, title=f"題名{d}"))


def _ingest(worker: FakeWorker, root, *, apply: bool = True):
    repo = D1Repository(sync_for(worker))
    return ingest(sorted(root.glob("*.pdf")), root, repo, apply=apply, collection="guides")


def _count(worker: FakeWorker, action):
    before = len(worker.sql_log)
    result = action()
    return result, len(worker.sql_log) - before


def test_a_second_ingest_of_unchanged_pdfs_writes_nothing(tmp_path, d1_worker):
    root = tmp_path / "guides"
    _write_corpus(root)
    first, n_first = _count(d1_worker, lambda: _ingest(d1_worker, root))
    chunks = sum(r.chunks for r in first)
    second, n_second = _count(d1_worker, lambda: _ingest(d1_worker, root))
    assert [r.status for r in second] == ["unchanged"] * DOCS
    print(f"\n[SQL 本数] 文書 {DOCS} 件 / チャンク {chunks} 件: 初回 {n_first} 本、変更なしの再実行 {n_second} 本")
    writes = [s for s in d1_worker.sql_log[-n_second:] if not s.lstrip().upper().startswith("SELECT")]
    assert writes == []
    assert n_second <= 2  # 文書の数によらず、まとめて読む 2 本（文書の行・チャンクのハッシュ）


def test_a_changed_pdf_is_still_saved(tmp_path, d1_worker):
    root = tmp_path / "guides"
    _write_corpus(root)
    _ingest(d1_worker, root)
    (root / "doc0.pdf").write_bytes(build_pdf([paragraphs("新しい本文です。" * 6)], title="題名0"))
    reports = _ingest(d1_worker, root)
    assert [r.status for r in reports] == ["updated"] + ["unchanged"] * (DOCS - 1)
    row = d1_worker.rows("SELECT pages FROM documents WHERE source = 'doc0.pdf'")[0]
    assert row["pages"] == 1


def test_a_failed_record_is_restored_to_ready_by_the_next_ingest(tmp_path, d1_worker):
    root = tmp_path / "guides"
    _write_corpus(root, docs=1)
    _ingest(d1_worker, root)
    d1_worker.run("UPDATE documents SET status = 'failed', failure_reason = 'garbled'")
    _ingest(d1_worker, root)
    doc = d1_worker.rows("SELECT status, failure_reason FROM documents")[0]
    assert doc == {"status": "ready", "failure_reason": None}


def test_a_changed_title_or_collection_is_saved(tmp_path, d1_worker):
    root = tmp_path / "guides"
    _write_corpus(root, docs=1)
    _ingest(d1_worker, root)
    d1_worker.run("UPDATE documents SET title = '古い題名', collection = 'old'")
    _ingest(d1_worker, root)
    doc = d1_worker.rows("SELECT title, collection FROM documents")[0]
    assert doc == {"title": "題名0", "collection": "guides"}


def test_the_embedding_check_reads_the_chunks_once(tmp_path, d1_worker):
    root = tmp_path / "guides"
    _write_corpus(root, docs=2)
    _ingest(d1_worker, root)
    store = D1EmbeddingStore(sync_for(d1_worker))
    _, n = _count(d1_worker, lambda: (store.pending_embeddings(60), store.count_pending()))
    print(f"\n[SQL 本数] 埋め込みの対象の確認（件数つき）: {n} 本")
    assert n == 1
