"""検索の重ね（app/search/pipeline.py: ハイブリッド・リランカー・GraphRAG）のテスト。実 API・実 DB は使わない。

ベクトル検索（`search_vector.search`）と資料の写しの読み取り（`_fetch_chunks`）を偽物に差し替える。
conftest は既存のテストのために 3 つのフラグを off にしている。ここでは、確かめたいものだけを on にする。
"""

import asyncio
import logging
from pathlib import Path
from typing import Any

import pytest
import yaml

from app.agents.tools import _sources
from app.scope import Scope
from app.search import pipeline as search_pipeline
from app.search import vector as search_vector
from app.search.graph import build_edges
from app.search.keyword import KeywordIndex, identifiers, tokenize
from app.search.pipeline import Chunk
from app.search.types import Hit, VectorSearchError

FLAGS = ("SEARCH_HYBRID", "SEARCH_RERANK", "SEARCH_GRAPH")


def chunk(source: str, text: str, page: int = 1, collection: str | None = "zahard") -> Chunk:
    return Chunk(source, text, page, None, collection)


def hit(c: Chunk, sim: float) -> Hit:
    return Hit(c.source, c.text, c.page, c.page_end, sim, c.collection)


class Env:
    """偽のベクトル検索と偽の資料の写し。呼び出しを記録する。"""

    def __init__(self, chunks: list[Chunk], vector: list[Hit]) -> None:
        self.chunks = chunks
        self.vector = vector
        self.vector_calls: list[tuple[str, int, str | None]] = []
        self.fetches = 0

    async def search(self, query: str, top_k: int, collection: str | None = None, *, scope: Scope) -> list[Hit]:
        self.vector_calls.append((query, top_k, collection))
        return self.vector[:top_k]

    async def fetch(self, scope: Scope, collection: str | None) -> list[Chunk]:
        self.fetches += 1
        return [c for c in self.chunks if scope.allows(c.collection) and collection in (None, c.collection)]


def install(monkeypatch: pytest.MonkeyPatch, chunks: list[Chunk], vector: list[Hit], **flags: str) -> Env:
    env = Env(chunks, vector)
    search_pipeline.clear_cache()
    monkeypatch.setattr(search_vector, "search", env.search)
    monkeypatch.setattr(search_pipeline, "_fetch_chunks", env.fetch)
    for name in FLAGS:
        monkeypatch.setenv(name, flags.get(name, "off"))
    return env


def run(query: str, top_k: int = 2, collection: str | None = None, scope: Scope = Scope.ALL) -> list[Hit]:
    return asyncio.run(search_pipeline.search(query, top_k, collection, scope=scope))


A = chunk("spec.pdf", "AP-340S の消費電力は 42W、質量は 6.9kg です。", page=1)
B = chunk("spec.pdf", "AP-430 の加湿量は最大 400mL/h です。", page=2)
C = chunk("spec.pdf", "お手入れ\nフィルターは月に一度、水洗いしてください。", page=3)


# ---- フラグ ----


def test_hybrid_and_rerank_default_to_on_and_graph_to_off(monkeypatch):
    for name in FLAGS:
        monkeypatch.delenv(name, raising=False)
    flags = search_pipeline.Flags.from_env()
    assert (flags.hybrid, flags.rerank, flags.graph) == (True, True, False)
    assert (flags.candidates, flags.graph_extra) == (20, 1)


@pytest.mark.parametrize(
    "value,expected", [("on", True), ("1", True), ("TRUE", True), ("off", False), ("0", False), ("No", False)]
)
def test_flag_values(monkeypatch, value, expected):
    monkeypatch.setenv("SEARCH_HYBRID", value)
    assert search_pipeline.Flags.from_env().hybrid is expected


