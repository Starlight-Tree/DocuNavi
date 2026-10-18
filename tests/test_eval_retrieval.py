"""scripts/eval_retrieval.py（検索の評価）のテスト。

検索そのものの精度は tests/test_bm25_baseline.py が見る。ここは「評価の道具」が正しく採点し、
LLM・DB・ネットワークに触れないことを確かめる。
"""

import json
import os
import re
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from scripts import bm25_baseline as tools
from scripts import eval_retrieval as ev
from tests.rag_eval_cases import ALL_CASES
from tests.zahard_corpus import ZAHARD_PDF_DIR

ROOT = Path(__file__).resolve().parents[1]
RESULTS = ROOT / "evals" / "results"
QUESTIONS = ROOT / "evals" / "questions.yaml"
# ザーハードの PDF 版の baseline（storage/pdf/zahard/ の PDF を、取り込みと同じ経路で読んだ BM25 の実測）。
# baseline_bm25.json / _k2.json は、ザーハードが .txt だった時代の実測（比較の記録として残す。コードは読まない）
ZAHARD_BASELINES = ["baseline_bm25_zahard.json", "baseline_bm25_zahard_k2.json"]

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
    assert ev.check_against_corpus(questions, ev.load_pdf_corpus(ZAHARD_PDF_DIR)[0]) == []


def test_questions_include_the_cases_of_test_rag():
    # tests/rag_eval_cases.py（test_bm25_baseline.py の回帰テストの元）と評価セットが食い違うと、
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

        assert eval_retrieval.main(
            ["--k", "3", "--corpus", {str(ZAHARD_PDF_DIR)!r}, "--questions", {str(QUESTIONS)!r}, "--out", {str(out)!r}]
        ) == 0
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


@pytest.mark.parametrize("name", ZAHARD_BASELINES)
def test_current_search_does_not_regress_from_the_committed_baseline(name, tmp_path):
    # evals/results/baseline_bm25_zahard*.json は「現行 BM25 の実測」。同じ PDF・同じ質問で、
    # 該当数が baseline を下回ったら退行。上回ったら改善なので baseline を取り直す
    # （python -m scripts.eval_retrieval --mode bm25 --k 5 --corpus storage/pdf/zahard --questions evals/questions.yaml）。
    # 件数が少ないので、割合ではなく件数で比べる
    baseline = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    out = tmp_path / "r.json"
    argv = [
        "--corpus",
        str(ZAHARD_PDF_DIR),
        "--questions",
        str(QUESTIONS),
        "--k",
        str(baseline["k"]),
        "--out",
        str(out),
    ]
    assert ev.main(argv) == 0
    current = json.loads(out.read_text(encoding="utf-8"))
    if current["corpus"]["pdf_sha256"] != baseline["corpus"]["pdf_sha256"]:
        pytest.skip(
            "PDF が変わった。baseline を取り直す（--corpus storage/pdf/zahard --questions evals/questions.yaml）"
        )

    now, then = current["summary"]["counts"], baseline["summary"]["counts"]
    regressed = {name: (then[name], now[name]) for name in then if now[name] < then[name]}
    assert not regressed, f"baseline より該当数が減った（baseline, 現在）: {regressed}"
    assert current["summary"]["total_questions"] == baseline["summary"]["total_questions"], (
        "質問の数が baseline と違う。評価セットを変えたら baseline を取り直す"
    )


@pytest.mark.parametrize(
    ("pdf_baseline", "text_baseline"),
    [
        ("baseline_bm25_zahard.json", "baseline_bm25.json"),
        ("baseline_bm25_zahard_k2.json", "baseline_bm25_k2.json"),
        ("baseline_bm25_app_pdf.json", "baseline_bm25_app.json"),
        ("baseline_bm25_app_pdf_k2.json", "baseline_bm25_app_k2.json"),
    ],
)
def test_pdf_baselines_match_the_text_era_baselines_apart_from_the_zahard_file_names(pdf_baseline, text_baseline):
    # ザーハードを .txt から PDF にしても、BM25 の結果（質問ごとに返るファイルと本文、指標）は変わっていない、という記録。
    # 違いはファイル名の拡張子だけ（ザーハード1〜3.txt → .pdf）。.txt の時代の baseline は、この比較のために残してある
    def as_text(report: dict) -> dict:
        return json.loads(re.sub(r"(ザーハード[123])\.pdf", r"\1.txt", json.dumps(report, ensure_ascii=False)))

    now = json.loads((RESULTS / pdf_baseline).read_text(encoding="utf-8"))
    then = json.loads((RESULTS / text_baseline).read_text(encoding="utf-8"))
    assert as_text(now)["summary"] == then["summary"]
    assert as_text(now)["results"] == then["results"]


