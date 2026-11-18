"""BM25 の基準線（scripts/bm25_baseline.py）のテストだけが使うフィクスチャ。

アプリの検索のテストは使わない（アプリの検索の偽物は tests/fakes.py の FakeSearch）。
使うテストファイルが `from tests.bm25_fixtures import approved_pdfs, no_pdfs, txt_dir  # noqa: F401` で取り込む。
"""

import shutil
from pathlib import Path

import pytest

from scripts import bm25_baseline
from tests.zahard_corpus import ZAHARD_TXT_DIR

# アプリの資料として Git に入れている PDF の資料群（storage/pdf/ の直下）。BM25 の基準線と評価の数字は、この 3 つを前提にしている
APPROVED_PDF_COLLECTIONS = ("cafe", "guides", "zahard")
APP_PDF_DIR = Path(__file__).resolve().parents[1] / "storage" / "pdf"


@pytest.fixture
def approved_pdfs(monkeypatch: pytest.MonkeyPatch, tmp_path):
    """BM25 の基準線の storage/pdf/ を、承認済みの 3 資料群（APPROVED_PDF_COLLECTIONS）だけを写した一時ディレクトリに差し替える。

    開発機の storage/pdf/ には、Git の管理外の PDF（テスト用の資料群。例: synthetic-test/）が置かれることがある。
    それが混ざると、評価の基準線（無関係な質問は何も返さない・資料群ごとの結果）が動くので、基準線を測るテストは
    これを使って、置かれた PDF に左右されないようにする。読み込みのキャッシュも、前後で捨てる。
    """
    root = tmp_path / "approved_pdf"
    for name in APPROVED_PDF_COLLECTIONS:
        shutil.copytree(APP_PDF_DIR / name, root / name)
    monkeypatch.setattr(bm25_baseline, "PDF_DIR", root)
    bm25_baseline._load_pdf_collections.cache_clear()
    yield
    bm25_baseline._load_pdf_collections.cache_clear()


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
