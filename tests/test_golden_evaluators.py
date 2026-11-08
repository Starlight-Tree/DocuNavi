"""golden set（evals/golden/）の評価器・比較・安全確認。実 API・DB は使わない（CI でも動く）。

実 API で流すテストは tests/test_golden_chat_real.py（GOLDEN_REAL=1 のときだけ）。
"""

import copy
from typing import Any

import pytest

from evals.golden import compare as golden_compare
from evals.golden import trace_check
from evals.golden.evaluators import (
    EVALUATORS,
    Turn,
    evaluate,
    evaluate_all,
    percentile,
    summarize,
)
from evals.golden.runner import check_environment, load_golden

DEFAULTS, CASES = load_golden()


def case(**kw: Any) -> dict[str, Any]:
    return {**DEFAULTS, "id": "x", "category": "通常", "thread": "t", "question": "q", **kw}


def turn(**kw: Any) -> Turn:
    base = {
        "id": "x",
        "rep": 1,
        "question": "q",
        "answer": "答え",
        "steps": ["search_documents", "llm"],
        "latency_s": 1.0,
        "llm_calls": 1,
        "tokens_in": 400,
        "tokens_out": 50,
    }
    return Turn(**{**base, **kw})


# ---- golden set 自体 ----


def test_the_golden_set_has_the_15_turns_with_unique_ids() -> None:
    ids = [c["id"] for c in CASES]
    assert len(ids) == 15 and len(set(ids)) == 15
    assert all(i.startswith("gc") for i in ids)


def test_every_case_defines_the_expectations_the_evaluators_need() -> None:
    for c in CASES:
        assert c["question"].strip() and c["category"] and c["thread"]
        assert isinstance(c["expect_search"], bool)
        assert c["trajectory"] and set(c["trajectory"]) <= {"llm", "search_documents"}
        assert all(isinstance(group, list) and group for group in c.get("must_include", []))
        # 検索しない期待のターンは、軌跡にも検索を含めない（期待が矛盾しない）
        assert ("search_documents" in c["trajectory"]) == c["expect_search"], c["id"]
        for name in (c.get("known_issue") or {}).get("evaluators", []):
            assert name in EVALUATORS and (c["known_issue"]["reason"]), c["id"]


def test_follow_up_turns_expect_a_query_rewrite_before_the_search() -> None:
    by_q = {c["id"]: c for c in CASES}
    for cid in ("gc12", "gc13", "gc15"):
        assert by_q[cid]["trajectory"] == ["llm", "search_documents", "llm"]


def test_threads_keep_their_turns_together_and_in_order() -> None:
    seen: list[str] = []
    for c in CASES:
        if not seen or seen[-1] != c["thread"]:
            assert c["thread"] not in seen, "同じ会話のターンが離れている"
            seen.append(c["thread"])


# ---- 評価器 ----


def test_accuracy_needs_every_fact_group_and_no_forbidden_word() -> None:
    c = case(must_include=[["午後11時", "23時"], ["届け出"]], must_not_include=["洗濯"])
    assert evaluate(c, turn(answer="午後11時です。届け出れば遅くできます"), "accuracy")[0]
    assert not evaluate(c, turn(answer="午後11時です"), "accuracy")[0]  # 2 つ目の事実が無い
    assert not evaluate(c, turn(answer="23時です。届け出が要ります。洗濯機"), "accuracy")[0]
    # 全角・半角の違いは吸収する（NFKC。「１／１０」は「1/10」になる）
    assert evaluate(case(must_include=[["1/10"]]), turn(answer="地球の１／１０"), "accuracy")[0]
    assert evaluate(case(must_include=[["1/10"]]), turn(answer="地球の1/10以下"), "accuracy")[0]


def test_grounding_flags_numbers_that_the_search_result_does_not_contain() -> None:
    c = case(expect_sources=["a.pdf"])
    ok = turn(answer="3,300 円かかります", context="出張料は3,300円です", sources=["a.pdf"])
    assert evaluate(c, ok, "grounding")[0]
    made_up = turn(answer="5,000 円かかります", context="出張料は3,300円です", sources=["a.pdf"])
    ok_, detail = evaluate(c, made_up, "grounding")
    assert not ok_ and "5000" in detail
    wrong_doc = turn(answer="3,300 円", context="3,300円", sources=["b.pdf"])
    assert not evaluate(c, wrong_doc, "grounding")[0]


