"""scripts/eval_graph.py と evals/fixtures/graph_rules/（GraphRAG を測るための評価専用の資料と質問）のテスト。

実 API は呼ばない。資料の辺・質問の根拠・アプリの検索経路（`app/search/pipeline.py`）での追加の文脈を、手元で確かめる。
"""

import asyncio
import json
import re
from pathlib import Path

import pytest

from app.ingestion import embedding
from app.scope import Scope
from app.search import pipeline as search_pipeline
from app.search import vector as search_vector
from scripts import eval_graph as eg
from scripts import eval_vector as ev
from scripts.eval_pipeline import CorpusChunk, _sha

KOBO = "工房利用規程.txt"
LOUNGE = "ラウンジ利用規程.txt"

# (資料, 参照する条, 参照される条)。資料を読んで決めた。条の番号が両方の資料で重なる（第7条・第8条）ので、資料をまたがないことも見る
EXPECTED_EDGES = {
    (KOBO, 5, 8),
    (KOBO, 6, 9),
    (KOBO, 7, 10),
    (KOBO, 9, 11),
    (KOBO, 11, 8),
    (KOBO, 12, 7),
    (LOUNGE, 4, 7),
    (LOUNGE, 5, 8),
    (LOUNGE, 9, 8),
    (LOUNGE, 10, 7),
}


@pytest.fixture(scope="module")
def corpus() -> list[CorpusChunk]:
    return eg.load_corpus()


@pytest.fixture(scope="module")
def questions():
    return eg.load_eval_questions()


def _edge_set(corpus: list[CorpusChunk]) -> set[tuple[str, int, int]]:
    return {
        (corpus[src].source, corpus[src].page, corpus[dst].page)
        for src, targets in eg.edges_of(corpus).items()
        for dst in targets
    }


def test_each_article_is_one_chunk_and_the_chunk_starts_with_its_article_heading(corpus):
    assert [(c.source, c.page) for c in corpus] == [(LOUNGE, n) for n in range(1, 11)] + [
        (KOBO, n) for n in range(1, 13)
    ]  # 資料はファイル名の順
    for c in corpus:
        assert c.text.startswith(f"第{c.page}条（")
        assert c.collection == "graph_rules"
        assert c.doc_input.startswith(f"title: 第{c.page}条（")


def test_the_corpus_lives_outside_storage_pdf_and_the_production_chunker_is_not_used():
    root = Path(__file__).resolve().parents[1]
    assert eg.CORPUS_DIR == root / "evals" / "fixtures" / "graph_rules"
    assert not list((root / "storage" / "pdf").rglob("*規程*"))
    assert "app.ingestion.chunker" not in Path(eg.__file__).read_text(encoding="utf-8").split('"""', 2)[2]


def test_graph_edges_are_built_between_the_articles_that_refer_to_each_other(corpus):
    assert _edge_set(corpus) == EXPECTED_EDGES


def test_edges_never_cross_documents_even_when_article_numbers_overlap(corpus):
    edges = eg.edges_of(corpus)
    assert edges
    for src, targets in edges.items():
        assert all(corpus[dst].source == corpus[src].source for dst in targets)


def test_every_article_reference_in_the_texts_points_to_an_existing_article(corpus):
    numbers = {source: {c.page for c in corpus if c.source == source} for source in {c.source for c in corpus}}
    for c in corpus:
        for number in re.findall(r"第(\d+)条", c.text):
            assert int(number) in numbers[c.source] or int(number) == c.page


def test_a_body_line_starting_with_an_article_number_is_rejected(tmp_path):
    path = tmp_path / "bad.txt"
    path.write_text("題名\n\n第1条（目的）\n本文です。\n第2条の定めによる。\n", encoding="utf-8")
    with pytest.raises(ev.EvalError):
        eg.parse_articles(path)


def test_article_numbers_must_run_from_one_without_gaps(tmp_path):
    path = tmp_path / "gap.txt"
    path.write_text("題名\n\n第1条（目的）\n本文です。\n\n第3条（範囲）\n本文です。\n", encoding="utf-8")
    with pytest.raises(ev.EvalError):
        eg.parse_articles(path)


def test_every_answerable_question_has_its_evidence_in_a_chunk_of_the_expected_document(corpus, questions):
    chunks = eg.as_eval_chunks(corpus)
    for q in questions:
        if not q.answerable:
            continue
        for evidence, ids in ev.gold_chunks(q, chunks).items():
            assert ids, f"{q.id}: {evidence[:20]} がどのチャンクにもありません"


