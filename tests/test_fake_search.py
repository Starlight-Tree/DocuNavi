"""テスト用の偽の検索（tests/fakes.py の FakeSearch）のテスト。アプリの検索ではない。"""

import asyncio

from app.search.types import Hit
from tests.fakes import FakeSearch


def search(query: str, top_k: int = 2) -> list[Hit]:
    return asyncio.run(FakeSearch()(query, top_k))


def test_a_keyword_in_the_query_returns_that_file():
    assert [hit.source for hit in search("ザーハードの大気について教えて")] == ["ザーハード1.pdf"]
    assert [hit.source for hit in search("ザーハリアン文明とは何ですか")] == ["ザーハード2.pdf"]
    assert [hit.source for hit in search("エネルギー源の正体は")] == ["ザーハード3.pdf"]


def test_a_query_without_a_keyword_finds_nothing():
    assert search("xyzqwerty") == []
    assert search("") == []


def test_top_k_limits_the_number_of_hits():
    both = "大気とエネルギーと文明"
    assert len(search(both, top_k=3)) == 3
    assert len(search(both, top_k=2)) == 2
    assert len(search(both, top_k=1)) == 1