@pytest.mark.parametrize("name", ["baseline_bm25_app_pdf.json", "baseline_bm25_app_pdf_k2.json"])
def test_app_search_matches_the_committed_baseline(name):
    # アプリが検索する全資料（storage/pdf/ の PDF。ザーハードも PDF）を、全部の質問で。
    # 件数ではなく、質問ごとに返るファイルの並びまで一致を求める。資料群を足したときに、既存の質問の結果が
    # 良くも悪くも動いたら（混ざって採点された、など）知らせる。意図した変更なら baseline を取り直す
    baseline = json.loads((RESULTS / name).read_text(encoding="utf-8"))
    assert baseline["corpus"]["source"] == "app"
    questions = [q for path in baseline["question_sets"] for q in ev.load_questions(ROOT / path)]
    current = ev.run(questions, baseline["mode"], baseline["k"], collections=tools.load_collections())
    if current["corpus"]["sha256"] != baseline["corpus"]["sha256"]:
        pytest.skip("資料が変わった。baseline を取り直す（python -m scripts.eval_retrieval --mode bm25 --app）")

    now = {r["id"]: r["retrieved_files"] for r in current["results"]}
    then = {r["id"]: r["retrieved_files"] for r in baseline["results"]}
    assert now == then
    assert current["summary"]["counts"] == baseline["summary"]["counts"]


def test_app_baseline_is_the_sum_of_the_single_collection_baselines():
    # アプリ全体の結果は、各資料群を単独で評価した結果と同じ（互いに邪魔をしていない）
    for suffix in ("", "_k2"):
        app = json.loads((RESULTS / f"baseline_bm25_app_pdf{suffix}.json").read_text(encoding="utf-8"))
        parts = [
            json.loads((RESULTS / f"baseline_bm25{kind}{suffix}.json").read_text(encoding="utf-8"))
            for kind in ("_zahard", "_cafe", "_guides")
        ]
        summed = {key: sum(part["summary"]["counts"][key] for part in parts) for key in app["summary"]["counts"]}
        # 答えのある質問は、各資料群を単独で評価した結果と同じ
        assert app["summary"]["counts"]["doc_hit"] == summed["doc_hit"]
        assert app["summary"]["counts"]["keyword_hit"] == summed["keyword_hit"]
        # 資料外の質問は、他の資料群が偶然に返すことがあるので、単独の合計より増えるだけ（減ることはない）。
        # 実測: t010（寮でペット）が cafe の PDF を返す（tests/test_bm25_collections.py の KNOWN_SPILL）
        assert app["summary"]["counts"]["answered_empty"] <= summed["answered_empty"]
        assert app["summary"]["total_questions"] == sum(part["summary"]["total_questions"] for part in parts)


