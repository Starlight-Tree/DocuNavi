import re

import pytest

from app.agents import tools
from app.agents.graph import _NOT_FOUND_MARK, _compose_mock_reply
from app.agents.tools import (
    TOP_K,
    TOOLS,
    bigrams,
    load_chunks,
    search_chunks,
    search_documents,
)
from tests.rag_eval_cases import ALL_CASES, RELATED_CASES, UNRELATED_CASES

# graph._compose_mock_reply がツール出力をパースするのと同じ正規表現。これで割れることが契約
SOURCE_HEADER = re.compile(r"(?m)^\[([^\]\n]+\.(?:txt|pdf))\]\n")

# 評価セットは 21 件と小さく、1 件の増減で 5% 動く。実測（hit@1 18/21, hit@2 19/21,
# evidence 19/21）より 1 件分だけ余裕を持たせた下限で、大きな退行だけを検知する。
MIN_HIT_AT_1 = 0.80
MIN_HIT_AT_2 = 0.85
MIN_EVIDENCE = 0.85


# 2-gram の照合では原理的に拾えない言い換え（「気温差」→「温度差」、「毒ガス」→「有害なガス」）。
# 再現率の測定には含めるが、出力形式の契約テストからは外す（ヒットしないので検査する中身がない）
KNOWN_MISSES = {"昼と夜の気温差について教えて", "毒ガスは含まれていますか"}
HIT_CASES = [case for case in RELATED_CASES if case.query not in KNOWN_MISSES]


def _search_text(query: str, top_k: int = TOP_K) -> tuple[list[str], str]:
    hits = search_chunks(query, top_k)
    return [name for name, _ in hits], "\n".join(text for _, text in hits)


def _rate(flags: list[bool]) -> float:
    return sum(flags) / len(flags)


# ---- 評価セットの健全性 ----


def test_eval_set_size_and_composition():
    assert 20 <= len(ALL_CASES) <= 30
    assert len(RELATED_CASES) >= 15
    assert len(UNRELATED_CASES) >= 5
    assert {case.kind for case in RELATED_CASES} >= {"direct", "keyword", "paraphrase", "colloquial"}


@pytest.mark.parametrize("case", RELATED_CASES, ids=lambda case: case.query)
def test_eval_evidence_exists_in_expected_files(case):
    # 資料を書き換えたときに、評価セットの根拠文字列が黙って無効にならないようにする
    chunks = load_chunks()
    assert case.expected_files
    for name in case.expected_files:
        assert name in chunks
    assert any(
        evidence in chunks[name] for evidence in case.evidence for name in case.expected_files
    ), f"根拠 {case.evidence} が {case.expected_files} に見つからない"


# ---- 検索精度（評価セットによる回帰テスト） ----


def test_related_queries_recall():
    hit1: list[bool] = []
    hit2: list[bool] = []
    evidence: list[bool] = []
    misses: list[str] = []
    for case in RELATED_CASES:
        names, text = _search_text(case.query)
        hit1.append(bool(names) and names[0] in case.expected_files)
        hit2.append(any(name in case.expected_files for name in names))
        evidence.append(any(snippet in text for snippet in case.evidence))
        if not (hit1[-1] and evidence[-1]):
            misses.append(f"{case.kind}: {case.query} -> {names}")

    detail = "\n".join(misses)
    assert _rate(hit1) >= MIN_HIT_AT_1, f"hit@1={_rate(hit1):.2f}\n{detail}"
    assert _rate(hit2) >= MIN_HIT_AT_2, f"hit@2={_rate(hit2):.2f}\n{detail}"
    assert _rate(evidence) >= MIN_EVIDENCE, f"evidence={_rate(evidence):.2f}\n{detail}"


@pytest.mark.parametrize("case", UNRELATED_CASES, ids=lambda case: case.query)
def test_unrelated_queries_find_nothing(case):
    assert search_chunks(case.query) == []
    assert _NOT_FOUND_MARK in search_documents.invoke({"query": case.query})


def test_returned_text_is_much_smaller_than_whole_files():
    # 全文を返していた旧実装では 1 回の検索で 1 ファイル ≈ 700 文字 × 2 を返していた。
    # 文単位に絞れていること（= 無関係な文を LLM に渡していないこと）を確認する
    total_chars = len(load_chunks()) and max(len(text) for text in load_chunks().values())
    for case in RELATED_CASES:
        _, text = _search_text(case.query)
        assert len(text) < total_chars * TOP_K, case.query