def _holder(corpus: list[CorpusChunk], q, evidence: str) -> int:
    holders = [
        i
        for i, c in enumerate(corpus)
        if c.source in q.expected_doc_ids and ev._contains(c.text, evidence)  # noqa: SLF001
    ]
    assert len(holders) == 1, f"{q.id}: {evidence[:20]} を含むチャンクが {len(holders)} 件です"
    return holders[0]


def test_the_reference_questions_need_a_referrer_and_a_target_joined_by_an_edge(corpus, questions):
    edges = eg.edges_of(corpus)
    seen = set()
    for q in questions:
        if q.kind not in eg.DEPENDENT_KINDS:
            continue
        seen.add(q.kind)
        assert q.evidence_mode == "all"
        positions = [_holder(corpus, q, e) for e in q.evidence]
        assert len(set(positions)) == len(q.evidence), f"{q.id}: 根拠が 1 つのチャンクに収まっています"
        if q.kind == "chain3":
            first, middle, last = positions
            assert middle in edges[first] and last in edges[middle]
            assert last not in edges[first]  # 1 段では届かない
        else:
            referrer, target = positions
            assert target in edges[referrer], f"{q.id}: 参照元から参照先へ辺がありません"
    assert seen == set(eg.DEPENDENT_KINDS)


def test_the_control_questions_are_answered_by_one_chunk(corpus, questions):
    for q in questions:
        if q.kind in ("single", "ref_unneeded"):
            assert len({_holder(corpus, q, e) for e in q.evidence}) == 1


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


def test_graph_returns_the_referenced_article_of_the_same_document_as_extra_context(monkeypatch, corpus, questions):
    checked = 0
    for q in questions:
        if q.kind not in ("ref_far", "ref_near"):
            continue
        referrer, target = (_holder(corpus, q, e) for e in q.evidence)

        off = _search_with_graph(monkeypatch, corpus, referrer, "off")
        on = _search_with_graph(monkeypatch, corpus, referrer, "on")

        assert [h.origin for h in off] == [None]  # 3 つとも off: search_vector.search の結果そのまま
        assert [h.origin for h in on] == ["vector", "graph"]
        assert (on[1].source, on[1].text) == (corpus[target].source, corpus[target].text)
        assert not ev.satisfied(q, [ev.Chunk(0, h.source, 0, None, h.text, "text", "", "") for h in off])
        assert ev.satisfied(q, [ev.Chunk(i, h.source, 0, None, h.text, "text", "", "") for i, h in enumerate(on)])
        checked += 1
    assert checked == 9


def test_graph_adds_the_article_from_the_same_document_not_the_one_with_the_same_number(monkeypatch, corpus):
    lounge_9 = next(i for i, c in enumerate(corpus) if (c.source, c.page) == (LOUNGE, 9))
    on = _search_with_graph(monkeypatch, corpus, lounge_9, "on")
    assert [(h.source, h.origin) for h in on] == [(LOUNGE, "vector"), (LOUNGE, "graph")]
    assert on[1].text.startswith("第8条（許可の申請）")


# ---- コマンド（API なし） ----


@pytest.fixture
def sandbox(monkeypatch, tmp_path):
    monkeypatch.setattr(eg, "PLAN_PATH", tmp_path / "plan.json")
    monkeypatch.setattr(eg, "CACHE_PATH", tmp_path / "cache.json")
    monkeypatch.setattr(eg, "RESULT_PATH", tmp_path / "result.json")

    def no_api(*args, **kwargs):
        raise AssertionError("API を呼ぼうとしました")

    monkeypatch.setattr(embedding, "GeminiEmbedder", no_api)
    return tmp_path


def test_plan_writes_edges_and_the_api_estimate_without_calling_the_api(sandbox, capsys):
    assert eg.cmd_plan() == 0
    plan = json.loads((sandbox / "plan.json").read_text(encoding="utf-8"))
    assert len(plan["edges"]) == len(EXPECTED_EDGES)
    assert plan["chunk_count"] == 22
    assert plan["api"]["calls"] == 2 and plan["api"]["calls"] <= plan["api"]["planned_call_limit"]
    assert plan["api"]["hard_call_limit"] == 3
    assert plan["fixed"]["chunking"].startswith("1 条 = 1 チャンク")
    assert len(plan["questions"]) == 22 and all(q["text"] for q in plan["questions"])


def test_embed_without_apply_sends_nothing(sandbox, capsys):
    assert eg.cmd_plan() == 0
    assert eg.cmd_embed(apply=False) == 0
    assert "API は呼んでいません" in capsys.readouterr().out
    assert not (sandbox / "cache.json").exists()


