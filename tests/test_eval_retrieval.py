"""scripts/eval_retrieval.py（検索の評価）のテスト。

検索そのものの精度は tests/test_rag.py が見る。ここは「評価の道具」が正しく採点し、
LLM・DB・ネットワークに触れないことを確かめる。
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from app.agents import tools
from scripts import eval_retrieval as ev
from tests.rag_eval_cases import ALL_CASES

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "evals" / "results" / "baseline_bm25.json"

# 実資料に依存しない小さな資料。「林檎」は 3 ファイルすべてに出る
CORPUS = {
    "a.txt": "冒頭の説明です。\\赤い林檎は甘い。\\続きの文です。\\青い海原は広い。",
    "b.txt": "林檎の木は高い。\\林檎の花は白い。\\桜の花が咲く。",
    "c.txt": "紅茶の葉を摘む。\\林檎の香りの茶もある。",
}


@pytest.fixture
def small_corpus(monkeypatch):
    monkeypatch.setattr(tools, "load_chunks", lambda: CORPUS)
    return CORPUS


def _q(**kwargs) -> ev.Question:
    base = {"id": "t001", "question": "海原", "category": "factual"}
    return ev.Question(**{**base, **kwargs})


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "questions.yaml"
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


SMALL_QUESTIONS = """
    - id: t001
      question: "海原"
      category: factual
      expected_doc_ids: ["a.txt"]
      evidence: ["青い海原"]
    - id: t002
      question: "林檎"
      category: factual
      expected_doc_ids: ["b.txt"]
      evidence: ["林檎の木"]
    - id: t003
      question: "宇宙船"
      category: out_of_scope
      scope_kind: off_topic
    - id: t004
      question: "紅茶"
      category: out_of_scope
      scope_kind: on_topic_absent
