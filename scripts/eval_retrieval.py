"""検索の評価（段階 0）。evals/questions.yaml の質問で現行の検索を採点し、JSON に残す。

LLM も DB も呼ばない。検索の実体は app/agents/tools.py の search_chunks（BM25）。
指標と結果の見方は evals/README.md。

    python -m scripts.eval_retrieval --mode bm25 --k 5
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from app.agents import tools

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "evals" / "questions.yaml"
RESULTS_DIR = ROOT / "evals" / "results"
JST = timezone(timedelta(hours=9))

OUT_OF_SCOPE = "out_of_scope"
ANSWERABLE_CATEGORIES = ("factual", "paraphrase", "multi_hop")
CATEGORIES = (*ANSWERABLE_CATEGORIES, OUT_OF_SCOPE)
SCOPE_KINDS = ("off_topic", "on_topic_absent")

# (質問, 上位何ファイルまで) -> [(ファイル名, 本文)]。モードを足すときはここに登録する
Search = Callable[[str, int], list[tuple[str, str]]]
MODES: dict[str, Search] = {
    "bm25": lambda query, k: tools.search_chunks(query, top_k=k),
}


class QuestionsError(ValueError):
    """評価セットの書式や、資料との食い違いのエラー。"""


@dataclass(frozen=True)
class Question:
    id: str
    question: str
    category: str
    expected_doc_ids: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()
    kind: str | None = None
    scope_kind: str | None = None

    @property
    def answerable(self) -> bool:
        return self.category != OUT_OF_SCOPE


def _string_tuple(item: dict[str, Any], key: str) -> tuple[str, ...]:
    value = item.get(key, [])
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise QuestionsError(f"{item.get('id')}: {key} は文字列のリストにしてください")
    return tuple(value)


def load_questions(path: Path = QUESTIONS_PATH) -> list[Question]:
    """YAML を読んで、書式を検査する（資料との突き合わせは check_against_corpus）。"""
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list) or not raw:
        raise QuestionsError("質問のリストが空です")

    questions: list[Question] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise QuestionsError(f"質問は辞書にしてください: {item!r}")
        qid, text, category = item.get("id"), item.get("question"), item.get("category")
        if not isinstance(qid, str) or not qid:
            raise QuestionsError(f"id がありません: {item!r}")
        if qid in seen:
            raise QuestionsError(f"{qid}: id が重複しています")
        seen.add(qid)
        if not isinstance(text, str) or not text.strip():
            raise QuestionsError(f"{qid}: question が空です")
        if category not in CATEGORIES:
            raise QuestionsError(f"{qid}: category は {CATEGORIES} のどれかにしてください")

        expected = _string_tuple(item, "expected_doc_ids")
        evidence = _string_tuple(item, "evidence")
        scope_kind = item.get("scope_kind")
        if category == OUT_OF_SCOPE:
            if expected or evidence:
                raise QuestionsError(f"{qid}: out_of_scope に expected_doc_ids / evidence は書けません")
            if scope_kind not in SCOPE_KINDS:
                raise QuestionsError(f"{qid}: scope_kind は {SCOPE_KINDS} のどれかにしてください")
        else:
            if not expected or not evidence:
                raise QuestionsError(f"{qid}: expected_doc_ids と evidence が必要です")
            if scope_kind is not None:
                raise QuestionsError(f"{qid}: scope_kind は out_of_scope だけに書けます")
        questions.append(
            Question(
                id=qid,
                question=text,
                category=category,
                expected_doc_ids=expected,
                evidence=evidence,
                kind=item.get("kind"),
                scope_kind=scope_kind,
            )
        )
    return questions


def check_against_corpus(questions: Sequence[Question], corpus: dict[str, str]) -> list[str]:
    """期待ファイル・根拠が資料に実在するかを検査する。資料を書き換えて評価が黙って無効になるのを防ぐ。"""
    problems: list[str] = []
    for q in questions:
        if not q.answerable:
            continue
        for name in q.expected_doc_ids:
            if name not in corpus:
                problems.append(f"{q.id}: 資料に {name} がありません")
            elif not any(ev in corpus[name] for ev in q.evidence):
                problems.append(f"{q.id}: {name} に根拠 {list(q.evidence)} が見つかりません")
        known = [corpus[n] for n in q.expected_doc_ids if n in corpus]
        for ev in q.evidence:
            if known and not any(ev in text for text in known):
                problems.append(f"{q.id}: 根拠 {ev!r} が期待ファイルのどれにもありません")
    return problems


def classify_failure(
    q: Question,
    retrieved_files: Sequence[str],
    doc_hit: bool,
    first_hit_rank: int | None,
    keyword_hit: bool,
) -> str | None:
    """失敗の種類。成功なら None。

    no_hit            何も返らなかった
    wrong_file        何か返ったが、期待ファイルが（全部は）入っていない
    wrong_rank        期待ファイルは返ったが、先頭ではない
    keyword_miss      期待ファイルは先頭だが、根拠の文が返っていない
    paraphrase_miss   paraphrase の質問での no_hit / wrong_file / keyword_miss
    unexpected_result out_of_scope なのに何か返った
    """
    if not q.answerable:
        return "unexpected_result" if retrieved_files else None
    miss = q.category == "paraphrase"
    if not retrieved_files:
        return "paraphrase_miss" if miss else "no_hit"
    if not doc_hit:
        return "paraphrase_miss" if miss else "wrong_file"
    if first_hit_rank != 1:
        return "wrong_rank"
    if not keyword_hit:
        return "paraphrase_miss" if miss else "keyword_miss"
    return None


def evaluate_question(q: Question, hits: list[tuple[str, str]]) -> dict[str, Any]:
    files = [name for name, _ in hits]
    record: dict[str, Any] = {
        "id": q.id,
        "question": q.question,
        "category": q.category,
    }
    if q.kind:
        record["kind"] = q.kind
    if q.scope_kind:
        record["scope_kind"] = q.scope_kind
    record["expected_doc_ids"] = list(q.expected_doc_ids)
    record["retrieved_files"] = files

    if not q.answerable:
        record.update(
            doc_hit=None,
            first_hit_rank=None,
            keyword_hits=[],
            keyword_hit=None,
            answered_empty=not files,
        )
    else:
        ranks = [files.index(name) + 1 for name in q.expected_doc_ids if name in files]
        doc_hit = len(ranks) == len(q.expected_doc_ids)
        first_hit_rank = min(ranks) if ranks else None
        text_by_file = dict(hits)
        found: list[str] = []
        satisfied: list[bool] = []
        for name in q.expected_doc_ids:
            matched = [ev for ev in q.evidence if ev in text_by_file.get(name, "")]
            satisfied.append(bool(matched))
            found.extend(ev for ev in matched if ev not in found)
        keyword_hit = all(satisfied)
        record.update(
            doc_hit=doc_hit,
            first_hit_rank=first_hit_rank,
            keyword_hits=found,
            keyword_hit=keyword_hit,
            answered_empty=None,
        )
    record["failure_category"] = classify_failure(
        q,
        files,
        bool(record["doc_hit"]),
        record["first_hit_rank"],
        bool(record["keyword_hit"]),
    )
    record["retrieved"] = [
        {"rank": rank, "file": name, "text": text} for rank, (name, text) in enumerate(hits, start=1)
    ]
    return record


def _rate(count: int, total: int) -> float | None:
    return round(count / total, 4) if total else None


def _answerable_stats(records: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(records)
    doc = sum(1 for r in records if r["doc_hit"])
    kw = sum(1 for r in records if r["keyword_hit"])
    rr = [1 / r["first_hit_rank"] if r["first_hit_rank"] else 0.0 for r in records]
    return {
        "n": n,
        "doc_hits": doc,
        "keyword_hits": kw,
        "doc_recall": _rate(doc, n),
        "keyword_hit_rate": _rate(kw, n),
        "mrr": round(sum(rr) / n, 4) if n else None,
    }


def summarize(records: list[dict[str, Any]], k: int) -> dict[str, Any]:
    answerable = [r for r in records if r["category"] != OUT_OF_SCOPE]
    out_of_scope = [r for r in records if r["category"] == OUT_OF_SCOPE]
    overall = _answerable_stats(answerable)
    empty = sum(1 for r in out_of_scope if r["answered_empty"])

    by_scope_kind: dict[str, Any] = {}
    for kind in SCOPE_KINDS:
        rows = [r for r in out_of_scope if r.get("scope_kind") == kind]
        if rows:
            n_empty = sum(1 for r in rows if r["answered_empty"])
            by_scope_kind[kind] = {"n": len(rows), "empty": n_empty, "accuracy": _rate(n_empty, len(rows))}

    by_category = {
        cat: _answerable_stats([r for r in answerable if r["category"] == cat])
        for cat in ANSWERABLE_CATEGORIES
        if any(r["category"] == cat for r in answerable)
    }
    failures: dict[str, int] = {}
    for r in records:
        if r["failure_category"]:
            failures[r["failure_category"]] = failures.get(r["failure_category"], 0) + 1

    return {
        "total_questions": len(records),
        "answerable_questions": len(answerable),
        "out_of_scope_questions": len(out_of_scope),
        f"doc_recall_at_{k}": overall["doc_recall"],
        f"keyword_hit_rate_at_{k}": overall["keyword_hit_rate"],
        "mrr": overall["mrr"],
        "empty_answer_accuracy": _rate(empty, len(out_of_scope)),
        "counts": {
            "doc_hit": overall["doc_hits"],
            "keyword_hit": overall["keyword_hits"],
            "answered_empty": empty,
        },
        "empty_answer_by_scope_kind": by_scope_kind,
        "by_category": by_category,
        "failure_counts": dict(sorted(failures.items())),
    }


def corpus_info(corpus: dict[str, str]) -> dict[str, Any]:
    digest = hashlib.sha256()
    for name in sorted(corpus):
        digest.update(name.encode("utf-8") + b"\0" + corpus[name].encode("utf-8") + b"\0")
    return {"files": sorted(corpus), "sha256": digest.hexdigest()}


def search_params() -> dict[str, Any]:
    """採点結果を左右する検索の定数。資料が増えて挙動が変わったときの突き合わせ用。"""
    names = (
        "TOP_K",
        "MAX_PASSAGES_PER_FILE",
        "CONTEXT_WINDOW",
        "RELATIVE_SCORE_CUTOFF",
        "GENERIC_TERM_RATIO",
        "MIN_SHARED_TERMS",
    )
    return {name: getattr(tools, name) for name in names}


def run(questions: Sequence[Question], mode: str, k: int) -> dict[str, Any]:
    search = MODES[mode]
    records = [evaluate_question(q, search(q.question, k)) for q in questions]
    return {
        "mode": mode,
        "k": k,
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "corpus": corpus_info(tools.load_chunks()),
        "search_params": search_params(),
        "summary": summarize(records, k),
        "results": records,
    }


def _display_path(path: Path) -> str:
    try:
        return str(path.resolve().relative_to(ROOT))
    except ValueError:
        return str(path)


def print_summary(report: dict[str, Any]) -> None:
    s, k = report["summary"], report["k"]
    c = s["counts"]
    print(f"mode={report['mode']} k={k} corpus={len(report['corpus']['files'])} files")
    print(f"questions: {s['total_questions']} (answerable {s['answerable_questions']}, out_of_scope {s['out_of_scope_questions']})")
    print(f"  doc_recall@{k}        {c['doc_hit']}/{s['answerable_questions']}  {s[f'doc_recall_at_{k}']}")
    print(f"  keyword_hit_rate@{k}  {c['keyword_hit']}/{s['answerable_questions']}  {s[f'keyword_hit_rate_at_{k}']}")
    print(f"  mrr                {s['mrr']}")
    print(f"  empty_answer_accuracy {c['answered_empty']}/{s['out_of_scope_questions']}  {s['empty_answer_accuracy']}")
    for kind, v in s["empty_answer_by_scope_kind"].items():
        print(f"    {kind}: {v['empty']}/{v['n']}")
    for cat, v in s["by_category"].items():
        print(f"  [{cat}] n={v['n']} doc_recall={v['doc_recall']} keyword={v['keyword_hit_rate']} mrr={v['mrr']}")
    failed = [r for r in report["results"] if r["failure_category"]]
    print(f"failures: {len(failed)}  {s['failure_counts']}")
    for r in failed:
        print(f"  {r['id']} [{r['category']}] {r['failure_category']}: {r['question']} -> {r['retrieved_files']}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="検索の評価（LLM・DB は使わない）")
    parser.add_argument("--mode", choices=sorted(MODES), default="bm25")
    parser.add_argument("--k", type=int, default=5, help="評価する上位ファイル数（既定 5）")
    parser.add_argument("--questions", type=Path, default=QUESTIONS_PATH)
    parser.add_argument("--out", type=Path, default=None, help="既定は evals/results/baseline_<mode>.json")
    args = parser.parse_args(argv)
    if args.k < 1:
        parser.error("--k は 1 以上にしてください")

    try:
        questions = load_questions(args.questions)
        problems = check_against_corpus(questions, tools.load_chunks())
    except (QuestionsError, OSError, yaml.YAMLError) as exc:
        print(f"評価セットを読めません: {exc}", file=sys.stderr)
        return 2
    if problems:
        print("評価セットが資料と合っていません:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    report = run(questions, args.mode, args.k)
    out = args.out or RESULTS_DIR / f"baseline_{args.mode}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_summary(report)
    print(f"-> {_display_path(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
