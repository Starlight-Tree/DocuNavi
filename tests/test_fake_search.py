"""テスト用の偽の検索（tests/fakes.py の FakeSearch）のテスト。アプリの検索ではない。"""

import asyncio

from tests.fakes import FakeSearch


def search(query: str, top_k: int = 2) -> list[tuple[str, str]]:
    return asyncio.run(FakeSearch()(query, top_k))


def test_a_keyword_in_the_query_returns_that_file():
    assert [name for name, _ in search("ザーハードの大気について教えて")] == ["ザーハード1.pdf"]
    assert [name for name, _ in search("ザーハリアン文明とは何ですか")] == ["ザーハード2.pdf"]
    assert [name for name, _ in search("エネルギー源の正体は")] == ["ザーハード3.pdf"]


def test_a_query_without_a_keyword_finds_nothing():
    assert search("xyzqwerty") == []
    assert search("") == []


def test_top_k_limits_the_number_of_hits():
    both = "大気とエネルギーと文明"
    assert len(search(both, top_k=3)) == 3
    assert len(search(both, top_k=2)) == 2
    assert len(search(both, top_k=1)) == 1
