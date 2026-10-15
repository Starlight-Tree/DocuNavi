"""検索の評価（段階 0）。evals/questions.yaml の質問で現行の検索を採点し、JSON に残す。

LLM も DB も呼ばない。検索の実体は app/agents/tools.py の search_chunks（BM25）。
指標と結果の見方は evals/README.md。

    python -m scripts.eval_retrieval --mode bm25 --k 5                          # storage/chunk/（ザーハード）
    python -m scripts.eval_retrieval --mode bm25 --k 5 --corpus evals/corpora/cafe   # PDF の資料
    python -m scripts.eval_retrieval --mode bm25 --k 5 --app                    # アプリが検索する全資料（全部の質問）
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import yaml

from app.agents import tools
from app.ingestion.corpus import PASSAGES, load_pdf_corpus
from app.ingestion.service import find_pdfs, source_name

ROOT = Path(__file__).resolve().parents[1]
QUESTIONS_PATH = ROOT / "evals" / "questions.yaml"
RESULTS_DIR = ROOT / "evals" / "results"
CORPORA_DIR = ROOT / "evals" / "corpora"
JST = timezone(timedelta(hours=9))

OUT_OF_SCOPE = "out_of_scope"
ANSWERABLE_CATEGORIES = ("factual", "paraphrase", "multi_hop")
CATEGORIES = (*ANSWERABLE_CATEGORIES, OUT_OF_SCOPE)
SCOPE_KINDS = ("off_topic", "on_topic_absent")
EVIDENCE_MODES = ("any", "all")

# (質問, 上位何ファイルまで, 資料, 資料群) -> [(ファイル名, 本文)]。モードを足すときはここに登録する。
# 資料は (ファイル名 -> 本文) の 1 つの資料群。None なら storage/chunk/（ザーハード）。
# 資料群（名前 -> 資料）を渡すと、アプリの検索と同じに、資料群ごとに採点して合わせる
Search = Callable[..., list[tuple[str, str]]]


def _bm25(
    query: str,
    k: int,
    corpus: dict[str, Any] | None = None,
    collections: dict[str, dict[str, Any]] | None = None,
) -> list[tuple[str, str]]:
    if collections is not None:
        return tools.search_collections(query, k, collections)
    return tools.search_chunks(query, top_k=k, corpus=tools.load_chunks() if corpus is None else corpus)


MODES: dict[str, Search] = {"bm25": _bm25}


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
    evidence_mode: str = "any"  # any: 期待ファイルごとにどれか 1 つ / all: evidence の全部が要る（表の集計など）
    answer: str | None = None  # 正解の文（検索の評価では使わない。回答の評価のために持つ）
    page: int | None = None  # 根拠のページ（任意。取り込み後に抽出結果から確かめる）

    @property
    def answerable(self) -> bool:
        return self.category != OUT_OF_SCOPE


def _norm(text: str) -> str:
    """照合用。全角半角と空白の違い（PDF の改行・セルの区切りなど）を無視する。"""
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def _contains(haystack: str, needle: str) -> bool:
    return _norm(needle) in _norm(haystack)


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
        mode = item.get("evidence_mode", "any")
        if mode not in EVIDENCE_MODES:
            raise QuestionsError(f"{qid}: evidence_mode は {EVIDENCE_MODES} のどれかにしてください")
        answer = item.get("answer")
        if answer is not None and not isinstance(answer, str):
            raise QuestionsError(f"{qid}: answer は文字列にしてください")
        page = item.get("page")
        if page in ("", None):
            page = None
        elif not isinstance(page, int) or isinstance(page, bool) or page < 1:
            raise QuestionsError(f"{qid}: page は 1 以上の整数か、空にしてください")
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
                evidence_mode=mode,
                answer=answer,
                page=page,
            )
        )
    return questions


def _text_of(value: str | Sequence[Any]) -> str:
    if isinstance(value, str):
        return value
    return "\n".join(_text_of(item) for item in value)


def check_against_corpus(questions: Sequence[Question], corpus: dict[str, Any]) -> list[str]:
    """期待ファイル・根拠が資料に実在するかを検査する。資料を書き換えて評価が黙って無効になるのを防ぐ。"""
    problems: list[str] = []
    for q in questions:
        if not q.answerable:
            continue
        for name in q.expected_doc_ids:
            if name not in corpus:
                problems.append(f"{q.id}: 資料に {name} がありません")
            elif not any(_contains(_text_of(corpus[name]), ev) for ev in q.evidence):
                problems.append(f"{q.id}: {name} に根拠 {list(q.evidence)} が見つかりません")
        known = [_text_of(corpus[n]) for n in q.expected_doc_ids if n in corpus]
        for ev in q.evidence:
            if known and not any(_contains(text, ev) for text in known):
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
    if q.answerable and q.evidence_mode != "any":
        record["evidence_mode"] = q.evidence_mode
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
            matched = [ev for ev in q.evidence if _contains(text_by_file.get(name, ""), ev)]
            satisfied.append(bool(matched))
            found.extend(ev for ev in matched if ev not in found)
        if q.evidence_mode == "all":
            # 集計・比較の質問: 答えを確定する根拠が、全部返っていること（一部の行だけでは確定できない）
            keyword_hit = all(ev in found for ev in q.evidence)
        else:
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


def corpus_info(corpus: dict[str, Any]) -> dict[str, Any]:
    digest = hashlib.sha256()
    for name in sorted(corpus):
        digest.update(name.encode("utf-8") + b"\0" + _text_of(corpus[name]).encode("utf-8") + b"\0")
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


def collections_info(collections: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """資料群の一覧。files・sha256 は全体（資料群をまたいだ並びと内容）、collections は資料群ごとの内容のハッシュ。"""
    merged = {name: text for corpus in collections.values() for name, text in corpus.items()}
    info = corpus_info(merged)
    info["collections"] = {name: corpus_info(corpus) for name, corpus in collections.items()}
    return info


def run(
    questions: Sequence[Question],
    mode: str,
    k: int,
    corpus: dict[str, Any] | None = None,
    collections: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """corpus は 1 つの資料群（None なら storage/chunk/）。collections を渡すと、アプリと同じに資料群ごとに採点する。"""
    search = MODES[mode]
    records = [evaluate_question(q, search(q.question, k, corpus, collections)) for q in questions]
    if collections is not None:
        info = collections_info(collections)
    else:
        info = corpus_info(tools.load_chunks() if corpus is None else corpus)
    return {
        "mode": mode,
        "k": k,
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "corpus": info,
        "search_params": search_params(),
        "summary": summarize(records, k),
        "results": records,
    }


def locate_evidence(q: Question, pages_by_doc: dict[str, list[Any]]) -> dict[str, list[int]]:
    """各 evidence が、期待ファイルの何ページ目にあるか（抽出結果から。評価セットの page の推測に頼らない）。"""
    return {
        ev: sorted(
            {
                page.number
                for name in q.expected_doc_ids
                for page in pages_by_doc.get(name, [])
                if _contains(page.text, ev)
            }
        )
        for ev in q.evidence
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
    parser.add_argument(
        "--corpus",
        type=Path,
        default=None,
        help="PDF の資料のディレクトリ（既定の質問は <dir>/questions.yaml）。無ければ storage/chunk/",
    )
    parser.add_argument(
        "--passages",
        choices=PASSAGES,
        default="parent",
        help="--corpus のとき、表の返し方。parent（既定）は行ごとに採点して表全体を返す。sentence は表も 1 行ずつの文として返す",
    )
    parser.add_argument(
        "--app",
        action="store_true",
        help="アプリが検索する全資料（storage/chunk/ の .txt と storage/pdf/ の PDF）を、アプリと同じ検索で採点する。"
        "質問は evals/questions.yaml と evals/corpora/*/questions.yaml の全部",
    )
    parser.add_argument("--questions", type=Path, default=None, help="評価セット（既定 evals/questions.yaml）")
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="既定は evals/results/baseline_<mode>.json（--corpus 付きは baseline_<mode>_<ディレクトリ名>.json）",
    )
    args = parser.parse_args(argv)
    if args.k < 1:
        parser.error("--k は 1 以上にしてください")
    if args.app and args.corpus is not None:
        parser.error("--app と --corpus は同時に指定できません")

    corpus: dict[str, Any] | None = None
    collections: dict[str, dict[str, Any]] | None = None
    pages_by_doc: dict[str, list[Any]] = {}
    if args.corpus is not None:
        if not args.corpus.is_dir():
            print(f"--corpus がディレクトリではありません: {args.corpus.name}", file=sys.stderr)
            return 2
        corpus, pages_by_doc, failures = load_pdf_corpus(args.corpus, args.passages)
        if failures or not corpus:
            print("PDF を取り込めません:" if failures else "PDF がありません。", file=sys.stderr)
            for failure in failures:
                print(f"  {failure}", file=sys.stderr)
            return 2
    question_paths = [args.questions] if args.questions else [QUESTIONS_PATH]
    if args.app:
        collections = tools.load_collections()  # SEARCH_SCOPE に関わらず全部（評価が環境変数で動かないように）
        if args.questions is None:
            question_paths += sorted(CORPORA_DIR.glob("*/questions.yaml"))
    elif args.corpus is not None and args.questions is None:
        question_paths = [args.corpus / "questions.yaml"]

    try:
        questions = [q for path in question_paths for q in load_questions(path)]
        ids = [q.id for q in questions]
        duplicates = sorted({i for i in ids if ids.count(i) > 1})
        if duplicates:
            raise QuestionsError(f"評価セットをまたいで id が重なっています: {', '.join(duplicates)}")
        if collections is not None:
            searched = {name: text for coll in collections.values() for name, text in coll.items()}
        else:
            searched = tools.load_chunks() if corpus is None else corpus
        problems = check_against_corpus(questions, searched)
    except (QuestionsError, OSError, yaml.YAMLError) as exc:
        print(f"評価セットを読めません: {exc}", file=sys.stderr)
        return 2
    evidence_pages = {q.id: locate_evidence(q, pages_by_doc) for q in questions if q.answerable} if corpus else {}
    for q in questions:
        # 評価セットに page が書かれていれば、抽出結果と食い違わないことを確かめる
        if q.page is not None and corpus is not None:
            found = {n for pages in evidence_pages.get(q.id, {}).values() for n in pages}
            if q.page not in found:
                problems.append(f"{q.id}: page={q.page} と、抽出結果のページ {sorted(found)} が違います")
    if problems:
        print("評価セットが資料と合っていません:", file=sys.stderr)
        for problem in problems:
            print(f"  {problem}", file=sys.stderr)
        return 2

    report = run(questions, args.mode, args.k, corpus, collections)
    if collections is not None:
        report["corpus"]["source"] = "app"
        report["corpus"]["passages"] = "parent"
        report["corpus"]["pdf_sha256"] = {
            source_name(path, tools.PDF_DIR): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in find_pdfs(tools.PDF_DIR)
        }
        report["question_sets"] = [_display_path(path) for path in question_paths]
    elif corpus is not None:
        report["corpus"]["source"] = f"pdf:{args.corpus.name}"
        report["corpus"]["passages"] = args.passages
        report["corpus"]["pages"] = {name: len(pages) for name, pages in pages_by_doc.items()}
        # 入力の PDF そのもののハッシュ。チャンク化を変えても動かない（baseline との比較の可否に使う）
        report["corpus"]["pdf_sha256"] = {
            source_name(path, args.corpus): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in find_pdfs(args.corpus)
        }
        report["evidence_pages"] = evidence_pages
    suffix = "_app" if args.app else (f"_{args.corpus.name}" if args.corpus is not None else "")
    out = args.out or RESULTS_DIR / f"baseline_{args.mode}{suffix}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_summary(report)
    print(f"-> {_display_path(out)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