def test_embed_with_apply_stops_before_any_call_without_a_key(sandbox, monkeypatch):
    assert eg.cmd_plan() == 0
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert eg.cmd_embed(apply=True) == 2


def test_embed_refuses_to_exceed_the_cumulative_call_limit(sandbox, monkeypatch):
    assert eg.cmd_plan() == 0
    cache = eg._load_cache()
    cache["calls"] = eg.HARD_CALL_LIMIT - 1
    eg._save_cache(cache)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")
    assert eg.cmd_embed(apply=True) == 2


def test_embed_stops_at_the_first_failure_and_does_not_retry(sandbox, monkeypatch):
    calls = []

    class Failing:
        def __init__(self, api_key, budget):
            self.budget = budget

        def embed(self, texts):
            calls.append(len(texts))
            self.budget.spend()
            raise embedding.EmbeddingError("ClientError (status=429)")

    assert eg.cmd_plan() == 0
    monkeypatch.setattr(embedding, "GeminiEmbedder", Failing)
    monkeypatch.setattr(eg.time, "sleep", lambda s: pytest.fail("失敗の後に待たずに止める"))
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")

    assert eg.cmd_embed(apply=True) == 1
    assert calls == [22]  # 1 回だけ。次の呼び出し（質問）にも、同じ呼び出しの再試行にも進まない
    cache = eg._load_cache()
    assert cache["calls"] == 1 and cache["log"][0]["status"] == "error" and not cache["docs"]


def test_embed_saves_vectors_to_the_cache_and_waits_between_calls(sandbox, monkeypatch):
    sent = []

    class Fake:
        def __init__(self, api_key, budget):
            self.budget = budget

        def embed(self, texts):
            self.budget.spend()
            sent.append(len(texts))
            return [[1.0] + [0.0] * (embedding.DIMENSIONS - 1)] * len(texts)

    slept = []
    assert eg.cmd_plan() == 0
    monkeypatch.setattr(embedding, "GeminiEmbedder", Fake)
    monkeypatch.setattr(eg.time, "sleep", slept.append)
    monkeypatch.setenv("GEMINI_API_KEY", "dummy-not-a-key")

    assert eg.cmd_embed(apply=True) == 0
    assert sent == [22, 22] and slept == [eg.PAUSE_SECONDS]
    cache = eg._load_cache()
    assert cache["calls"] == 2 and len(cache["docs"]) == 22 and len(cache["queries"]) == 22
    assert eg.cmd_embed(apply=True) == 0  # 2 回目は送るものが無い
    assert sent == [22, 22]


def test_run_needs_the_cache(sandbox):
    assert eg.cmd_plan() == 0
    with pytest.raises(ev.EvalError):
        eg.cmd_run()


def test_run_with_synthetic_embeddings_separates_the_main_hit_from_the_graph_hit(sandbox, corpus, questions):
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
    report = json.loads((sandbox / "result.json").read_text(encoding="utf-8"))
    configs = report["summary"]["configs"]
    n = report["summary"]["n"]
    assert (n["ref_far"], n["ref_near"], n["chain3"], n["dependent"]) == (4, 5, 1, 10)
    vector, graph = configs["vector"], configs["hybrid+graph"]
    assert vector["hit_main"]["dependent"]["1"] == 0  # 参照元だけでは、参照先の文が足りない
    assert vector["hit_with_graph"]["dependent"]["1"] == 0  # graph が off
    assert graph["hit_with_graph"]["ref_far"]["1"] == n["ref_far"]
    assert graph["hit_with_graph"]["ref_near"]["1"] == n["ref_near"]
    assert graph["hit_with_graph"]["chain3"]["1"] == 0  # 1 段では 3 つ目の条に届かない
    assert graph["hit_main"]["dependent"]["1"] == 0
    assert report["log_check"]["lines_containing_query_text"] == 0


def test_run_does_not_overwrite_an_existing_result(sandbox):
    (sandbox / "result.json").write_text("{}", encoding="utf-8")
    with pytest.raises(ev.EvalError):
        eg.cmd_run()
    assert (sandbox / "result.json").read_text(encoding="utf-8") == "{}"


def test_the_existing_pipeline_files_are_not_the_targets():
    root = Path(__file__).resolve().parents[1]
    assert eg.RESULT_PATH != root / "evals" / "results" / "pipeline_compare.json"
    assert eg.CACHE_PATH != root / "evals" / "cache" / "pipeline_embeddings.json"
    assert eg.PLAN_PATH != root / "evals" / "pipeline" / "plan.json"
    assert eg.CACHE_PATH.parent == root / "evals" / "cache"
