"""ザーハードの PDF（storage/pdf/zahard/）が、元の .txt の資料そのものであることを確かめる。

ザーハードの資料は .txt から PDF に移った。移したことで文面・順序・設定が変わっていないこと、PDF の段落の分け方
（= 検索のチャンク）が evals/fixtures/zahard_pdf/layout.json のとおりであること、承認した PDF から動いていないことを固定する。
PDF の見た目（表示の崩れ）は、このテストでは確かめられない（資料一覧で実際に開いて目視する）。

2026-11-15 から、PDF の先頭に表紙と目次があり（ページラベルは 表紙・i・1）、各まとまりに見出し（layout.json のまとまりの名前）が付く。
本文の文（.txt の文）の文面・順序は変えていない。ここでは、見出しと目次を除いた文が .txt と一致することを確かめる。
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
    "ザーハード1.pdf": "6d8490c2ffd9763e51c01bee50457ad9855d1e0446b3483c695f53119e57b54e",
    "ザーハード2.pdf": "62baaabb3154c1d952f95ab160cb684f2969c529409e56c4c075df73ecea53fa",
    "ザーハード3.pdf": "a9ea0854d297dcbf5bc9c5edab41873692a4e557f31567e6cea17c9f4e18a770",
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
    # 欠落・重複・順序の入れ替え・文面の違いが無い（正規化なしの完全一致）。検索が採点する単位（文）で比べる。
    # 単位には、目次の表（1 つ）と、見出しの行（「目次」と、まとまりの名前）が混ざる。それらを除いた文が、.txt の文
    units = zahard_units()[f"{name}.pdf"]
    groups = list(LAYOUT[name]["groups"])
    (contents,) = [unit for unit in units if not isinstance(unit, str)]
    assert list(contents) == [f"{group} 1" for group in groups]  # 目次: まとまりの名前と、本文のページ（印字番号 1）
    headings = {"目次", *groups}
    assert [unit for unit in units if isinstance(unit, str) and unit not in headings] == txt_sentences(name)


@pytest.mark.parametrize("name", NAMES)
def test_the_pdf_paragraphs_are_the_layout_groups(name):
    # 段落 = チャンク。話題のまとまり（layout.json）どおりに分かれ、見出しはまとまりの名前で、1 つのチャンクが 2 ページにまたがらない。
    # 先頭に目次の表のチャンクが 1 つある（通し番号 2・ラベル i）。本文は通し番号 3・ラベル 1
    sentences = txt_sentences(name)
    groups = LAYOUT[name]["groups"]
    expected = [[sentences[i] for i in indexes] for indexes in groups.values()]
    chunks = chunk_pages(extract_pages(ZAHARD_PDF_DIR / f"{name}.pdf"))
    contents, body = chunks[0], chunks[1:]
    assert (contents.kind, contents.heading, contents.page, contents.page_label) == ("table", "目次", 2, "i")
    assert [chunk.text.split("\n") for chunk in body] == expected
    assert [chunk.heading for chunk in body] == list(groups)
    assert all(chunk.kind == "text" for chunk in body)
    assert all((chunk.page, chunk.page_label, chunk.page_end) == (3, "1", None) for chunk in body)


@pytest.mark.parametrize("name", NAMES)
def test_the_pdf_title_is_the_one_in_the_layout(name):
    assert extract_title(ZAHARD_PDF_DIR / f"{name}.pdf") == LAYOUT[name]["title"]


def test_the_directory_holds_only_the_three_approved_pdfs():
    assert sorted(path.name for path in ZAHARD_PDF_DIR.iterdir()) == sorted(APPROVED_SHA256)


@pytest.mark.parametrize("file_name", sorted(APPROVED_SHA256))
def test_the_pdf_bytes_are_the_approved_ones(file_name):
    assert hashlib.sha256((ZAHARD_PDF_DIR / file_name).read_bytes()).hexdigest() == APPROVED_SHA256[file_name]