# ---- 出力形式の契約 ----


@pytest.mark.parametrize("case", HIT_CASES, ids=lambda case: case.query)
def test_search_chunks_contract(case):
    hits = search_chunks(case.query)
    chunks = load_chunks()
    assert 0 < len(hits) <= TOP_K
    names = [name for name, _ in hits]
    assert len(set(names)) == len(names), "同じファイルが重複している"
    for name, text in hits:
        assert name in chunks and name.endswith(".txt")
        assert text.strip()
        # 返すのは資料の文そのもの。言い換えや創作が混ざっていない
        for line in text.split("\n"):
            assert line in chunks[name]


@pytest.mark.parametrize("case", HIT_CASES, ids=lambda case: case.query)
def test_tool_output_is_parsable_by_mock_reply(case):
    output = search_documents.invoke({"query": case.query})
    assert _NOT_FOUND_MARK not in output
    parts = SOURCE_HEADER.split(output)
    # split の結果は ["", 名前, 本文, 名前, 本文, ...]
    assert parts[0] == ""
    names, bodies = parts[1::2], parts[2::2]
    assert names and len(names) == len(bodies)
    assert set(names) <= set(load_chunks())
    assert all(body.strip() for body in bodies)


def test_tool_output_when_not_found():
    output = search_documents.invoke({"query": "今日の天気"})
    assert _NOT_FOUND_MARK in output
    assert SOURCE_HEADER.search(output) is None


def test_tool_is_registered():
    assert TOOLS == [search_documents]
    assert search_documents.name == "search_documents"


def test_mock_reply_uses_search_output():
    found = _compose_mock_reply("遺跡の構造は？", search_documents.invoke({"query": "遺跡の構造は？"}))
    assert "出典: ザーハード2.txt" in found
    assert "多層的な構造" in found

    missing = _compose_mock_reply("今日の天気は？", search_documents.invoke({"query": "今日の天気は？"}))
    assert "見つかりませんでした" in missing
    assert "出典" not in missing


# ---- 公開 API と境界値 ----


def test_bigrams_contract_is_unchanged():
    # graph.py（モック返答の文抜粋）が依存している仕様
    assert bigrams("ab c") == {"ab", "bc"}
    assert bigrams("あ") == {"あ"}
    assert bigrams("") == set()
    assert bigrams("惑 星") == {"惑星"}


def test_load_chunks_is_cached_and_complete():
    first = load_chunks()
    assert load_chunks() is first
    assert set(first) == {"ザーハード1.txt", "ザーハード2.txt", "ザーハード3.txt"}


@pytest.mark.parametrize("query", ["", "   ", "？！", "は", "あのですね", "x"])
def test_degenerate_queries_return_empty(query):
    assert search_chunks(query) == []


def test_top_k_limits_number_of_files():
    # 「エネルギー源」は 3 ファイルすべてに出てくる
    assert len(search_chunks("エネルギー源について教えて", top_k=1)) == 1
    assert len(search_chunks("エネルギー源について教えて", top_k=2)) == 2
    assert search_chunks("エネルギー源について教えて", top_k=0) == []


def test_search_chunks_with_isolated_corpus(monkeypatch):
    # 実資料に依存せず、文の分割（「\」と改行）・前後文の付与・出現順を確認する。
    # load_chunks 本体の lru_cache には触れず、参照だけを差し替える
    corpus = {
        "a.txt": "冒頭の説明です。\\赤い林檎は甘い。\\続きの文です。\\青い海原は広い。\n緑の山脈は高い。\\末尾です。",
        "b.txt": "桜の花が咲く。\\春の風が吹く。",
    }
    monkeypatch.setattr(tools, "load_chunks", lambda: corpus)

    hits = search_chunks("林檎")
    assert hits == [("a.txt", "冒頭の説明です。\n赤い林檎は甘い。\n続きの文です。")]

    # 前後の文も付くが、元の並び順は保たれる
    hits = search_chunks("山脈")
    assert hits == [("a.txt", "青い海原は広い。\n緑の山脈は高い。\n末尾です。")]

    assert search_chunks("宇宙船") == []
