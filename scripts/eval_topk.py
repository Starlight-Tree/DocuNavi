"""検索の「件数（TOP_K）としきい値」の現状を数える（実験。アプリの検索は変えない）。

質問の埋め込みは、`eval_vector embed-queries --apply` が evals/cache/ に保存した分だけを使う。**API は呼ばない。**
DB は読み取りだけ（`scripts/eval_vector.py` の接続。読み取り専用の取引）。結果は evals/results/topk_probe.json に残す。

    python -m scripts.eval_topk            # 数えて表示し、evals/results/topk_probe.json に書く

数えるもの（全部、記述統計。方式や値を選ぶ測定ではない）:
- 上位 K 件（K = 1, 2, 3, 5。しきい値 MIN_SIMILARITY を適用）で、答えのある質問の根拠が返った数と、返ったチャンクのうち根拠を含むものの割合
- 上位 2 件で根拠が返らなかった質問の理由（しきい値で切れた / 順位が K より下 / 上位 10 件にも無い）
- しきい値を動かしたときの、答えのある質問が切れる数と、資料外の質問が LLM まで通る数
- 資料ごとのチャンクの数と長さ

質問は、選定用の質問と、最終確認の 52 問を合わせた全部。最終確認は方式の選定に 1 回使い終えているので、ここで値を選ぶのに使わない
（値を変える提案をするなら、新しい質問を先に固定して測る）。
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.agents.tools import TOP_K
from app.search_vector import MIN_SIMILARITY
from scripts.eval_retrieval import CORPORA_DIR, QUESTIONS_PATH, Question, load_questions
from scripts.eval_vector import (
    JST,
    ROOT,
    Chunk,
    EvalError,
    _load_cache,
    cached_vector,
    connect,
    corpus_fingerprint,
    gold_chunks,
    load_chunks,
    satisfied,
    search_vector,
)

OUT_PATH = ROOT / "evals" / "results" / "topk_probe.json"
FINAL_QUESTION_FILES = (
    ROOT / "evals" / "search_compare" / "final_questions_independent.yaml",
    ROOT / "evals" / "search_compare" / "final_questions_claude.yaml",
)
KS = (1, 2, 3, 5)
THRESHOLDS = (0.55, 0.60, 0.62, 0.64, 0.65, 0.66, 0.68, 0.70)
DEPTH = 10  # 順位を見る深さ（これより下は「上位 10 件にも無い」）
APP_TOP_K = TOP_K  # アプリが LLM に渡す件数

Ranked = list[tuple[Chunk, float]]
Row = tuple[Question, Ranked]


def _gold_ids(q: Question, chunks: Sequence[Chunk]) -> set[int]:
    return {cid for ids in gold_chunks(q, chunks).values() for cid in ids}


def _kept(ranked: Ranked, k: int, min_similarity: float) -> list[Chunk]:
    return [c for c, sim in ranked[:k] if sim >= min_similarity]


def sweep_k(
    rows: Sequence[Row], chunks: Sequence[Chunk], ks: Sequence[int], min_similarity: float
) -> list[dict[str, Any]]:
    """上位 K 件の中身。答えのある質問だけを数える。"""
    out = []
    for k in ks:
        answerable = returned = gold_returned = ok = empty = 0
        for q, ranked in rows:
            if not q.answerable:
                continue
            kept = _kept(ranked, k, min_similarity)
            gold = _gold_ids(q, chunks)
            answerable += 1
            ok += satisfied(q, kept)
            empty += not kept
            returned += len(kept)
            gold_returned += sum(c.id in gold for c in kept)
        out.append(
            {
                "k": k,
                "answerable": answerable,
                "satisfied": ok,
                "empty": empty,
                "returned_chunks": returned,
                "gold_chunks": gold_returned,
            }
        )
    return out


def miss_reasons(rows: Sequence[Row], k: int, min_similarity: float) -> list[dict[str, Any]]:
    """上位 k 件で根拠が返らなかった質問と、その理由。

    below_threshold: しきい値が無ければ返っていた / beyond_k: 上位 DEPTH 件には入る / not_in_top: 上位 DEPTH 件にも無い。
    """
    out = []
    for q, ranked in rows:
        if not q.answerable or satisfied(q, _kept(ranked, k, min_similarity)):
            continue
        if satisfied(q, _kept(ranked, k, float("-inf"))):
            reason = "below_threshold"
        elif satisfied(q, _kept(ranked, DEPTH, float("-inf"))):
            reason = "beyond_k"
        else:
            reason = "not_in_top"
        out.append(
            {
                "id": q.id,
                "reason": reason,
                "expected_docs": len(q.expected_doc_ids),
                "top1_similarity": round(ranked[0][1], 3) if ranked else None,
            }
        )
    return out


def sweep_threshold(rows: Sequence[Row], thresholds: Sequence[float]) -> list[dict[str, Any]]:
    """1 位の類似度でしきい値を動かしたとき、答えのある質問が切れる数と、資料外の質問が LLM まで通る数。"""
    top = [(q.answerable, ranked[0][1] if ranked else float("-inf")) for q, ranked in rows]
    answerable = [sim for ok, sim in top if ok]
    out_of_scope = [sim for ok, sim in top if not ok]
    return [
        {
            "threshold": t,
            "answerable": len(answerable),
            "answerable_cut": sum(sim < t for sim in answerable),
            "out_of_scope": len(out_of_scope),
            "out_of_scope_passed": sum(sim >= t for sim in out_of_scope),
        }
        for t in thresholds
    ]


def chunk_sizes(chunks: Sequence[Chunk]) -> dict[str, dict[str, int]]:
    by_source: dict[str, list[int]] = {}
    for c in chunks:
        by_source.setdefault(c.source, []).append(len(c.text))
    return {
        source: {"chunks": len(sizes), "min": min(sizes), "median": int(statistics.median(sizes)), "max": max(sizes)}
        for source, sizes in sorted(by_source.items())
    }


def load_probe_questions() -> list[Question]:
    paths = [*sorted(CORPORA_DIR.glob("*/questions.yaml")), QUESTIONS_PATH, *FINAL_QUESTION_FILES]
    questions = [q for path in paths for q in load_questions(path)]
    if len({q.id for q in questions}) != len(questions):
        raise EvalError("質問の id が重なっています")
    return questions


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def measure() -> dict[str, Any]:
    questions = load_probe_questions()
    conn = connect()
    try:
        chunks = load_chunks(conn)
        by_id = {c.id: c for c in chunks}
        cache = _load_cache()
        rows: list[Row] = [(q, search_vector(conn, cached_vector(cache, q), DEPTH, by_id)[0]) for q in questions]
    finally:
        conn.close()
    return {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "corpus_fingerprint": corpus_fingerprint(chunks),
        "min_similarity": MIN_SIMILARITY,
        "app_top_k": APP_TOP_K,
        "questions": len(questions),
        "answerable": sum(q.answerable for q, _ in rows),
        "k_sweep": sweep_k(rows, chunks, KS, MIN_SIMILARITY),
        "misses_at_app_top_k": miss_reasons(rows, APP_TOP_K, MIN_SIMILARITY),
        "threshold_sweep": sweep_threshold(rows, THRESHOLDS),
        "chunk_sizes": chunk_sizes(chunks),
    }


def print_report(report: dict[str, Any]) -> None:
    print(f"質問 {report['questions']}（答えのある {report['answerable']}）、しきい値 {report['min_similarity']}")
    for row in report["k_sweep"]:
        share = row["gold_chunks"] / row["returned_chunks"] if row["returned_chunks"] else 0.0
        print(
            f"K={row['k']}: 根拠が返った {row['satisfied']}/{row['answerable']}  何も返らない {row['empty']}  "
            f"返ったチャンク {row['returned_chunks']} のうち根拠を含む {row['gold_chunks']} ({share:.0%})"
        )
    for miss in report["misses_at_app_top_k"]:
        print(
            f"  外れ {miss['id']}: {miss['reason']}（期待ファイル {miss['expected_docs']}、1 位 {miss['top1_similarity']}）"
        )
    for row in report["threshold_sweep"]:
        print(
            f"しきい値 {row['threshold']:.2f}: 答えのある質問が切れる {row['answerable_cut']}/{row['answerable']}  "
            f"資料外が通る {row['out_of_scope_passed']}/{row['out_of_scope']}"
        )
    for source, size in report["chunk_sizes"].items():
        print(
            f"{source}: {size['chunks']} チャンク、文字数 最小/中央/最大 = {size['min']}/{size['median']}/{size['max']}"
        )


def main() -> int:
    try:
        report = measure()
    except EvalError as exc:
        print(f"評価を続けられません: {exc}", file=sys.stderr)
        return 2
    OUT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_report(report)
    print(f"-> {Path(OUT_PATH).relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
