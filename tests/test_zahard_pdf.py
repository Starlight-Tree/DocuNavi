"""ザーハードの PDF（storage/pdf/zahard/）が、元の .txt の資料そのものであることを確かめる。

ザーハードの資料は .txt から PDF に移った。移したことで文面・順序・設定が変わっていないこと、PDF の段落の分け方
（= 検索のチャンク）が evals/fixtures/zahard_pdf/layout.json のとおりであること、承認した PDF から動いていないことを固定する。
PDF の見た目（表示の崩れ）は、このテストでは確かめられない（資料一覧で実際に開いて目視する）。
"""

import hashlib
import json
import re

import pytest

from app.ingestion.chunker import chunk_pages
from app.ingestion.pdf_parser import extract_pages, extract_title
from tests.zahard_corpus import ROOT, ZAHARD_PDF_DIR, ZAHARD_TXT_DIR, zahard_units

LAYOUT = json.loads((ROOT / "evals" / "fixtures" / "zahard_pdf" / "layout.json").read_text(encoding="utf-8"))["files"]
NAMES = ("ザーハード1", "ザーハード2", "ザーハード3")

# 承認した PDF のバイト列。作り直したら、ここも更新する（DB の documents.content_hash と元 PDF の表示は、この内容に結び付く）
APPROVED_SHA256 = {
    "ザーハード1.pdf": "def11eb1ce30e58f726f902fa05b0613b73821f37fa8fe2eaed0fd16cebcc974",
    "ザーハード2.pdf": "9876a7aa728f90683a5f9bceceb7e1e88a1136a317fe5477aba5e8e68b1686de",
    "ザーハード3.pdf": "52f74a07e101fc22a17aa42786951be17c0cd1a98a8f3a13422dcbabae88f594",
}


def txt_sentences(name: str) -> list[str]:
    """元の .txt の文。区切りは「\\」（文字としての円記号）と改行（検索の分け方と同じ）。"""
    raw = (ZAHARD_TXT_DIR / f"{name}.txt").read_text(encoding="utf-8")
    return [part.strip() for part in re.split(r"[\\\n]+", raw) if part.strip()]


def test_the_layout_covers_the_three_files():
    assert tuple(LAYOUT) == NAMES


@pytest.mark.parametrize("name", NAMES)
def test_layout_groups_cover_every_sentence_once_in_order(name):
    order = [i for indexes in LAYOUT[name]["groups"].values() for i in indexes]
    assert order == list(range(len(txt_sentences(name))))


@pytest.mark.parametrize("name", NAMES)
def test_the_pdf_has_exactly_the_sentences_of_the_text_in_order(name):
    # 欠落・重複・順序の入れ替え・文面の違いが無い（正規化なしの完全一致）。検索が採点する単位（文）で比べる
    units = zahard_units()[f"{name}.pdf"]
    assert all(isinstance(unit, str) for unit in units)  # 表や見出しの単位が混ざっていない
    assert units == txt_sentences(name)


@pytest.mark.parametrize("name", NAMES)
def test_the_pdf_paragraphs_are_the_layout_groups(name):
    # 段落 = チャンク。話題のまとまり（layout.json）どおりに分かれ、見出しが付かず、1 つのチャンクが 2 ページにまたがらない
    sentences = txt_sentences(name)
    expected = [[sentences[i] for i in indexes] for indexes in LAYOUT[name]["groups"].values()]
    chunks = chunk_pages(extract_pages(ZAHARD_PDF_DIR / f"{name}.pdf"))
    assert [chunk.text.split("\n") for chunk in chunks] == expected
    assert all(chunk.kind == "text" and chunk.heading is None for chunk in chunks)
    assert all(chunk.page_end is None for chunk in chunks)


@pytest.mark.parametrize("name", NAMES)
def test_the_pdf_title_is_the_one_in_the_layout(name):
    assert extract_title(ZAHARD_PDF_DIR / f"{name}.pdf") == LAYOUT[name]["title"]


def test_the_directory_holds_only_the_three_approved_pdfs():
    assert sorted(path.name for path in ZAHARD_PDF_DIR.iterdir()) == sorted(APPROVED_SHA256)


@pytest.mark.parametrize("file_name", sorted(APPROVED_SHA256))
def test_the_pdf_bytes_are_the_approved_ones(file_name):
    assert hashlib.sha256((ZAHARD_PDF_DIR / file_name).read_bytes()).hexdigest() == APPROVED_SHA256[file_name]
