"""評価セット `graph_rules_ext`（遠い参照・3 条またぎを増やした GraphRAG の評価。資料 4 つ・質問 62 問）のテスト。

実 API は呼ばない。資料の辺・質問の構造・「遠い」の規則・最初のセット（`graph_rules`）に触れないことを、手元で確かめる。
最初のセットのテストは `tests/test_eval_graph.py`。
"""

import asyncio
import json
import re
from collections import Counter
from pathlib import Path

import pytest

from app.ingestion import embedding
from app.scope import Scope
from app.search import pipeline as search_pipeline
from app.search import vector as search_vector
from scripts import eval_graph as eg
from scripts import eval_vector as ev
from scripts.eval_pipeline import CorpusChunk, _sha
from scripts.eval_retrieval import load_questions

ROOT = Path(__file__).resolve().parents[1]
POOL = "市民プール利用規程.txt"
LIBRARY = "市立図書館利用規程.txt"
HALL = "市民会館ホール利用規程.txt"
FARM = "市民農園利用規程.txt"

# 資料 -> {参照する条: 参照される条の列}。資料を読んで決めた（質問の構造は、この辺を前提にしている）
EXPECTED_EDGES: dict[str, dict[int, list[int]]] = {
    POOL: {7: [5], 8: [4, 6], 9: [5], 10: [8], 11: [9], 12: [8], 13: [4], 14: [4], 15: [4], 16: [6], 17: [6]},
    LIBRARY: {4: [8], 5: [6], 9: [6, 8], 10: [9], 11: [9], 12: [7], 13: [12], 14: [6], 15: [7], 16: [7], 17: [6]},
    HALL: {
        4: [9, 10],
        5: [9],
        6: [11],
        7: [11],
        8: [10],
        12: [10, 11],
        13: [9, 11],
        14: [12],
        15: [12],
        16: [13],
        17: [13],
    },
    FARM: {
        4: [10],
        5: [9],
        6: [9],
        7: [11],
        8: [10],
        12: [9, 10, 11],
        13: [11],
        14: [12],
        15: [12],
        16: [12],
        17: [13],
        18: [9],
    },
}
ARTICLE_COUNTS = {POOL: 17, LIBRARY: 17, HALL: 17, FARM: 18}

SET_GLOBALS = (
    "CORPUS_DIR",
    "QUESTIONS_PATH",
    "COLLECTION",
    "PLAN_PATH",
    "CACHE_PATH",
    "RESULT_PATH",
    "MAX_PLANNED_CALLS",
    "HARD_CALL_LIMIT",
)