def test_app_mode_reports_the_sets_and_pdfs(tmp_path):
    out = tmp_path / "app.json"
    assert ev.main(["--app", "--k", "3", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["corpus"]["source"] == "app"
    assert set(report["corpus"]["collections"]) == {"zahard", "cafe", "guides"}
    assert set(report["corpus"]["pdf_sha256"]) == {
        "zahard/ザーハード1.pdf",
        "zahard/ザーハード2.pdf",
        "zahard/ザーハード3.pdf",
        "cafe/cafe_store_guide.pdf",
        "guides/aoba_aw820_manual.pdf",
        "guides/tsubasa_dorm_rules.pdf",
    }
    assert report["question_sets"] == [
        "evals/questions.yaml",
        "evals/corpora/cafe/questions.yaml",
        "evals/corpora/guides/questions.yaml",
    ]
    assert report["summary"]["total_questions"] == 68


def test_app_mode_rejects_ids_shared_between_question_sets(tmp_path, monkeypatch, capsys):
    # 評価セットをまたいで id が重なると、結果の対応が付かない。黙って片方を捨てずに止める
    corpora = tmp_path / "corpora"
    (corpora / "other").mkdir(parents=True)
    (corpora / "other" / "questions.yaml").write_text(
        textwrap.dedent(
            """
            - id: q001
              question: "遺跡の構造は？"
              category: factual
              expected_doc_ids: ["ザーハード2.txt"]
              evidence: ["多層的な構造"]
            """
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(ev, "CORPORA_DIR", corpora)

    assert ev.main(["--app", "--out", str(tmp_path / "o.json")]) == 2
    assert "q001" in capsys.readouterr().err
    assert not (tmp_path / "o.json").exists()


def test_app_and_corpus_cannot_be_combined(capsys):
    with pytest.raises(SystemExit) as excinfo:
        ev.main(["--app", "--corpus", str(ROOT / "evals" / "corpora" / "cafe")])
    assert excinfo.value.code == 2


# ---- evidence_mode / answer / page / PDF の資料 ----


def test_evidence_mode_all_needs_every_evidence_but_any_needs_one_per_file():
    rows = ("港町本店 港町 68席", "湖畔テラス店 湖畔 76席")
    hits = [("g.pdf", "港町本店 港町 68席")]  # 2 行のうち 1 行だけ返った

    any_q = _q(expected_doc_ids=("g.pdf",), evidence=rows, evidence_mode="any")
    all_q = _q(expected_doc_ids=("g.pdf",), evidence=rows, evidence_mode="all")

    assert ev.evaluate_question(any_q, hits)["keyword_hit"] is True
    partial = ev.evaluate_question(all_q, hits)
    assert partial["keyword_hit"] is False
    assert partial["failure_category"] == "keyword_miss"
    assert partial["evidence_mode"] == "all"
    complete = ev.evaluate_question(all_q, [("g.pdf", "港町本店 港町 68席\n湖畔テラス店 湖畔 76席")])
    assert complete["keyword_hit"] is True


def test_evidence_matching_ignores_whitespace_and_width():
    # PDF では、表のセルが改行や空白で区切られて出る。全角の数字や記号も揺れる
    q = _q(expected_doc_ids=("g.pdf",), evidence=("港町本店 港町 7:00〜21:00 なし 68席",))
    extracted = "港町本店\n港町\n７:００〜２１:００\nなし\n68席"
    assert ev.evaluate_question(q, [("g.pdf", extracted)])["keyword_hit"] is True
    assert ev.evaluate_question(q, [("g.pdf", "港町本店 港町 7:00〜21:00 なし 69席")])["keyword_hit"] is False


def test_load_questions_reads_the_optional_fields(tmp_path):
    path = _write(
        tmp_path,
        """
        - id: c1
          question: "座席数が最も多い店舗は？"
          category: factual
          kind: aggregate
          evidence_mode: all
          answer: "湖畔テラス店です。"
          page: ""
          expected_doc_ids: ["g.pdf"]
          evidence: ["湖畔テラス店 76席"]
        - id: c2
          question: "本文？"
          category: factual
          page: 2
          expected_doc_ids: ["g.pdf"]
          evidence: ["本文"]
        """,
    )
    first, second = ev.load_questions(path)
    assert (first.evidence_mode, first.answer, first.page, first.kind) == (
        "all",
        "湖畔テラス店です。",
        None,
        "aggregate",
    )
    assert (second.evidence_mode, second.answer, second.page) == ("any", None, 2)


@pytest.mark.parametrize(
    "extra",
    ["evidence_mode: some", "page: 0", "page: abc", "page: true", "answer: 3"],
)
def test_load_questions_rejects_bad_optional_fields(tmp_path, extra):
    path = _write(
        tmp_path,
        f"""
        - id: c1
          question: "q"
          category: factual
          expected_doc_ids: ["g.pdf"]
          evidence: ["x"]
          {extra}
        """,
    )
    with pytest.raises(ev.QuestionsError):
        ev.load_questions(path)


def test_the_approved_cafe_questions_load():
    questions = ev.load_questions(ROOT / "evals" / "corpora" / "cafe" / "questions.yaml")
    assert len(questions) == 10
    by_id = {q.id: q for q in questions}
    assert by_id["c006"].evidence_mode == "all"
    assert len(by_id["c006"].evidence) == 12  # 集計の質問は、表の全行
    assert {q.category for q in questions} == {"factual", "paraphrase", "out_of_scope"}
    assert all(q.page is None for q in questions)


def test_search_chunks_accepts_another_corpus_and_keeps_the_default():
    other = {"x.pdf": "湖畔テラス店は76席です。\n港町本店は68席です。"}
    hits = tools.search_chunks("湖畔テラス店", corpus=other)
    assert [name for name, _ in hits] == ["x.pdf"]
    # corpus を渡さなければ、BM25 の基準線が検索する資料（storage/ の全部）。差し替えた資料は入らない
    assert [name for name, _ in tools.search_chunks("湖畔テラス店")] == ["cafe_store_guide.pdf"]


def _pdf_corpus(tmp_path, *, questions: str):
    from tests.pdf_fixtures import build_pdf

    corpus = tmp_path / "shop"
    corpus.mkdir()
    (corpus / "guide.pdf").write_bytes(
        build_pdf(
            [
                ["みなとコーヒーの店舗案内です。", "座席数が最も多いのは湖畔テラス店です。"],
                ["港町本店の定休日はありません。"],
            ]
        )
    )
    (corpus / "questions.yaml").write_text(textwrap.dedent(questions), encoding="utf-8")
    return corpus


def test_corpus_mode_evaluates_pdfs_and_locates_evidence_pages(tmp_path):
    corpus = _pdf_corpus(
        tmp_path,
        questions="""
        - id: p1
          question: "座席数が最も多いのは？"
          category: factual
          expected_doc_ids: ["guide.pdf"]
          evidence: ["座席数が最も多いのは湖畔テラス店です。"]
          page: 1
        - id: p2
          question: "港町本店の定休日は？"
          category: factual
          expected_doc_ids: ["guide.pdf"]
          evidence: ["港町本店の定休日はありません。"]
        - id: p3
          question: "宇宙船の速度は？"
          category: out_of_scope
          scope_kind: off_topic
        """,
    )
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(corpus), "--k", "3", "--out", str(out)]) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["corpus"]["source"] == "pdf:shop"
    assert report["corpus"]["files"] == ["guide.pdf"]
    assert report["corpus"]["pages"] == {"guide.pdf": 2}
    assert report["evidence_pages"]["p1"] == {"座席数が最も多いのは湖畔テラス店です。": [1]}
    assert report["evidence_pages"]["p2"] == {"港町本店の定休日はありません。": [2]}
    results = {r["id"]: r for r in report["results"]}
    assert results["p1"]["doc_hit"] and results["p1"]["keyword_hit"]
    assert results["p2"]["doc_hit"] and results["p2"]["keyword_hit"]
    assert results["p3"]["answered_empty"] is True


def test_corpus_mode_default_output_name_includes_the_directory(tmp_path, monkeypatch):
    corpus = _pdf_corpus(
        tmp_path,
        questions="""
        - id: p1
          question: "座席数が最も多いのは？"
          category: factual
          expected_doc_ids: ["guide.pdf"]
          evidence: ["座席数が最も多いのは湖畔テラス店です。"]
        """,
    )
    monkeypatch.setattr(ev, "RESULTS_DIR", tmp_path / "results")
    assert ev.main(["--corpus", str(corpus)]) == 0
    assert (tmp_path / "results" / "baseline_bm25_shop.json").is_file()


def test_corpus_mode_refuses_a_page_that_the_extraction_contradicts(tmp_path, capsys):
    corpus = _pdf_corpus(
        tmp_path,
        questions="""
        - id: p1
          question: "座席数が最も多いのは？"
          category: factual
          expected_doc_ids: ["guide.pdf"]
          evidence: ["座席数が最も多いのは湖畔テラス店です。"]
          page: 2
        """,
    )
    assert ev.main(["--corpus", str(corpus), "--out", str(tmp_path / "r.json")]) == 2
    err = capsys.readouterr().err
    assert "page=2" in err
    assert not (tmp_path / "r.json").exists()


def test_corpus_mode_does_not_drop_a_pdf_that_cannot_be_read(tmp_path, capsys):
    corpus = _pdf_corpus(
        tmp_path,
        questions="""
        - id: p1
          question: "座席数が最も多いのは？"
          category: factual
          expected_doc_ids: ["guide.pdf"]
          evidence: ["座席数が最も多いのは湖畔テラス店です。"]
        """,
    )
    (corpus / "broken.pdf").write_bytes(b"not a pdf")
    assert ev.main(["--corpus", str(corpus), "--out", str(tmp_path / "r.json")]) == 2
    err = capsys.readouterr().err
    assert "broken.pdf: unreadable" in err
    assert not (tmp_path / "r.json").exists()


# ---- extraction_miss（根拠が抽出結果に無い質問を、止まらずに数える） ----

EXTRACTION_QUESTIONS = """
- id: p1
  question: "座席数が最も多いのは？"
  category: factual
  expected_doc_ids: ["guide.pdf"]
  evidence: ["座席数が最も多いのは湖畔テラス店です。"]
- id: p2
  question: "港町本店の駐車場は何台ですか？"
  category: factual
  expected_doc_ids: ["guide.pdf"]
  evidence: ["港町本店の駐車場は 12 台です。"]
- id: p3
  question: "宇宙船の速度は？"
  category: out_of_scope
  scope_kind: off_topic
"""


def test_find_extraction_misses_lists_the_evidence_that_the_extraction_lacks():
    corpus = {"a.pdf": "港町本店 68席\n湖畔テラス店 76席", "b.pdf": "キャンペーンは 4 月 1 日から"}
    rows = ("港町本店 68席", "湖畔テラス店 76席", "天神本店 64席")
    questions = [
        _q(id="m_all", expected_doc_ids=("a.pdf",), evidence=rows, evidence_mode="all"),
        _q(id="m_any_partial", expected_doc_ids=("a.pdf",), evidence=rows, evidence_mode="any"),
        _q(id="ok", expected_doc_ids=("a.pdf",), evidence=rows[:2], evidence_mode="all"),
        # 根拠は b にあるが、a には 1 つも無い（2 冊をあわせる質問で、a の根拠を読み取れていない）
        _q(
            id="m_doc_without",
            expected_doc_ids=("a.pdf", "b.pdf"),
            evidence=("港町本店 68席", "キャンペーンは 4 月 1 日から"),
            evidence_mode="any",
        ),
        _q(id="no_file", expected_doc_ids=("nope.pdf",), evidence=("何か",)),
        _q(id="off", category="out_of_scope", scope_kind="off_topic"),
    ]
    misses = ev.find_extraction_misses(questions, corpus)
    assert set(misses) == {"m_all", "m_any_partial"}
    assert misses["m_all"] == ["天神本店 64席"]
    # 期待ファイルが資料に無いのは、読み取りの失敗ではなく評価セットの誤り（check_against_corpus が止める）
    assert "no_file" not in misses and "off" not in misses
    assert [m for m in ev.check_against_corpus(questions, corpus) if m.startswith("no_file")]


def test_a_missing_evidence_is_counted_as_extraction_miss_instead_of_a_search_failure():
    q = _q(expected_doc_ids=("g.pdf",), evidence=("港町本店の駐車場は 12 台です。",))
    hits = [("g.pdf", "港町本店の定休日はありません。")]

    plain = ev.evaluate_question(q, hits)
    assert plain["failure_category"] == "keyword_miss" and "extraction_miss" not in plain

    counted = ev.evaluate_question(q, hits, ["港町本店の駐車場は 12 台です。"])
    assert counted["failure_category"] == "extraction_miss"
    assert counted["extraction_miss"] is True
    assert counted["missing_evidence"] == ["港町本店の駐車場は 12 台です。"]
    # 検索そのものの結果（返ったファイル・順位）は消さない
    assert counted["doc_hit"] is True and counted["retrieved_files"] == ["g.pdf"]


def test_a_partly_missing_evidence_is_flagged_but_not_counted_as_a_failure_when_search_succeeds():
    q = _q(expected_doc_ids=("g.pdf",), evidence=("答えの文。", "言い換えた別の文。"), evidence_mode="any")
    record = ev.evaluate_question(q, [("g.pdf", "答えの文。")], ["言い換えた別の文。"])
    assert record["keyword_hit"] is True
    assert record["failure_category"] is None
    assert record["extraction_miss"] is True


def test_corpus_mode_stops_on_a_missing_evidence_unless_it_is_allowed(tmp_path, capsys):
    corpus = _pdf_corpus(tmp_path, questions=EXTRACTION_QUESTIONS)
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(corpus), "--out", str(out)]) == 2
    err = capsys.readouterr().err
    assert "駐車場は 12 台" in err and "--allow-missing-evidence" in err
    assert not out.exists()