def test_grounding_allows_numbers_that_come_from_the_question() -> None:
    c = case()
    t = turn(question="2月28日の次は？", answer="2月28日の次は3月です", context="3月に入寮します")
    assert evaluate(c, t, "grounding")[0]


def test_conciseness_uses_the_cases_limit() -> None:
    c = case(max_chars=10)
    assert evaluate(c, turn(answer="あ" * 10), "conciseness")[0]
    assert not evaluate(c, turn(answer="あ" * 11), "conciseness")[0]


def test_search_need_checks_presence_and_count() -> None:
    assert evaluate(case(expect_search=True), turn(), "search_need")[0]
    assert not evaluate(case(expect_search=True), turn(steps=["llm"]), "search_need")[0]
    assert not evaluate(case(expect_search=False), turn(), "search_need")[0]  # 挨拶なのに検索した
    assert evaluate(case(expect_search=False), turn(steps=["llm"]), "search_need")[0]
    too_many = turn(steps=["search_documents", "llm", "search_documents", "llm"])
    assert not evaluate(case(expect_search=True, max_search_calls=1), too_many, "search_need")[0]


def test_trajectory_must_match_the_expected_steps_exactly() -> None:
    c = case(trajectory=["llm", "search_documents", "llm"])
    assert evaluate(c, turn(steps=["llm", "search_documents", "llm"]), "trajectory")[0]
    assert not evaluate(c, turn(steps=["search_documents", "llm"]), "trajectory")[0]  # 補完が無い
    assert not evaluate(c, turn(steps=["llm", "llm", "search_documents", "llm"]), "trajectory")[0]  # ヘッジなどで増えた


def test_latency_and_cost_use_their_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    c = case(max_latency_s=2.0, max_tokens=500)
    assert evaluate(c, turn(latency_s=2.0), "latency")[0]
    assert not evaluate(c, turn(latency_s=2.1), "latency")[0]
    assert evaluate(c, turn(tokens_in=400, tokens_out=100), "cost")[0]
    assert not evaluate(c, turn(tokens_in=400, tokens_out=101), "cost")[0]
    # 単価が未設定なら費用は出さない（確かめた単価だけを使う）
    monkeypatch.delenv("GOLDEN_PRICE_IN_PER_MTOK", raising=False)
    assert "$" not in evaluate(c, turn(), "cost")[1]
    monkeypatch.setenv("GOLDEN_PRICE_IN_PER_MTOK", "1")
    monkeypatch.setenv("GOLDEN_PRICE_OUT_PER_MTOK", "2")
    assert "$0.000500" in evaluate(c, turn(tokens_in=400, tokens_out=50), "cost")[1]


def test_irrelevant_mentions_are_detected() -> None:
    assert evaluate(case(), turn(answer="記載がありません"), "no_irrelevant_mention")[0]
    bad = turn(answer="記載がありません。洗濯機の記述がありましたが、関係ありません")
    assert not evaluate(case(), bad, "no_irrelevant_mention")[0]


def test_a_failed_turn_fails_every_evaluator() -> None:
    results = evaluate_all(case(), turn(error="TimeoutError"))
    assert set(results) == set(EVALUATORS) and not any(r["passed"] for r in results.values())


def test_percentile_and_summary() -> None:
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.5) == 5
    assert percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 0.95) == 10
    assert percentile([], 0.95) == 0.0
    turns = [turn(latency_s=1.0), turn(latency_s=3.0), turn(llm_calls=0, latency_s=0.0, steps=[])]
    s = summarize(turns)
    assert s["turns"] == 3 and s["latency_p50_s"] == 2.0 and s["llm_calls"] == 2  # 拒否されたターンは速度から除く
    assert s["tokens_in"] == 1200 and s["search_calls"] == 2 and s["cost_usd"] is None


# ---- 変更前後の比較 ----


def report(
    evals: dict[str, bool], *, p95: float = 3.0, tokens: int = 450, llm: int = 1, known: list[str] | None = None
) -> dict[str, Any]:
    return {
        "summary": {
            "turns": 1,
            "latency_p50_s": 2.0,
            "latency_p95_s": p95,
            "llm_calls": llm,
            "tokens_in": tokens,
            "tokens_out": 0,
            "cost_usd": None,
            "search_calls": 1,
            "errors": 0,
        },
        "turns": [
            {
                "id": "gc01",
                "known_issue": known or [],
                "evals": {n: {"passed": ok, "detail": ""} for n, ok in evals.items()},
            }
        ],
    }