"""


# ---- 評価セットそのもの ----


def test_questions_file_is_valid_and_matches_corpus():
    questions = ev.load_questions()
    assert len(questions) >= 20
    assert sum(1 for q in questions if not q.answerable) >= 5
    assert {q.category for q in questions} == set(ev.CATEGORIES)
    assert {q.scope_kind for q in questions if not q.answerable} == set(ev.SCOPE_KINDS)
    assert ev.check_against_corpus(questions, tools.load_chunks()) == []


def test_questions_include_the_cases_of_test_rag():
    # tests/rag_eval_cases.py（test_rag.py の回帰テストの元）と評価セットが食い違うと、
    # 同じ質問に 2 通りの期待値ができる。移した質問は同じ期待値のままであること
    by_text = {q.question: q for q in ev.load_questions()}
    for case in ALL_CASES:
        q = by_text.get(case.query)
        assert q is not None, f"questions.yaml に無い: {case.query}"
        assert q.kind == case.kind
        assert q.answerable == bool(case.expected_files)
        assert set(q.expected_doc_ids) == set(case.expected_files)
        assert set(q.evidence) == set(case.evidence)


@pytest.mark.parametrize(
    "text",
    [
        "[]",
        "- {id: a, question: x, category: nope}",
        "- {id: a, question: x, category: factual}",
        "- {id: a, question: x, category: out_of_scope}",
        "- {id: a, question: x, category: out_of_scope, scope_kind: off_topic, expected_doc_ids: [a.txt]}",
        "- {id: a, question: x, category: factual, expected_doc_ids: [a.txt], evidence: [y], scope_kind: off_topic}",
        "- {id: a, question: x, category: factual, expected_doc_ids: a.txt, evidence: [y]}",
        "- {id: a, question: '', category: out_of_scope, scope_kind: off_topic}",
        "- {id: a, question: x, category: out_of_scope, scope_kind: off_topic}\n"
        "- {id: a, question: y, category: out_of_scope, scope_kind: off_topic}",
    ],
)
def test_load_questions_rejects_bad_files(tmp_path, text):
    path = tmp_path / "q.yaml"
    path.write_text(text, encoding="utf-8")
    with pytest.raises(ev.QuestionsError):
        ev.load_questions(path)


def test_check_against_corpus_reports_stale_expectations():
    questions = [
        _q(id="ok", expected_doc_ids=("a.txt",), evidence=("青い海原",)),
        _q(id="no_file", expected_doc_ids=("z.txt",), evidence=("青い海原",)),
        _q(id="no_evidence", expected_doc_ids=("a.txt",), evidence=("存在しない文",)),
    ]
    problems = ev.check_against_corpus(questions, CORPUS)
    assert any(p.startswith("no_file") for p in problems)
    assert any(p.startswith("no_evidence") for p in problems)
    assert not any(p.startswith("ok") for p in problems)


# ---- 採点 ----


def test_record_holds_what_search_returned(small_corpus):
    q = _q(expected_doc_ids=("a.txt",), evidence=("青い海原",))
    hits = ev.MODES["bm25"](q.question, 5)
    record = ev.evaluate_question(q, hits)

    assert hits and hits == tools.search_chunks(q.question, top_k=5)
    assert record["retrieved_files"] == [name for name, _ in hits]
    assert record["retrieved"] == [
        {"rank": i, "file": name, "text": text} for i, (name, text) in enumerate(hits, start=1)
    ]
    assert record["doc_hit"] is True
    assert record["first_hit_rank"] == 1
    assert record["keyword_hits"] == ["青い海原"]
    assert record["failure_category"] is None


def test_out_of_scope_is_correct_only_when_nothing_is_returned(small_corpus):
    empty = ev.evaluate_question(_q(question="宇宙船", category="out_of_scope", scope_kind="off_topic"), [])
    assert empty["answered_empty"] is True
    assert empty["failure_category"] is None

    q = _q(question="海原", category="out_of_scope", scope_kind="on_topic_absent")
    answered = ev.evaluate_question(q, ev.MODES["bm25"](q.question, 5))
    assert answered["answered_empty"] is False
    assert answered["failure_category"] == "unexpected_result"
    assert answered["doc_hit"] is None


def test_multi_hop_needs_every_expected_file_and_its_evidence():
    q = _q(
        category="multi_hop",
        expected_doc_ids=("a.txt", "b.txt"),
        evidence=("赤い林檎", "林檎の木"),
    )
    both = ev.evaluate_question(q, [("b.txt", "林檎の木は高い。"), ("a.txt", "赤い林檎は甘い。")])
    assert both["doc_hit"] and both["keyword_hit"]
    assert both["first_hit_rank"] == 1
    assert both["failure_category"] is None

    one_file = ev.evaluate_question(q, [("a.txt", "赤い林檎は甘い。")])
    assert one_file["doc_hit"] is False
    assert one_file["failure_category"] == "wrong_file"

    # b.txt は返ったが、根拠の文ではない
    wrong_text = ev.evaluate_question(q, [("a.txt", "赤い林檎は甘い。"), ("b.txt", "桜の花が咲く。")])
    assert wrong_text["doc_hit"] is True
    assert wrong_text["keyword_hit"] is False
    assert wrong_text["failure_category"] == "keyword_miss"


@pytest.mark.parametrize(
    ("category", "hits", "expected"),
    [
        ("factual", [], "no_hit"),
        ("paraphrase", [], "paraphrase_miss"),
        ("factual", [("b.txt", "桜の花が咲く。")], "wrong_file"),
        ("paraphrase", [("b.txt", "桜の花が咲く。")], "paraphrase_miss"),
        ("factual", [("b.txt", "x"), ("a.txt", "赤い林檎は甘い。")], "wrong_rank"),
        ("paraphrase", [("b.txt", "x"), ("a.txt", "赤い林檎は甘い。")], "wrong_rank"),
        ("factual", [("a.txt", "続きの文です。")], "keyword_miss"),
        ("paraphrase", [("a.txt", "続きの文です。")], "paraphrase_miss"),
        ("factual", [("a.txt", "赤い林檎は甘い。")], None),
    ],
)
def test_failure_categories(category, hits, expected):
    q = _q(category=category, expected_doc_ids=("a.txt",), evidence=("赤い林檎",))
    assert ev.evaluate_question(q, hits)["failure_category"] == expected


def test_summary_metrics():
    ok = _q(id="t1", expected_doc_ids=("a.txt",), evidence=("赤い林檎",))
    second = _q(id="t2", expected_doc_ids=("a.txt",), evidence=("赤い林檎",))
    missed = _q(id="t3", category="paraphrase", expected_doc_ids=("a.txt",), evidence=("赤い林檎",))
    quiet = _q(id="t4", category="out_of_scope", scope_kind="off_topic")
    noisy = _q(id="t5", category="out_of_scope", scope_kind="on_topic_absent")

    records = [
        ev.evaluate_question(ok, [("a.txt", "赤い林檎は甘い。")]),
        ev.evaluate_question(second, [("b.txt", "x"), ("a.txt", "赤い林檎は甘い。")]),
        ev.evaluate_question(missed, []),
        ev.evaluate_question(quiet, []),
        ev.evaluate_question(noisy, [("b.txt", "x")]),
    ]
    summary = ev.summarize(records, k=5)

    assert summary["total_questions"] == 5
    assert summary["answerable_questions"] == 3
    assert summary["out_of_scope_questions"] == 2
    assert summary["doc_recall_at_5"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["keyword_hit_rate_at_5"] == pytest.approx(2 / 3, abs=1e-4)
    assert summary["mrr"] == pytest.approx((1 + 0.5 + 0) / 3, abs=1e-4)
    assert summary["empty_answer_accuracy"] == 0.5
    assert summary["empty_answer_by_scope_kind"] == {
        "off_topic": {"n": 1, "empty": 1, "accuracy": 1.0},
        "on_topic_absent": {"n": 1, "empty": 0, "accuracy": 0.0},
    }
    assert summary["failure_counts"] == {
        "paraphrase_miss": 1,
        "unexpected_result": 1,
        "wrong_rank": 1,
    }


# ---- コマンド ----


def test_k_changes_what_is_evaluated(small_corpus, tmp_path):
    path = _write(tmp_path, SMALL_QUESTIONS)
    reports = {}
    for k in (1, 3):
        out = tmp_path / f"k{k}.json"
        assert ev.main(["--questions", str(path), "--k", str(k), "--out", str(out)]) == 0
        reports[k] = json.loads(out.read_text(encoding="utf-8"))

    assert reports[1]["k"] == 1 and reports[3]["k"] == 3
    assert "doc_recall_at_1" in reports[1]["summary"]
    assert "doc_recall_at_3" in reports[3]["summary"]
    # 「林檎」は 3 ファイルに出る。k で返るファイル数が変わる
    by_id = {k: {r["id"]: r for r in rep["results"]} for k, rep in reports.items()}
    assert len(by_id[1]["t002"]["retrieved_files"]) == 1
    assert len(by_id[3]["t002"]["retrieved_files"]) == 3


def test_report_is_valid_json_with_the_documented_shape(small_corpus, tmp_path):
    path = _write(tmp_path, SMALL_QUESTIONS)
    out = tmp_path / "nested" / "report.json"
    assert ev.main(["--questions", str(path), "--k", "5", "--out", str(out)]) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["mode"] == "bm25"
    assert report["k"] == 5
    assert report["generated_at"].endswith("+09:00")
    assert report["corpus"]["files"] == sorted(CORPUS)
    assert len(report["corpus"]["sha256"]) == 64
    assert set(report["search_params"]) >= {"TOP_K", "GENERIC_TERM_RATIO", "MIN_SHARED_TERMS"}
    assert {
        "total_questions",
        "doc_recall_at_5",
        "keyword_hit_rate_at_5",
        "mrr",
        "empty_answer_accuracy",
    } <= set(report["summary"])
    assert [r["id"] for r in report["results"]] == ["t001", "t002", "t003", "t004"]
    assert {
        "id",
        "question",
        "category",
        "expected_doc_ids",
        "retrieved_files",
        "doc_hit",
        "first_hit_rank",
        "keyword_hits",
        "failure_category",
        "retrieved",
    } <= set(report["results"][0])


def test_main_refuses_a_questions_file_that_does_not_match_the_corpus(small_corpus, tmp_path, capsys):
    path = _write(
        tmp_path,
        """
        - id: t001
          question: "海原"
          category: factual
          expected_doc_ids: ["a.txt"]
          evidence: ["存在しない文"]
        """,
    )
    out = tmp_path / "report.json"
    assert ev.main(["--questions", str(path), "--out", str(out)]) == 2
    assert not out.exists()
    assert "存在しない文" in capsys.readouterr().err


def test_main_reports_a_missing_questions_file(tmp_path, capsys):
    assert ev.main(["--questions", str(tmp_path / "none.yaml"), "--out", str(tmp_path / "r.json")]) == 2
    assert "評価セットを読めません" in capsys.readouterr().err


def test_runs_without_llm_or_network(tmp_path):
    # 別プロセスで実行し、ネットワークを塞いだ上で、LLM のモジュールが読み込まれていないことを確かめる
    out = tmp_path / "report.json"
    code = textwrap.dedent(
        f"""
        import socket, sys

        def deny(*args, **kwargs):
            raise AssertionError("network access")

        socket.socket.connect = deny
        socket.create_connection = deny

        from scripts import eval_retrieval

        assert eval_retrieval.main(["--k", "3", "--out", {str(out)!r}]) == 0
        loaded = [
            name
            for name in ("langchain_google_genai", "google.genai", "app.agents.graph", "app.main", "psycopg")
            if name in sys.modules
        ]
        assert not loaded, loaded
        """
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        cwd=ROOT,
        env={**os.environ, "PYTHONPATH": str(ROOT), "GEMINI_API_KEY": "", "DATABASE_URL": ""},
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["total_questions"] >= 20


# ---- baseline からの劣化 ----


def test_current_search_does_not_regress_from_the_committed_baseline(tmp_path):
    # evals/results/baseline_bm25.json は「現行 BM25 の実測」。同じ資料・同じ質問で、
    # 該当数が baseline を下回ったら退行。上回ったら改善なので baseline を取り直す
    # （python -m scripts.eval_retrieval --mode bm25 --k 5）。件数が少ないので、割合ではなく件数で比べる
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    current = ev.run(ev.load_questions(), baseline["mode"], baseline["k"])
    if current["corpus"]["sha256"] != baseline["corpus"]["sha256"]:
        pytest.skip("資料が変わった。baseline を取り直す（python -m scripts.eval_retrieval --mode bm25 --k 5）")

    now, then = current["summary"]["counts"], baseline["summary"]["counts"]
    regressed = {name: (then[name], now[name]) for name in then if now[name] < then[name]}
    assert not regressed, f"baseline より該当数が減った（baseline, 現在）: {regressed}"
    assert current["summary"]["total_questions"] == baseline["summary"]["total_questions"], (
        "質問の数が baseline と違う。評価セットを変えたら baseline を取り直す"
    )