def test_corpus_mode_counts_extraction_misses_when_allowed(tmp_path):
    corpus = _pdf_corpus(tmp_path, questions=EXTRACTION_QUESTIONS)
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(corpus), "--k", "3", "--allow-missing-evidence", "--out", str(out)]) == 0

    report = json.loads(out.read_text(encoding="utf-8"))
    summary = report["summary"]
    assert summary["extraction_miss"]["n"] == 1
    assert summary["extraction_miss"]["ids"] == ["p2"]
    assert summary["failure_counts"] == {"extraction_miss": 1}
    # 全体の指標は、読み取りの失敗も含む（パイプライン全体の結果）。読み取れた質問だけの指標も添える
    assert summary["counts"]["keyword_hit"] == 1 and summary["answerable_questions"] == 2
    only = summary["extraction_miss"]["extracted_only"]
    assert (only["n"], only["doc_hits"], only["keyword_hits"]) == (1, 1, 1)
    results = {r["id"]: r for r in report["results"]}
    assert results["p2"]["failure_category"] == "extraction_miss"
    assert "extraction_miss" not in results["p1"]
    assert results["p3"]["answered_empty"] is True


def test_the_report_has_no_extraction_miss_key_unless_it_is_allowed(tmp_path):
    corpus = _pdf_corpus(tmp_path, questions=EXTRACTION_QUESTIONS.split("- id: p2")[0])
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(corpus), "--out", str(out)]) == 0
    assert "extraction_miss" not in json.loads(out.read_text(encoding="utf-8"))["summary"]


