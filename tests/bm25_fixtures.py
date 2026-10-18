"""BM25 の基準線（scripts/bm25_baseline.py）のテストだけが使うフィクスチャ。

アプリの検索のテストは使わない（アプリの検索の偽物は tests/fakes.py の FakeSearch）。
使うテストファイルが `from tests.bm25_fixtures import no_pdfs, txt_dir  # noqa: F401` で取り込む。
"""

import pytest

from scripts import bm25_baseline
from tests.zahard_corpus import ZAHARD_TXT_DIR


@pytest.fixture
def no_pdfs(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """BM25 の基準線の storage/pdf/ を、存在しないディレクトリに差し替える（PDF の資料群を外す。読み込みのキャッシュも、前後で捨てる）。"""
    monkeypatch.setattr(bm25_baseline, "PDF_DIR", tmp_path / "pdf")
    bm25_baseline._load_pdf_collections.cache_clear()
    yield
    bm25_baseline._load_pdf_collections.cache_clear()


@pytest.fixture
def txt_dir(monkeypatch: pytest.MonkeyPatch):
    """storage/chunk/ を、元の .txt（evals/fixtures/zahard_txt/）に差し替える。ザーハードは PDF に移ったので、
    「.txt の資料群」の経路（まだコードに残っている）を確かめるテストだけが使う。読み込みのキャッシュも、前後で捨てる。"""
    monkeypatch.setattr(bm25_baseline, "CHUNK_DIR", ZAHARD_TXT_DIR)
    bm25_baseline.load_chunks.cache_clear()
    yield
    bm25_baseline.load_chunks.cache_clear()
