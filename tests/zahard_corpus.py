"""ザーハードの資料を、テストが読むための道具。

ザーハードの資料は、storage/chunk/ の .txt から storage/pdf/zahard/ の PDF に移った（元の .txt は evals/fixtures/zahard_txt/）。
アプリの検索と同じ経路（app/ingestion/corpus.py の load_pdf_corpus）で読む。
"""

from functools import lru_cache
from pathlib import Path
from typing import Any

from app.ingestion.corpus import load_pdf_corpus

ROOT = Path(__file__).resolve().parents[1]
ZAHARD_PDF_DIR = ROOT / "storage" / "pdf" / "zahard"
ZAHARD_TXT_DIR = ROOT / "evals" / "fixtures" / "zahard_txt"
ZAHARD_FILES = ("ザーハード1.pdf", "ザーハード2.pdf", "ザーハード3.pdf")


@lru_cache(maxsize=1)
def zahard_units() -> dict[str, list[Any]]:
    """ファイル名 -> 検索が採点する単位の列（アプリが読むのと同じ）。変更しないこと。"""
    corpus, _, failures = load_pdf_corpus(ZAHARD_PDF_DIR)
    assert not failures, failures
    return corpus


def zahard_texts() -> dict[str, str]:
    """ファイル名 -> 本文（単位を改行でつないだもの）。.txt の時代の load_chunks() に代わる、本文の参照用。"""
    return {
        name: "\n".join(unit if isinstance(unit, str) else "\n".join(unit) for unit in units)
        for name, units in zahard_units().items()
    }