def test_allowing_missing_evidence_does_not_hide_a_missing_expected_file(tmp_path, capsys):
    corpus = _pdf_corpus(
        tmp_path,
        questions="""
        - id: p1
          question: "座席数が最も多いのは？"
          category: factual
          expected_doc_ids: ["nope.pdf"]
          evidence: ["座席数が最も多いのは湖畔テラス店です。"]
        """,
    )
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(corpus), "--allow-missing-evidence", "--out", str(out)]) == 2
    assert "nope.pdf" in capsys.readouterr().err
    assert not out.exists()


def test_a_page_of_an_extraction_miss_is_not_checked_when_it_is_allowed(tmp_path):
    corpus = _pdf_corpus(
        tmp_path,
        questions="""
        - id: p2
          question: "港町本店の駐車場は何台ですか？"
          category: factual
          expected_doc_ids: ["guide.pdf"]
          evidence: ["港町本店の駐車場は 12 台です。"]
          page: 2
        """,
    )
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(corpus), "--allow-missing-evidence", "--out", str(out)]) == 0
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["extraction_miss"]["ids"] == ["p2"]


CAFE_DIR = ROOT / "evals" / "corpora" / "cafe"
CAFE_BASELINE = ROOT / "evals" / "results" / "baseline_bm25_cafe.json"