def test_compare_flags_a_case_that_used_to_pass_and_now_fails() -> None:
    base = report({"accuracy": True, "grounding": True})
    cur = report({"accuracy": False, "grounding": True})
    result = golden_compare.compare(base, cur)
    assert result["regressions"] == ["gc01/accuracy: baseline は全回通過、current は 1/1 回失敗"]


def test_compare_reports_improvements_and_ignores_known_issues() -> None:
    base = report({"accuracy": False, "search_need": False}, known=["search_need"])
    cur = report({"accuracy": True, "search_need": False}, known=["search_need"])
    result = golden_compare.compare(base, cur)
    assert not result["regressions"] and result["improvements"] == [
        "gc01/accuracy: baseline で失敗 → current は全回通過"
    ]


def test_compare_flags_slower_p95_more_tokens_and_more_llm_calls() -> None:
    base = report({"accuracy": True})
    assert not golden_compare.compare(base, report({"accuracy": True}, p95=4.4))[
        "regressions"
    ]  # +1.4s だが 1.5 倍に届かない
    slow = golden_compare.compare(base, report({"accuracy": True}, p95=5.0))["regressions"]
    assert slow == ["p95 レイテンシ: 3.00s → 5.00s"]
    assert len(golden_compare.compare(base, report({"accuracy": True}, tokens=700))["regressions"]) == 1
    assert len(golden_compare.compare(base, report({"accuracy": True}, llm=2))["regressions"]) == 1


def test_compare_flags_a_missing_result_and_renders_a_table() -> None:
    base = report({"accuracy": True})
    cur = copy.deepcopy(base)
    cur["turns"] = []
    result = golden_compare.compare(base, cur)
    assert result["regressions"] == ["gc01/accuracy: current に結果が無い"]
    assert "| p95 レイテンシ(s) |" in golden_compare.render(result)


# ---- トレースとの突き合わせ ----


def test_trace_check_compares_queries_and_sources() -> None:
    rep = {"turns": [{"id": "gc01", "rep": 1, "queries": ["q1"], "sources": ["a.pdf"]}]}
    same = {("gc01", 1): {"queries": ["q1"], "sources": ["a.pdf"]}}
    assert trace_check.check(rep, same) == []
    other = {("gc01", 1): {"queries": ["q2"], "sources": ["b.pdf"]}}
    assert len(trace_check.check(rep, other)) == 2
    assert trace_check.check(rep, {}) == ["gc01#1: トレースが見つからない"]


def test_trace_check_reads_sources_from_the_tool_output() -> None:
    out = {"output": {"artifact": [{"name": "b.pdf"}, {"name": "a.pdf"}, {"name": "a.pdf"}]}}
    assert trace_check.tool_sources(out) == ["a.pdf", "b.pdf"]
    assert trace_check.tool_sources({"artifact": [{"name": "c.pdf"}]}) == ["c.pdf"]
    assert trace_check.tool_sources({}) == []


# ---- 実行前の安全確認 ----

GOOD_ENV = {
    "GOOGLE_GENAI_USE_VERTEXAI": "true",
    "GOOGLE_CLOUD_PROJECT": "p",
    "EMBEDDING_USE_VERTEXAI": "true",
    "DB_BACKEND": "pg",
    "DATABASE_URL": "postgresql://u:pw@host.docker.internal:5433/db",
}


def test_the_safety_check_accepts_the_local_vertex_setup() -> None:
    assert check_environment(GOOD_ENV) == []


@pytest.mark.parametrize(
    "change",
    [
        {"GOOGLE_GENAI_USE_VERTEXAI": ""},
        {"GOOGLE_CLOUD_PROJECT": ""},
        {"EMBEDDING_USE_VERTEXAI": "false"},
        {"DB_BACKEND": "d1"},
        {"DATABASE_URL": ""},
        {"DATABASE_URL": "postgresql://u:pw@db.prisma.io:5432/postgres"},  # 本番の DB
        {"CHAT_SMALLTALK_SKIP": "on"},
    ],
)
def test_the_safety_check_refuses_anything_else(change: dict[str, str]) -> None:
    problems = check_environment({**GOOD_ENV, **change})
    assert problems
    assert all("pw" not in p and "prisma" not in p for p in problems)  # 値は出さない