@pytest.fixture(autouse=True)
def ext_set(monkeypatch, tmp_path):
    """`graph_rules_ext` を選ぶ。出力先は一時ディレクトリにする（実際の計画・キャッシュ・結果には書かない）。終わると元に戻る。"""
    for name in SET_GLOBALS:
        monkeypatch.setattr(eg, name, getattr(eg, name))  # 元の値を記録して、終了時に戻す
    eg.use_set("graph_rules_ext")
    monkeypatch.setattr(eg, "PLAN_PATH", tmp_path / "plan.json")
    monkeypatch.setattr(eg, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(eg, "RESULT_PATH", tmp_path / "result.json")

    def no_api(*args, **kwargs):
        raise AssertionError("API を呼ぼうとしました")

    monkeypatch.setattr(embedding, "GeminiEmbedder", no_api)
    return tmp_path


@pytest.fixture(scope="module")
def corpus() -> list[CorpusChunk]:
    return eg.load_corpus(ROOT / "evals" / "fixtures" / "graph_rules_ext")


@pytest.fixture(scope="module")
def questions():
    return eg.load_eval_questions(ROOT / "evals" / "fixtures" / "graph_rules_ext" / "questions.yaml")


def _position(corpus: list[CorpusChunk], source: str, number: int) -> int:
    return next(i for i, c in enumerate(corpus) if (c.source, c.page) == (source, number))


def _holder(corpus: list[CorpusChunk], q, evidence: str) -> int:
    holders = [
        i
        for i, c in enumerate(corpus)
        if c.source in q.expected_doc_ids and ev._contains(c.text, evidence)  # noqa: SLF001
    ]
    assert len(holders) == 1, f"{q.id}: {evidence[:20]} を含むチャンクが {len(holders)} 件です"
    return holders[0]


def _title_words(chunk: CorpusChunk) -> list[str]:
    """条の見出しの題を「の」で分けた語（2 文字以上）。例: 許可の申請 -> 許可, 申請"""
    title = re.match(r"第\d+条（(.+)）", chunk.heading).group(1)
    return [w for w in title.split("の") if len(w) >= 2]


# ---- 資料 ----


def test_the_ext_set_is_selected_and_the_first_set_is_not_touched():
    assert eg.COLLECTION == "graph_rules_ext"
    assert eg.CORPUS_DIR == ROOT / "evals" / "fixtures" / "graph_rules_ext"
    first, ext = eg.SETS["graph_rules"], eg.SETS["graph_rules_ext"]
    # 最初のセットの値は変わらない（既存の結果・計画・キャッシュを壊さない）
    assert first["corpus_dir"] == ROOT / "evals" / "fixtures" / "graph_rules"
    assert first["plan"] == ROOT / "evals" / "pipeline" / "graph_plan.json"
    assert first["cache"] == ROOT / "evals" / "cache" / "graph_rules_embeddings.json"
    assert first["result"] == ROOT / "evals" / "results" / "graph_rules_compare.json"
    assert (first["planned_calls"], first["hard_limit"]) == (2, 3)
    for key in ("corpus_dir", "plan", "cache", "result"):
        assert ext[key] != first[key]
    assert (ext["planned_calls"], ext["hard_limit"]) == (5, 6)


def test_each_article_is_one_chunk_and_the_chunk_starts_with_its_article_heading(corpus):
    counts = Counter(c.source for c in corpus)
    assert dict(counts) == ARTICLE_COUNTS
    assert len(corpus) == 69
    for source, count in ARTICLE_COUNTS.items():
        assert [c.page for c in corpus if c.source == source] == list(range(1, count + 1))
    for c in corpus:
        assert c.text.startswith(f"第{c.page}条（")
        assert c.collection == "graph_rules_ext"


def test_the_ext_corpus_lives_outside_storage_pdf():
    assert not list((ROOT / "storage" / "pdf").rglob("*利用規程*"))


def test_graph_edges_are_exactly_the_designed_ones(corpus):
    edges = eg.edges_of(corpus)
    actual: dict[str, dict[int, list[int]]] = {}
    for src, targets in edges.items():
        actual.setdefault(corpus[src].source, {})[corpus[src].page] = [corpus[t].page for t in targets]
    assert actual == EXPECTED_EDGES
    assert sum(len(t) for t in edges.values()) == 52


def test_edges_never_cross_documents_even_when_article_numbers_overlap(corpus):
    for src, targets in eg.edges_of(corpus).items():
        assert all(corpus[dst].source == corpus[src].source for dst in targets)


def test_every_article_reference_points_to_an_existing_article(corpus):
    numbers = {source: set(range(1, count + 1)) for source, count in ARTICLE_COUNTS.items()}
    for c in corpus:
        for number in re.findall(r"第(\d+)条", c.text):
            assert int(number) in numbers[c.source] or int(number) == c.page


# ---- 質問 ----


def test_the_question_counts_and_ids(questions):
    assert [q.id for q in questions] == [f"gx{n:03d}" for n in range(1, 63)]
    assert Counter(q.kind for q in questions) == {
        "ref_far": 20,
        "chain3": 14,
        "ref_near": 4,
        "ref_unneeded": 8,
        "single": 8,
        "absent": 8,
    }
    assert sum(1 for q in questions if q.answerable) == 54


def test_the_question_ids_do_not_collide_with_the_first_set_or_the_evaluated_sets(questions):
    others = [ROOT / "evals" / "questions.yaml", *sorted((ROOT / "evals").glob("corpora/*/questions.yaml"))]
    others += [
        p for p in sorted((ROOT / "evals").glob("fixtures/*/questions.yaml")) if p.parent.name != "graph_rules_ext"
    ]
    ids = {q.id for path in others for q in load_questions(path)}
    assert not ids & {q.id for q in questions}


def test_every_answerable_question_has_its_evidence_in_exactly_one_chunk_of_the_expected_document(corpus, questions):
    for q in questions:
        if not q.answerable:
            continue
        assert len(q.expected_doc_ids) == 1
        for evidence in q.evidence:
            _holder(corpus, q, evidence)


def test_the_reference_questions_need_a_referrer_and_a_target_joined_by_an_edge(corpus, questions):
    edges = eg.edges_of(corpus)
    for q in questions:
        if q.kind not in eg.DEPENDENT_KINDS:
            continue
        assert q.evidence_mode == "all"
        positions = [_holder(corpus, q, e) for e in q.evidence]
        assert len(set(positions)) == len(q.evidence), f"{q.id}: 根拠が 1 つのチャンクに収まっています"
        if q.kind == "chain3":
            assert len(positions) == 3
            first, middle, last = positions
            assert middle in edges[first] and last in edges[middle], q.id
            assert last not in edges[first], f"{q.id}: 1 段で届いてしまう"  # 1 段では届かない
        else:
            assert len(positions) == 2
            referrer, target = positions
            assert target in edges[referrer], f"{q.id}: 参照元から参照先へ辺がありません"


def test_far_and_chain3_questions_do_not_contain_the_words_of_the_target_heading(corpus, questions):
    """「遠い」の機械の規則: 質問に、参照先（chain3 は終点）の条の見出しの題の語を含めない。"""
    checked = 0
    for q in questions:
        if q.kind not in ("ref_far", "chain3"):
            continue
        target = corpus[_holder(corpus, q, q.evidence[-1])]
        words = _title_words(target)
        assert words
        hit = [w for w in words if w in q.question]
        assert not hit, f"{q.id}: 質問に参照先の見出しの語 {hit} があります（{target.heading}）"
        checked += 1
    assert checked == 34


def test_the_referrers_of_far_questions_and_the_starts_of_chains_are_all_different_articles(corpus, questions):
    far = [
        (corpus[_holder(corpus, q, q.evidence[0])].source, corpus[_holder(corpus, q, q.evidence[0])].page)
        for q in questions
        if q.kind == "ref_far"
    ]
    chain = [
        (corpus[_holder(corpus, q, q.evidence[0])].source, corpus[_holder(corpus, q, q.evidence[0])].page)
        for q in questions
        if q.kind == "chain3"
    ]
    assert len(set(far)) == 20
    assert len(set(chain)) == 14
    assert not set(far) & set(chain)
    # 独立でない部分（報告に書く）: 中間の条は 8 条、終点は複数の質問で共有する
    middles = {
        (corpus[_holder(corpus, q, q.evidence[1])].source, corpus[_holder(corpus, q, q.evidence[1])].page)
        for q in questions
        if q.kind == "chain3"
    }
    assert len(middles) == 8


def test_the_control_questions_are_answered_by_one_chunk_and_match_their_kind(corpus, questions):
    edges = eg.edges_of(corpus)
    for q in questions:
        if q.kind not in ("single", "ref_unneeded"):
            continue
        holders = {_holder(corpus, q, e) for e in q.evidence}
        assert len(holders) == 1, q.id
        (holder,) = holders
        if q.kind == "ref_unneeded":
            assert edges.get(holder), f"{q.id}: 参照が要らない質問の根拠の条に、辺がありません（追加が付かない）"
        else:
            assert not edges.get(holder), f"{q.id}: single の根拠の条に辺があります"


def test_the_absent_questions_have_no_evidence_and_the_expected_scope_kinds(questions):
    absent = [q for q in questions if q.kind == "absent"]
    assert Counter(q.scope_kind for q in absent) == {"on_topic_absent": 6, "off_topic": 2}
    assert all(not q.answerable and not q.evidence for q in absent)


# ---- アプリの検索経路での追加の文脈（偽のベクトル検索。API も DB も使わない） ----


class _Env:
    def __init__(self, corpus: list[CorpusChunk], first: int) -> None:
        self.corpus = corpus
        self.first = first

    async def search(self, query, top_k, collection=None, *, scope):
        c = self.corpus[self.first]
        return [search_vector.Hit(c.source, c.text, c.page, c.page_end, 0.9, c.collection)][:top_k]

    async def fetch(self, scope, collection):
        return [search_pipeline.Chunk(c.source, c.text, c.page, c.page_end, c.collection) for c in self.corpus]


def _search_with_graph(monkeypatch, corpus, first: int, graph: str):
    env = _Env(corpus, first)
    monkeypatch.setattr(search_vector, "search", env.search)
    monkeypatch.setattr(search_pipeline, "_fetch_chunks", env.fetch)
    for name, value in (("SEARCH_HYBRID", "off"), ("SEARCH_RERANK", "off"), ("SEARCH_GRAPH", graph)):
        monkeypatch.setenv(name, value)
    search_pipeline.clear_cache()
    return asyncio.run(search_pipeline.search("質問", 1, None, scope=Scope.ALL))


def _chunks_of(hits):
    return [ev.Chunk(i, h.source, 0, None, h.text, "text", "", "") for i, h in enumerate(hits)]


def test_graph_completes_the_ref_questions_from_the_referrer_alone(monkeypatch, corpus, questions):
    checked = 0
    for q in questions:
        if q.kind not in ("ref_far", "ref_near"):
            continue
        referrer, target = (_holder(corpus, q, e) for e in q.evidence)
        off = _search_with_graph(monkeypatch, corpus, referrer, "off")
        on = _search_with_graph(monkeypatch, corpus, referrer, "on")
        assert [h.origin for h in off] == [None]
        # 参照元が複数の条を参照するときは、追加は 1 件（SEARCH_GRAPH_MAX_EXTRA の既定）なので、番号の小さい方が足される
        targets = eg.edges_of(corpus)[referrer]
        assert len(on) == 2 and on[1].origin == "graph", q.id
        assert (on[1].source, on[1].page) == (corpus[min(targets)].source, corpus[min(targets)].page)
        if len(targets) == 1:
            assert (on[1].source, on[1].text) == (corpus[target].source, corpus[target].text)
            assert not ev.satisfied(q, _chunks_of(off)) and ev.satisfied(q, _chunks_of(on)), q.id
        checked += 1
    assert checked == 24


def test_graph_does_not_reach_the_end_of_a_chain_in_one_hop(monkeypatch, corpus, questions):
    for q in questions:
        if q.kind != "chain3":
            continue
        first, middle, last = (_holder(corpus, q, e) for e in q.evidence)
        on = _search_with_graph(monkeypatch, corpus, first, "on")
        assert [(h.source, h.page) for h in on] == [
            (corpus[first].source, corpus[first].page),
            (corpus[middle].source, corpus[middle].page),
        ], q.id
        assert not ev.satisfied(q, _chunks_of(on)), q.id


def test_graph_adds_the_article_from_the_same_document_not_the_one_with_the_same_number(monkeypatch, corpus):
    # どの資料にも第9条・第10条・第12条がある。第5条（図書館）は第6条を参照する。他の資料の第6条は足されない
    start = _position(corpus, LIBRARY, 5)
    on = _search_with_graph(monkeypatch, corpus, start, "on")
    assert [(h.source, h.origin) for h in on] == [(LIBRARY, "vector"), (LIBRARY, "graph")]
    assert on[1].text.startswith("第6条（利用者カード）")


# ---- コマンド（API なし） ----


def test_plan_writes_edges_and_the_api_estimate_without_calling_the_api(ext_set):
    assert eg.cmd_plan() == 0
    plan = json.loads((ext_set / "plan.json").read_text(encoding="utf-8"))
    assert plan["chunk_count"] == 69 and len(plan["edges"]) == 52
    assert plan["fixed"]["collection"] == "graph_rules_ext"
    assert plan["fixed"]["graph_max_extra"] == 1  # アプリの既定のまま
    assert plan["api"]["doc_texts"] == 69 and plan["api"]["query_texts"] == 62
    assert plan["api"]["calls"] == 5 and plan["api"]["planned_call_limit"] == 5 and plan["api"]["hard_call_limit"] == 6
    assert plan["api"]["estimated_cost_usd"] < 0.01
    assert len(plan["questions"]) == 62


def test_embed_without_apply_sends_nothing(ext_set, capsys):
    assert eg.cmd_plan() == 0
    assert eg.cmd_embed(apply=False) == 0
    out = capsys.readouterr().out
    assert "API は呼んでいません" in out and "今回の呼び出し 5 回" in out
    assert not (ext_set / "cache.json").exists()


def test_embed_refuses_to_exceed_the_cumulative_call_limit(ext_set, monkeypatch):
    assert eg.cmd_plan() == 0
    cache = eg._load_cache()
    cache["calls"] = eg.HARD_CALL_LIMIT - 1  # あと 1 回しか残っていない（必要なのは 5 回）
    eg._save_cache(cache)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
    assert eg.cmd_embed(apply=True) == 2


def test_run_with_synthetic_embeddings_separates_the_main_hit_from_the_graph_hit(ext_set, corpus, questions):
    """質問の埋め込みを「参照元の条と同じ向き」にした偽の埋め込みで、測定の経路を端から端まで通す（数字は意味を持たない）。"""
    assert eg.cmd_plan() == 0
    order = {(c.source, c.page): i for i, c in enumerate(corpus)}

    def unit(i: int) -> list[float]:
        return [1.0 if j == i else 0.0 for j in range(embedding.DIMENSIONS)]

    cache = eg._load_cache()
    for c in corpus:
        cache["docs"][_sha(c.doc_input)] = unit(order[(c.source, c.page)])
    for q in questions:
        first = _holder(corpus, q, q.evidence[0]) if q.answerable else 500
        cache["queries"][_sha(embedding.query_text(q.question))] = unit(first)
    eg._save_cache(cache)

    assert eg.cmd_run() == 0
    report = json.loads((ext_set / "result.json").read_text(encoding="utf-8"))
    assert report["set"] == "graph_rules_ext"
    summary = report["summary"]
    n = summary["n"]
    assert (n["ref_far"], n["ref_near"], n["chain3"], n["dependent"], n["ref_unneeded"], n["single"]) == (
        20,
        4,
        14,
        38,
        8,
        8,
    )
    assert summary["n_out_of_scope"] == 8
    graph = summary["configs"]["hybrid+graph"]
    vector = summary["configs"]["vector"]
    assert vector["hit_main"]["dependent"]["1"] == 0  # 参照元だけでは、参照先の文が足りない
    assert graph["hit_main"]["dependent"]["1"] == 0
    assert graph["hit_with_graph"]["chain3"]["1"] == 0  # 1 段では 3 つ目の条に届かない
    assert graph["hit_with_graph"]["ref_far"]["1"] >= 1
    # 誤追加の内訳: 参照が要らない質問にも追加が付く（辺のある条が根拠）。single には付かない。資料外には、ヒットが無いので付かない
    fa = graph["false_additions"]["1"]
    assert fa["extras_on_unneeded"]["extras"] == 8  # ref_unneeded 8 問
    assert all(
        qid in {q.id for q in questions if q.kind == "ref_unneeded"} for qid in fa["extras_on_unneeded"]["questions"]
    )
    assert fa["extras_on_out_of_scope"]["extras"] == 0
    # あり・なしの比較: 本体の結果は変わらず、悪化は無い
    for per_k in summary["graph_vs_without"].values():
        for row in per_k.values():
            assert row["lost"] == [] and row["main_changed"] == []
    assert report["log_check"]["lines_containing_query_text"] == 0


def test_run_does_not_overwrite_an_existing_result(ext_set):
    (ext_set / "result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ev.EvalError):
        eg.cmd_run()
    assert (ext_set / "result.json").read_text(encoding="utf-8") == "{}"


def test_use_set_rejects_an_unknown_set():
    with pytest.raises(ev.EvalError):
        eg.use_set("no_such_set")