@pytest.mark.skipif(
    not (CAFE_DIR / "cafe_store_guide.pdf").exists(), reason="評価用の PDF（evals/corpora/cafe/）が無い"
)
def test_cafe_pdf_search_does_not_regress_from_the_committed_baseline(tmp_path):
    # baseline は「現行の抽出・チャンク化・BM25 の実測」。同じ PDF で、該当数が baseline を下回ったら退行。
    # 比べるのは入力の PDF が同じときだけ（チャンク化を変えても PDF のハッシュは動かない）。上回ったら baseline を取り直す
    baseline = json.loads(CAFE_BASELINE.read_text(encoding="utf-8"))
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(CAFE_DIR), "--k", str(baseline["k"]), "--out", str(out)]) == 0
    current = json.loads(out.read_text(encoding="utf-8"))
    if current["corpus"]["pdf_sha256"] != baseline["corpus"]["pdf_sha256"]:
        pytest.skip("PDF が変わった。baseline を取り直す（--corpus evals/corpora/cafe）")

    now, then = current["summary"]["counts"], baseline["summary"]["counts"]
    regressed = {name: (then[name], now[name]) for name in then if now[name] < then[name]}
    assert not regressed, f"baseline より該当数が減った（baseline, 現在）: {regressed}"
    assert current["summary"]["total_questions"] == baseline["summary"]["total_questions"]