@pytest.mark.parametrize(
    "name,value",
    [
        ("SEARCH_RERANK", "maybe"),
        ("SEARCH_CANDIDATES", "abc"),
        ("SEARCH_CANDIDATES", "-5"),
        ("SEARCH_GRAPH_MAX_EXTRA", "7"),
    ],
)
def test_an_invalid_value_is_an_error_and_never_a_silent_default(monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    with pytest.raises(VectorSearchError) as err:
        search_pipeline.Flags.from_env()
    assert name in str(err.value)
    assert value not in str(err.value)


# ---- 既存のベクトル検索との比較 ----


def test_with_every_flag_off_the_result_is_the_vector_search_itself(monkeypatch):
    vector = [hit(B, 0.81), hit(A, 0.80)]
    env = install(monkeypatch, [A, B, C], vector)
    result = run("加湿量", top_k=2)
    assert result == vector  # 同じ並び・同じ値・origin も None のまま
    assert env.vector_calls == [("加湿量", 2, None)]  # 候補を増やさず、top_k のまま
    assert env.fetches == 0  # 資料の写しは読まない


def test_vector_only_with_rerank_off_keeps_the_vector_order(monkeypatch):
    vector = [hit(B, 0.81), hit(A, 0.80), hit(C, 0.70)]
    install(monkeypatch, [A, B, C], vector, SEARCH_HYBRID="on")
    result = run("消費電力は", top_k=3)
    assert [h.origin for h in result][0] in {"vector", "vector+keyword"}
    assert {h.text for h in result} == {A.text, B.text, C.text}


def test_every_returned_chunk_is_one_the_vector_or_keyword_stage_found(monkeypatch):
    vector = [hit(B, 0.81), hit(C, 0.70)]
    install(monkeypatch, [A, B, C], vector, SEARCH_HYBRID="on", SEARCH_RERANK="on", SEARCH_GRAPH="on")
    for h in run("AP-340S の消費電力は", top_k=3):
        assert h.text in {A.text, B.text, C.text}
        assert h.origin in {"vector", "keyword", "vector+keyword", "graph"}


# ---- ハイブリッド ----


def test_an_exact_identifier_rescues_a_chunk_the_vector_search_missed(monkeypatch):
    # AP-340S の消費電力の質問。ベクトル側は別の機種と手入れを上位に返し、答えのチャンク A はしきい値の下
    vector = [hit(B, 0.80), hit(C, 0.70)]
    install(monkeypatch, [A, B, C], vector, SEARCH_HYBRID="on")
    result = run("AP-340S の消費電力は何Wですか", top_k=2)
    rescued = [h for h in result if h.text == A.text]
    assert len(rescued) == 1
    assert rescued[0].origin == "keyword"
    assert rescued[0].similarity is None


def test_with_the_reranker_the_rescued_chunk_comes_first(monkeypatch):
    vector = [hit(B, 0.80), hit(C, 0.70)]
    install(monkeypatch, [A, B, C], vector, SEARCH_HYBRID="on", SEARCH_RERANK="on")
    result = run("AP-340S の消費電力は何Wですか", top_k=2)
    assert result[0].text == A.text and result[0].origin == "keyword"


def test_a_keyword_only_chunk_without_an_identifier_is_not_admitted(monkeypatch):
    # 質問に型番が無い。語が重なるだけのチャンクを、ベクトルの下限を越えていないのに返してはいけない
    vector = [hit(C, 0.70)]
    install(monkeypatch, [A, B, C], vector, SEARCH_HYBRID="on")
    result = run("消費電力と質量を教えてください", top_k=3)
    assert [h.text for h in result] == [C.text]


def test_the_keyword_rank_can_reorder_the_vector_candidates(monkeypatch):
    # ベクトルは C を 1 位、A を 2 位にするが、質問の語は A にだけある
    vector = [hit(C, 0.81), hit(A, 0.80)]
    install(monkeypatch, [A, B, C], vector, SEARCH_HYBRID="on")
    result = run("AP-340S の質量", top_k=2)
    assert [h.text for h in result] == [A.text, C.text]
    assert result[0].origin == "vector+keyword"
    assert result[0].similarity == 0.80  # 類似度はベクトルの値のまま


def test_hybrid_off_never_reads_the_keyword_side_ranking(monkeypatch):
    vector = [hit(B, 0.81), hit(A, 0.80)]
    install(monkeypatch, [A, B, C], vector, SEARCH_RERANK="on")
    result = run("AP-340S の質量", top_k=2)
    assert {h.origin for h in result} == {"vector"}


# ---- リランカー ----


def test_the_reranker_moves_the_chunk_that_contains_the_identifier_up(monkeypatch):
    vector = [hit(B, 0.81), hit(A, 0.80)]
    install(monkeypatch, [A, B, C], vector, SEARCH_RERANK="on")
    result = run("AP-340S の質量", top_k=2)
    assert [h.text for h in result] == [A.text, B.text]


def test_rerank_off_keeps_the_order_the_earlier_stages_gave(monkeypatch):
    vector = [hit(B, 0.81), hit(A, 0.80)]
    install(monkeypatch, [A, B, C], vector)
    assert [h.text for h in run("AP-340S の質量", top_k=2)] == [B.text, A.text]


# ---- GraphRAG ----


R1 = chunk("rules.pdf", "第1条（目的）\nこの規程は寮の運営を定める。\n第2条（入寮）\n入寮は第5条の定めによる。", page=1)
R2 = chunk("rules.pdf", "第3条（退寮）\n退寮の手続きは次のとおり。", page=2)
R3 = chunk("rules.pdf", "第5条（審査）\n入寮の審査は、書類により行う。", page=3)
OTHER = chunk("other.pdf", "第5条（別の資料）\n関係ない。", page=1)


def test_build_edges_links_a_reference_to_the_chunk_that_defines_the_article_in_the_same_document():
    edges = build_edges(["rules.pdf", "rules.pdf", "rules.pdf", "other.pdf"], [R1.text, R2.text, R3.text, OTHER.text])
    assert edges == {0: [2]}  # 他の資料の第5条には張らない。自分で定義する条への参照にも張らない


def test_a_referenced_chunk_is_added_as_graph_context_after_the_top_k(monkeypatch):
    vector = [hit(R1, 0.80), hit(R2, 0.75)]
    install(monkeypatch, [R1, R2, R3, OTHER], vector, SEARCH_GRAPH="on")
    result = run("入寮の手続きは", top_k=2)
    assert [h.text for h in result] == [R1.text, R2.text, R3.text]
    assert result[2].origin == "graph"
    assert result[2].similarity is None
    assert [s["weak"] for s in _sources(result)] == [False, False, True]  # 出典カードには出さない


def test_graph_max_extra_zero_adds_nothing(monkeypatch):
    install(monkeypatch, [R1, R2, R3], [hit(R1, 0.80)], SEARCH_GRAPH="on")
    monkeypatch.setenv("SEARCH_GRAPH_MAX_EXTRA", "0")
    assert [h.text for h in run("入寮", top_k=2)] == [R1.text]


def test_a_chunk_already_returned_is_not_added_again(monkeypatch):
    install(monkeypatch, [R1, R2, R3], [hit(R1, 0.80), hit(R3, 0.78)], SEARCH_GRAPH="on")
    assert [h.text for h in run("入寮", top_k=2)] == [R1.text, R3.text]


def test_graph_context_stays_inside_the_access_scope(monkeypatch):
    hidden = chunk("rules.pdf", R3.text, page=3, collection="secret")
    install(monkeypatch, [R1, R2, hidden], [hit(R1, 0.80)], SEARCH_GRAPH="on")
    result = run("入寮", top_k=2, scope=Scope.only(["zahard"]))
    assert [h.text for h in result] == [R1.text]


# ---- 範囲・境界 ----


def test_an_empty_scope_a_blank_query_and_a_zero_top_k_do_not_search(monkeypatch):
    env = install(monkeypatch, [A], [hit(A, 0.9)], SEARCH_HYBRID="on")
    assert run("AP-340S", scope=Scope.NONE) == []
    assert run("   ") == []
    assert run("AP-340S", top_k=0) == []
    assert env.vector_calls == []


def test_a_missing_scope_is_a_type_error(monkeypatch):
    install(monkeypatch, [A], [hit(A, 0.9)], SEARCH_HYBRID="on")
    with pytest.raises(TypeError):
        asyncio.run(search_pipeline.search("AP-340S", 2))  # type: ignore[call-arg]


def test_candidates_are_never_fewer_than_top_k(monkeypatch):
    env = install(monkeypatch, [A, B, C], [hit(A, 0.9)], SEARCH_HYBRID="on")
    monkeypatch.setenv("SEARCH_CANDIDATES", "1")
    run("AP-340S", top_k=5)
    assert env.vector_calls[0][1] == 5


def test_the_corpus_is_cached_per_scope_and_collection(monkeypatch):
    env = install(monkeypatch, [A, B], [hit(A, 0.9)], SEARCH_HYBRID="on")
    run("AP-340S")
    run("AP-430")
    assert env.fetches == 1
    run("AP-340S", scope=Scope.only(["zahard"]))
    assert env.fetches == 2


def test_a_failure_of_the_keyword_side_is_an_error_and_not_a_vector_only_answer(monkeypatch):
    env = install(monkeypatch, [A], [hit(A, 0.9)], SEARCH_HYBRID="on")

    async def broken(scope: Scope, collection: str | None) -> list[Chunk]:
        raise VectorSearchError("DB の接続プールが開いていません")

    monkeypatch.setattr(search_pipeline, "_fetch_chunks", broken)
    with pytest.raises(VectorSearchError):
        run("AP-340S")
    assert env.vector_calls  # ベクトル側は呼ばれたが、その結果だけを返すことはしない


def test_the_real_corpus_query_filters_by_scope_and_reads_only_ready_embedded_chunks(monkeypatch):
    calls: list[tuple[str, dict[str, Any]]] = []

    class Cursor:
        async def fetchall(self) -> list[dict[str, Any]]:
            return [
                {
                    "source": "a.pdf",
                    "collection": "zahard",
                    "heading": "見出し",
                    "content": "本文",
                    "page": 1,
                    "page_end": None,
                },
                {
                    "source": "b.pdf",
                    "collection": "secret",
                    "heading": None,
                    "content": "漏れてはいけない",
                    "page": 1,
                    "page_end": None,
                },
            ]

    class Conn:
        async def execute(self, sql: str, params: dict[str, Any]) -> Cursor:
            calls.append((sql, params))
            return Cursor()

    class Pool:
        def connection(self):
            conn = Conn()

            class Ctx:
                async def __aenter__(self_inner):
                    return conn

                async def __aexit__(self_inner, *exc):
                    return False

            return Ctx()

    monkeypatch.setattr(search_vector, "get_pool", lambda: Pool())
    chunks = asyncio.run(search_pipeline._fetch_chunks(Scope.only(["zahard"]), None))
    assert [c.text for c in chunks] == ["見出し\n本文"]  # SQL が誤っても、範囲外のグループは持たない
    sql, params = calls[0]
    assert (
        "d.status = 'ready'" in sql and "c.embedding IS NOT NULL" in sql and "ORDER BY d.source, c.chunk_index" in sql
    )
    assert params == {"collection": None, "allowed": ["zahard"]}


# ---- ログ ----


def test_one_log_line_has_the_method_scores_and_origin_but_not_the_query(monkeypatch, caplog):
    install(
        monkeypatch, [A, B, C], [hit(B, 0.81), hit(C, 0.70)], SEARCH_HYBRID="on", SEARCH_RERANK="on", SEARCH_GRAPH="on"
    )
    query = "AP-340S の消費電力は何Wですか"
    with caplog.at_level(logging.INFO, logger="app.search_pipeline"):
        run(query, top_k=2)
    lines = [r.getMessage() for r in caplog.records if r.name == "app.search_pipeline"]
    assert len(lines) == 1
    line = lines[0]
    assert "flags=hybrid=on,rerank=on,graph=on" in line
    assert (
        "keyword " in line
        and "vec=" in line
        and "bm25=" in line
        and "rrf=" in line
        and "lex=" in line
        and "score=" in line
    )
    assert "spec.pdf p1" in line
    assert query not in line and "消費電力" not in line  # 質問の本文は載せない
    assert "42W" not in line  # チャンクの本文も載せない


def test_the_all_off_path_also_logs_the_method_and_the_vector_score(monkeypatch, caplog):
    install(monkeypatch, [A], [hit(A, 0.9)])
    with caplog.at_level(logging.INFO, logger="app.search_pipeline"):
        run("AP-340S")
    line = [r.getMessage() for r in caplog.records if r.name == "app.search_pipeline"][0]
    assert "flags=hybrid=off,rerank=off,graph=off" in line and "vector vec=0.900" in line


# ---- キーワード（BM25）の部品 ----


def test_tokenize_keeps_model_numbers_whole_and_in_parts_and_cuts_japanese_into_pairs():
    assert tokenize("AP-340S") == ["ap-340s", "ap", "340s"]
    assert tokenize("消費電力") == ["消費", "費電", "電力"]
    assert tokenize("Ｖ２．１") == ["v2.1", "v2", "1"]  # 全角は NFKC でそろえる


def test_identifiers_pick_model_like_strings_and_skip_plain_numbers_and_words():
    assert identifiers("AP-340S の消費電力は 42W ですか 2026 年 v2.1.0") == ["ap-340s", "42w", "v2.1.0"]
    assert identifiers("10 時に営業 hello") == []


def test_bm25_ranks_the_chunk_with_the_rare_term_first():
    index = KeywordIndex([A.text, B.text, C.text])
    assert index.ranked("AP-430 の加湿量", 3)[0][0] == 1
    assert index.ranked("まったく関係ない語句 qqq", 3) == []


# ---- 実際の資料での比較（評価用の完全一致コーパス。実 API・DB なし） ----

EXACT = Path(__file__).resolve().parents[1] / "evals" / "fixtures" / "exact"


def _exact_chunks() -> list[Chunk]:
    from app.ingestion.chunker import chunk_pages
    from app.ingestion.pdf_parser import extract_pages

    chunks: list[Chunk] = []
    for pdf in sorted(EXACT.glob("*.pdf")):
        for c in chunk_pages(extract_pages(pdf)):
            heading = getattr(c, "heading", None)
            body = c.content if hasattr(c, "content") else c.text
            text = f"{heading}\n{body}" if heading else body
            chunks.append(Chunk(pdf.name, text, getattr(c, "page", None), None, "exact"))
    return chunks


def _recall_at(k: int, rank) -> tuple[int, int]:
    questions = [
        q for q in yaml.safe_load((EXACT / "questions.yaml").read_text(encoding="utf-8")) if q["kind"] == "exact_match"
    ]
    hits = 0
    for q in questions:
        needles = ["".join(e.split()) for e in q["evidence"]]
        top = rank(q["question"])[:k]
        if any(n in "".join(c.text.split()) for n in needles for c in top):
            hits += 1
    return hits, len(questions)


@pytest.mark.skipif(not EXACT.exists(), reason="evals/fixtures/exact が無い")
def test_on_the_exact_match_corpus_the_keyword_stage_finds_the_evidence_for_most_questions():
    chunks = _exact_chunks()
    assert len(chunks) == 137  # 20c6662 の記録（新しいチャンク 137 件）
    index = KeywordIndex([c.text for c in chunks])

    def keyword_rank(question: str) -> list[Chunk]:
        return [chunks[i] for i, _ in index.ranked(question, 5)]

    hits, total = _recall_at(2, keyword_rank)
    assert total == 40
    # 実測（2026-11-01、top 2）。型番・コード・数値を質問に含む 40 問で、キーワード側だけでこの割合の根拠が上位 2 件に入る
    assert hits / total >= 0.7, f"{hits}/{total}"