MENU_DIR = ROOT / "evals" / "fixtures" / "menu"
MENU_BASELINE = ROOT / "evals" / "results" / "baseline_bm25_menu.json"


@pytest.mark.skipif(not (MENU_DIR / "cafe_menu.pdf").exists(), reason="評価用の PDF（evals/fixtures/menu/）が無い")
def test_menu_pdfs_search_does_not_regress_from_the_committed_baseline(tmp_path):
    # 折り返したセルは、チャンク化（chunker._merge_wrapped_rows）が列ごとに 1 行へ組み直すので、根拠が抽出結果に無い質問は無い
    # （--allow-missing-evidence は要らない）。組み直しが効かなくなると、「評価セットが資料と合っていません」で止まる
    baseline = json.loads(MENU_BASELINE.read_text(encoding="utf-8"))
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(MENU_DIR), "--k", str(baseline["k"]), "--out", str(out)]) == 0
    current = json.loads(out.read_text(encoding="utf-8"))
    if current["corpus"]["pdf_sha256"] != baseline["corpus"]["pdf_sha256"]:
        pytest.skip("PDF が変わった。baseline を取り直す（--corpus evals/fixtures/menu）")

    now, then = current["summary"]["counts"], baseline["summary"]["counts"]
    regressed = {name: (then[name], now[name]) for name in then if now[name] < then[name]}
    assert not regressed, f"baseline より該当数が減った（baseline, 現在）: {regressed}"
    assert current["summary"]["total_questions"] == baseline["summary"]["total_questions"]
    assert "extraction_miss" not in current["summary"]


def test_question_ids_do_not_collide_between_the_evaluated_sets_and_the_fixtures():
    # --app は評価セットの間の重なりを調べるが、fixtures の質問は --app に入らない。id が重なると、結果の取り違えのもとになる
    seen: dict[str, Path] = {}
    paths = [ROOT / "evals" / "questions.yaml", *sorted((ROOT / "evals").glob("corpora/*/questions.yaml"))]
    paths += sorted((ROOT / "evals").glob("fixtures/*/questions.yaml"))
    for path in paths:
        for q in ev.load_questions(path):
            assert q.id not in seen, f"{q.id}: {seen[q.id].name} と {path} で重なっています"
            seen[q.id] = path


# ---- まとまり（表）の返し方 ----

TABLE = (
    "店舗名 エリア 座席数",
    "港町本店 港町 68席",
    "港町ベイ店 港町 42席",
    "湖畔テラス店 湖畔 76席",
    "鳥栖店 佐賀 30席",
)


def test_a_group_is_returned_whole_when_one_of_its_rows_matches():
    corpus = {"g.pdf": ["前置きの文です。", TABLE, "後置きの文です。"]}
    ((name, text),) = tools.search_chunks("湖畔テラス店", corpus=corpus)

    assert name == "g.pdf"
    lines = text.split("\n")
    assert lines[1:6] == list(TABLE)  # 表全体が、行の順のまま
    assert "前置きの文です。" in lines and "後置きの文です。" in lines  # 前後の文は、これまでどおり付く


def test_without_a_group_only_the_matching_row_and_its_neighbours_are_returned():
    corpus = {"g.pdf": ["前置きの文です。", *TABLE, "後置きの文です。"]}
    ((_, text),) = tools.search_chunks("湖畔テラス店", corpus=corpus)

    assert "湖畔テラス店 湖畔 76席" in text
    assert "店舗名 エリア 座席数" not in text  # 見出し行は、近くになければ返らない


def test_rows_of_one_group_count_once_toward_the_per_file_limit(monkeypatch):
    # 同じ表の複数の行が採用されても、別の文の枠を使い切らない
    monkeypatch.setattr(tools, "MAX_PASSAGES_PER_FILE", 2)
    table = ("テラス席の表 A", "テラス席の表 B", "テラス席の表 C")
    corpus = {"g.pdf": [table, "ほかの話題です。", "テラス席の別の文です。", "さらに別の話題です。"]}
    ((_, text),) = tools.search_chunks("テラス席", corpus=corpus)

    assert all(row in text for row in table)
    assert "テラス席の別の文です。" in text


def test_units_of_a_plain_string_are_unchanged():
    passages, spans = tools._units_of("一つ目です。\n二つ目です。\\三つ目です。")
    assert passages == ["一つ目です。", "二つ目です。", "三つ目です。"]
    assert spans == [(0, 0), (1, 1), (2, 2)]


def test_units_of_a_list_with_groups_and_blanks():
    passages, spans = tools._units_of(["文 A", ("行 1", "", "行 2"), "  ", "文 B"])
    assert passages == ["文 A", "行 1", "行 2", "文 B"]
    assert spans == [(0, 0), (1, 2), (1, 2), (3, 3)]


def _table_pdf_corpus(tmp_path):
    from tests.pdf_fixtures import Big, build_pdf

    corpus = tmp_path / "tbl"
    corpus.mkdir()
    rows = [("店舗名", "エリア", "座席数")] + [(f"店舗{n}号店", "港町", f"{n * 10}席") for n in range(1, 7)]
    (corpus / "guide.pdf").write_bytes(
        build_pdf([[Big("店舗一覧"), "", "店舗の表です。", "", *rows, "", "おわりの説明です。"]])
    )
    evidence = "\n".join(f'    - "{" ".join(row)}"' for row in rows)
    (corpus / "questions.yaml").write_text(
        textwrap.dedent(
            """
            - id: t1
              question: "店舗6号店の座席数は？"
              category: factual
              kind: aggregate
              evidence_mode: all
              expected_doc_ids: ["guide.pdf"]
              evidence:
            """
        )
        + evidence
        + "\n",
        encoding="utf-8",
    )
    return corpus


def test_passages_parent_returns_the_whole_table_and_sentence_does_not(tmp_path):
    corpus = _table_pdf_corpus(tmp_path)
    results = {}
    for mode in ("parent", "sentence"):
        out = tmp_path / f"{mode}.json"
        assert ev.main(["--corpus", str(corpus), "--passages", mode, "--k", "3", "--out", str(out)]) == 0
        report = json.loads(out.read_text(encoding="utf-8"))
        assert report["corpus"]["passages"] == mode
        results[mode] = report["results"][0]

    assert results["parent"]["keyword_hit"] is True
    assert results["sentence"]["keyword_hit"] is False
    assert results["sentence"]["failure_category"] == "keyword_miss"


def test_parent_is_the_default_for_a_pdf_corpus(tmp_path):
    corpus = _table_pdf_corpus(tmp_path)
    out = tmp_path / "r.json"
    assert ev.main(["--corpus", str(corpus), "--k", "3", "--out", str(out)]) == 0
    report = json.loads(out.read_text(encoding="utf-8"))
    assert report["corpus"]["passages"] == "parent"
    assert report["results"][0]["keyword_hit"] is True
