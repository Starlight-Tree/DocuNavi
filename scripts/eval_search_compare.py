"""検索の一本化のための比較（実験。アプリの検索経路は変えない）。DB の chunks を唯一のデータ源にして、5 つの経路を同じ質問で測る。

    bm25            現行の BM25（アプリの採点を、チャンク単位の順位にしたもの。scripts/eval_vector.py と同じ）
    vector          pgvector のコサイン類似度
    pgroonga        pgroonga の全文検索（Groonga のスコア。BM25 そのものではない）。db/migrations/0004_pgroonga_chunks.sql が要る
    bm25_vector     bm25 と vector の RRF
    pgroonga_vector pgroonga と vector の RRF

DB は読み取りだけ（読み取り専用の取引）。API を呼ぶのは `embed-queries --apply` だけで、質問の埋め込みだけ（LLM は呼ばない）。
回答の質（LLM）は測らない。結果は evals/search_compare/ に JSON で残す。

    python -m scripts.eval_search_compare check                        # 質問・DB・pgroonga・埋め込みの有無を表示するだけ
    python -m scripts.eval_search_compare embed-queries                # 送る件数・概算費用を表示するだけ（API は呼ばない）
    python -m scripts.eval_search_compare embed-queries --apply        # 実 API。足りない質問の埋め込みを evals/cache/ に足す
    python -m scripts.eval_search_compare plan                         # 正解チャンクと設定を確定して evals/search_compare/plan.json に書く
    python -m scripts.eval_search_compare run                          # 選定用の質問（既存の評価セット）で 5 経路を測る
    python -m scripts.eval_search_compare final --questions PATH       # 最終確認用の新しい質問で、1 回だけ測る

守ること（結果を見る前に固定した決まり。plan.json に写す）:
- 正解チャンクは、DB のチャンク本文と質問の根拠（evidence）だけから決める。検索の結果は使わない。
- 経路ごとに設定を変えて試さない。pgroonga の質問の作り方（pgroonga_query）は下に固定してあり、最終確認の質問では変えない。
- 最終確認の質問（final）は、方式の選定に使わない。結果のファイルが既にあれば上書きしない。
- 「空の結果」は補助の指標で、答えなしの判定ではない（しきい値は別に決める。この比較には含めない）。
- API の上限: 追加で 80 件・10 回（1 回 8 件）。失敗・再実行も数える（cache に累計を残す）。SDK の再試行は無効。
- まだ実 DB・実 API では動かしていない。pgroonga の SQL（演算子・スコア・索引の使われ方）は、拡張を有効にした後の最初の `check` / `run` で確かめる。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import statistics
import sys
import time
import unicodedata
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.ingestion import embedding
from scripts.eval_retrieval import CORPORA_DIR, OUT_OF_SCOPE, QUESTIONS_PATH, Question, load_questions, search_params
from scripts.eval_vector import (
    BATCH,
    JST,
    KS,
    LIST_DEPTH,
    RRF_K,
    Chunk,
    EvalError,
    _entry_key,
    _git_head,
    _load_cache,
    _save_cache,
    bm25_collections,
    cached_vector,
    connect,
    corpus_fingerprint,
    first_gold_rank,
    gold_chunks,
    load_chunks,
    query_text_of,
    rrf,
    satisfied,
    search_bm25,
    search_vector,
)

ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = ROOT / "evals" / "search_compare"
PLAN_PATH = OUT_DIR / "plan.json"
RUN_PATH = OUT_DIR / "run.json"
FINAL_PATH = OUT_DIR / "final.json"

# --- 結果を見る前に固定した値（変えるときは plan を作り直す） ---
ARMS = ("bm25", "vector", "pgroonga", "bm25_vector", "pgroonga_vector")
MAX_NEW_TEXTS = 80  # 追加で送る質問の埋め込みの件数の上限（失敗・再実行を含む）
MAX_NEW_CALLS = 10

# pgroonga の質問の作り方。ひらがなを区切りにして、漢字・カタカナ・英数字の連なり（語の核）を取り出し、2 文字以上のものを OR でつなぐ。
# 現行 BM25 の _terms（app/agents/tools.py）が語の核を取る規則と同じ。連なりは Groonga が自分の分かち書き（TokenBigram）で照合する。
# 連なりは「文字・数字だけ」なので、問い合わせの構文（括弧・引用符・コロン・先頭の - など）は入らない。小文字にしてあるので、
# 構文の OR（大文字）と取り違えることもない。
_WORD_RUN = re.compile(r"[^\W_ぁ-ゟ]+")

_PGROONGA_SQL = (
    "SELECT c.id, extensions.pgroonga_score(c.tableoid, c.ctid) AS score FROM chunks c "
    "WHERE c.content OPERATOR(extensions.&@~) %s ORDER BY score DESC, c.id LIMIT %s"
)


def question_files() -> list[Path]:
    """選定用の質問（既存の評価セット）: 資料群ごとの評価セットと、ザーハードの評価セット。"""
    return [*sorted(CORPORA_DIR.glob("*/questions.yaml")), QUESTIONS_PATH]


def load_selection_questions() -> list[Question]:
    return load_with_check([q for path in question_files() for q in load_questions(path)])


def load_with_check(questions: list[Question]) -> list[Question]:
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise EvalError("質問の id が重なっています")
    return questions


def pgroonga_query(question: str) -> str | None:
    """質問から pgroonga の問い合わせ文字列を作る。使える語が無ければ None（その経路は「空」）。"""
    text = unicodedata.normalize("NFKC", question).lower()
    runs: list[str] = []
    for run in _WORD_RUN.findall(text):
        if len(run) >= 2 and run not in runs:
            runs.append(run)
    return " OR ".join(runs) if runs else None


def pgroonga_installed(conn: Any) -> bool:
    return conn.execute("SELECT 1 FROM pg_extension WHERE extname = 'pgroonga'").fetchone() is not None


def search_pgroonga(conn: Any, question: str, depth: int, by_id: dict[int, Chunk]) -> tuple[list[tuple[Chunk, float]], float]:
    """Groonga のスコアの高い順。小さい表では計画が順次走査になり、スコアが取れないことがあるので、この呼び出しの間だけ順次走査を避ける。"""
    query = pgroonga_query(question)
    if query is None:
        return [], 0.0
    started = time.perf_counter()
    conn.execute("SET LOCAL enable_seqscan = off")
    try:
        rows = conn.execute(_PGROONGA_SQL, (query, depth)).fetchall()
    finally:
        conn.execute("SET LOCAL enable_seqscan = on")
    return [(by_id[cid], float(score)) for cid, score in rows], time.perf_counter() - started


def fuse(rankings: dict[str, list[int]]) -> dict[str, list[int]]:
    """単独の 3 経路（bm25 / vector / pgroonga）の順位から、5 経路の順位（チャンク id の列）を作る。"""
    return {
        "bm25": rankings["bm25"],
        "vector": rankings["vector"],
        "pgroonga": rankings["pgroonga"],
        "bm25_vector": rrf([rankings["bm25"], rankings["vector"]]),
        "pgroonga_vector": rrf([rankings["pgroonga"], rankings["vector"]]),
    }


def score_question(q: Question, ranked: dict[str, list[int]], by_id: dict[int, Chunk]) -> dict[str, Any]:
    """1 問ぶんの採点（経路ごと）。空の結果かどうかは補助の指標。"""
    entry: dict[str, Any] = {"id": q.id, "question": q.question, "category": q.category, "scope_kind": q.scope_kind, "arms": {}}
    for arm in ARMS:
        ids = ranked[arm]
        chunks = [by_id[i] for i in ids]
        m: dict[str, Any] = {"empty": not ids, "top_ids": ids[: max(KS)]}
        if q.answerable:
            m["first_gold_rank"] = first_gold_rank(q, chunks)
            m["hit"] = {str(k): satisfied(q, chunks[:k]) for k in KS}
            m["hit_depth"] = satisfied(q, chunks)
        entry["arms"][arm] = m
    return entry


def summarize(entries: Sequence[dict[str, Any]]) -> dict[str, Any]:
    answerable = [e for e in entries if e["category"] != OUT_OF_SCOPE]
    absent = [e for e in entries if e["category"] == OUT_OF_SCOPE]
    summary: dict[str, Any] = {"n_answerable": len(answerable), "n_out_of_scope": len(absent)}
    for arm in ARMS:
        ms = [e["arms"][arm] for e in answerable]
        summary[arm] = {
            "mrr": round(sum(1 / m["first_gold_rank"] if m["first_gold_rank"] else 0.0 for m in ms) / len(ms), 4) if ms else None,
            "hit_at": {str(k): sum(m["hit"][str(k)] for m in ms) for k in KS},
            "hit_depth": sum(m["hit_depth"] for m in ms),
            "answerable_empty": sum(m["empty"] for m in ms),
            "out_of_scope_empty": sum(e["arms"][arm]["empty"] for e in absent),
        }
    return summary


def by_category(entries: Sequence[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """質問の種類（category）ごとの hit@3。どこで差が出たかを見るため。"""
    groups: dict[str, list[dict[str, Any]]] = {}
    for e in entries:
        if e["category"] != OUT_OF_SCOPE:
            groups.setdefault(e["category"], []).append(e)
    return {
        category: {"n": len(members), **{arm: sum(e["arms"][arm]["hit"]["3"] for e in members) for arm in ARMS}}
        for category, members in sorted(groups.items())
    }


# ---------------------------------------------------------------- 質問の埋め込み（API。上限つき・キャッシュつき）

def cmd_embed_queries(questions: list[Question], apply: bool) -> int:
    cache = _load_cache()
    todo = [q for q in questions if _entry_key(query_text_of(q)) not in cache["entries"]]
    chars = sum(len(query_text_of(q)) for q in todo)
    calls_needed = math.ceil(len(todo) / BATCH)
    print(f"質問 {len(questions)} 件、キャッシュ済み {len(questions) - len(todo)} 件、今回送る {len(todo)} 件（{chars} 文字、前置きを含む）")
    print(f"必要な呼び出し {calls_needed} 回（1 回 {BATCH} 件）。概算費用 約 ${chars * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000:.5f}（1 文字 1 トークンの推計）")
    print(f"今回の上限 {MAX_NEW_TEXTS} 件・{MAX_NEW_CALLS} 回（累計の送信は {cache['texts_sent']} 件・{cache['calls']} 回）")
    if not todo:
        print("送るものはありません。")
        return 0
    if len(todo) > MAX_NEW_TEXTS or calls_needed > MAX_NEW_CALLS:
        print("上限を超えるため送りません。", file=sys.stderr)
        return 2
    if not apply:
        print("（確認だけ。API は呼んでいません。--apply で送ります）")
        return 0

    from dotenv import find_dotenv, load_dotenv

    load_dotenv(find_dotenv(usecwd=True))
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key in ("", "your_api_key_here"):
        print("GEMINI_API_KEY が要ります（未設定です）。", file=sys.stderr)
        return 2
    embedder = embedding.GeminiEmbedder(api_key, embedding.ApiBudget(MAX_NEW_CALLS))
    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        texts = [query_text_of(q) for q in batch]
        # 送る前に数えて保存する（途中で落ちても、送った分は上限に数える）
        cache["texts_sent"] += len(texts)
        cache["calls"] += 1
        record: dict[str, Any] = {"n_texts": len(texts), "at": datetime.now(JST).isoformat(timespec="seconds"), "by": "eval_search_compare"}
        cache["log"].append(record)
        _save_cache(cache)
        began = time.perf_counter()
        try:
            vectors = embedder.embed(texts)
        except embedding.EmbeddingError as exc:
            record.update(status="error", error=str(exc), elapsed_s=round(time.perf_counter() - began, 3))
            _save_cache(cache)
            print(f"中断: {exc}", file=sys.stderr)
            return 1
        elapsed = time.perf_counter() - began
        record.update(status="ok", elapsed_s=round(elapsed, 3))
        for q, text, vector in zip(batch, texts, vectors, strict=True):
            cache["entries"][_entry_key(text)] = {"id": q.id, "vector": vector, "batch_elapsed_s": round(elapsed, 3), "batch_size": len(texts)}
        _save_cache(cache)
        print(f"  送信 {len(texts)} 件、{elapsed:.2f} 秒")
    print(f"完了。累計の送信 {cache['texts_sent']} 件・{cache['calls']} 回")
    return 0


# ---------------------------------------------------------------- check / plan / run / final

def cmd_check(questions: list[Question]) -> int:
    cache = _load_cache()
    missing = [q.id for q in questions if _entry_key(query_text_of(q)) not in cache["entries"]]
    with connect() as conn:
        chunks = load_chunks(conn)
        installed = pgroonga_installed(conn)
    print(f"質問 {len(questions)} 問（答えあり {sum(q.answerable for q in questions)}、資料外 {sum(not q.answerable for q in questions)}）")
    print(f"DB のチャンク {len(chunks)} 件、資料 {len({c.source for c in chunks})} 冊、埋め込み済み（すべて現在のモデル・本文と一致）")
    print(f"pgroonga: {'有効' if installed else '未有効（db/migrations/0004_pgroonga_chunks.sql が要る）'}")
    print(f"質問の埋め込み: キャッシュ済み {len(questions) - len(missing)} 件、足りない {len(missing)} 件" + (f" {missing}" if missing else ""))
    no_query = [q.id for q in questions if pgroonga_query(q.question) is None]
    print(f"pgroonga の問い合わせを作れない質問: {no_query or 'なし'}")
    return 0


def cmd_plan(questions: list[Question]) -> int:
    with connect() as conn:
        chunks = load_chunks(conn)
    gold: dict[str, Any] = {}
    problems: list[str] = []
    for q in questions:
        if not q.answerable:
            continue
        per_evidence = gold_chunks(q, chunks)
        if any(not ids for ids in per_evidence.values()):
            problems.append(f"{q.id}: 根拠のうち、どのチャンクにも無いものがあります {[e for e, ids in per_evidence.items() if not ids]}")
        gold[q.id] = {"evidence_mode": q.evidence_mode, "expected_doc_ids": list(q.expected_doc_ids), "gold_by_evidence": per_evidence}
    if problems:
        print("正解チャンクを決められない質問があります（止めます。評価セットと資料のずれ）:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 2
    plan = {
        "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        "note": "検索の結果を見る前に作った。正解チャンクは DB のチャンク本文と質問の根拠だけから決めた。",
        "fixed": {
            "arms": list(ARMS), "ks": list(KS), "list_depth": LIST_DEPTH, "rrf_k": RRF_K, "model": embedding.MODEL,
            "dimensions": embedding.DIMENSIONS, "pgroonga_query_rule": "語の核（ひらがな以外の連なり）のうち 2 文字以上を OR でつなぐ",
            "empty_is_supporting_metric": "空の結果は答えなしの判定ではない（しきい値はこの比較に含めない）",
        },
        "corpus_fingerprint": corpus_fingerprint(chunks),
        "chunk_count": len(chunks),
        "selection_questions": {q.id: {"category": q.category, "scope_kind": q.scope_kind} for q in questions},
        "gold": gold,
    }
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"チャンク {len(chunks)} 件、質問 {len(questions)} 問（答えあり {len(gold)}、資料外 {len(questions) - len(gold)}）")
    print(f"-> {PLAN_PATH.relative_to(ROOT)}")
    return 0


def measure(questions: list[Question], plan: dict[str, Any]) -> dict[str, Any]:
    cache = _load_cache()
    with connect() as conn:
        if not pgroonga_installed(conn):
            raise EvalError("pgroonga が有効ではありません（db/migrations/0004_pgroonga_chunks.sql を適用してください）")
        chunks = load_chunks(conn)
        if corpus_fingerprint(chunks) != plan["corpus_fingerprint"]:
            raise EvalError("DB のチャンクが plan を作ったときと違います（plan から作り直してください）")
        by_id = {c.id: c for c in chunks}
        by_key = {f"{c.source}#{c.chunk_index}": c for c in chunks}
        collections = bm25_collections(chunks)
        entries: list[dict[str, Any]] = []
        timing: dict[str, list[float]] = {"bm25": [], "vector": [], "pgroonga": []}
        for q in questions:
            bm_hits, t_bm = search_bm25(q, collections, by_key, LIST_DEPTH)
            vec_hits, t_vec = search_vector(conn, cached_vector(cache, q), LIST_DEPTH, by_id)
            pg_hits, t_pg = search_pgroonga(conn, q.question, LIST_DEPTH, by_id)
            timing["bm25"].append(t_bm)
            timing["vector"].append(t_vec)
            timing["pgroonga"].append(t_pg)
            ranked = fuse({"bm25": [c.id for c, _ in bm_hits], "vector": [c.id for c, _ in vec_hits], "pgroonga": [c.id for c, _ in pg_hits]})
            entries.append(score_question(q, ranked, by_id))
    return {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "plan_sha256": hashlib.sha256(PLAN_PATH.read_bytes()).hexdigest(),
        "corpus_fingerprint": plan["corpus_fingerprint"],
        "arms": list(ARMS), "ks": list(KS), "list_depth": LIST_DEPTH, "rrf_k": RRF_K,
        "bm25_params": search_params(),
        "summary": summarize(entries),
        "by_category_hit_at_3": by_category(entries),
        "latency_ms_median": {k: round(statistics.median(v) * 1000, 2) if v else None for k, v in timing.items()},
        "results": entries,
    }


def print_report(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(f"答えあり {s['n_answerable']} 問 / 資料外 {s['n_out_of_scope']} 問")
    print(f"{'経路':16} {'MRR':>6} " + " ".join(f"hit@{k}" for k in KS) + " | 答えありで空 | 資料外で空")
    for arm in ARMS:
        m = s[arm]
        print(f"{arm:16} {m['mrr']:>6} " + " ".join(f"{m['hit_at'][str(k)]:>5}" for k in KS) + f" | {m['answerable_empty']:>10} | {m['out_of_scope_empty']:>9}")
    print("質問の種類ごとの hit@3:", report["by_category_hit_at_3"])
    print("中央値の検索時間(ms):", report["latency_ms_median"])


def cmd_run() -> int:
    if not PLAN_PATH.exists():
        print("先に plan を実行してください。", file=sys.stderr)
        return 2
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    report = measure(load_selection_questions(), plan)
    report["set"] = "selection"
    RUN_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_report(report)
    print(f"-> {RUN_PATH.relative_to(ROOT)}")
    return 0


def cmd_final(path: Path, force: bool) -> int:
    if not PLAN_PATH.exists():
        print("先に plan を実行してください。", file=sys.stderr)
        return 2
    if FINAL_PATH.exists() and not force:
        print(f"{FINAL_PATH.relative_to(ROOT)} が既にあります。最終確認は 1 回だけ測るので、上書きしません。", file=sys.stderr)
        return 2
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    questions = load_with_check(load_questions(path))
    selection_ids = set(plan["selection_questions"])
    if overlap := sorted(selection_ids & {q.id for q in questions}):
        raise EvalError(f"最終確認の質問が選定用の質問と重なっています: {overlap}")
    # 最終確認の質問の正解チャンクも、検索の前に DB の本文と根拠だけから決める
    with connect() as conn:
        chunks = load_chunks(conn)
    for q in questions:
        if q.answerable and any(not ids for ids in gold_chunks(q, chunks).values()):
            raise EvalError(f"{q.id}: 根拠のうち、どのチャンクにも無いものがあります")
    report = measure(questions, plan)
    report["set"] = "final"
    report["questions_file"] = {"path": path.as_posix(), "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    FINAL_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_report(report)
    print(f"-> {FINAL_PATH.relative_to(ROOT)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="検索の一本化のための比較（DB は読み取りだけ。API は embed-queries --apply だけ）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("check")
    sub.add_parser("plan")
    sub.add_parser("run")
    emb = sub.add_parser("embed-queries")
    emb.add_argument("--apply", action="store_true")
    emb.add_argument("--questions", type=Path, help="最終確認用の質問の YAML（省略すると選定用の質問）")
    fin = sub.add_parser("final")
    fin.add_argument("--questions", type=Path, required=True)
    fin.add_argument("--force", action="store_true", help="既にある final.json を上書きする（通常は使わない）")
    args = parser.parse_args(argv)
    try:
        if args.command == "check":
            return cmd_check(load_selection_questions())
        if args.command == "plan":
            return cmd_plan(load_selection_questions())
        if args.command == "run":
            return cmd_run()
        if args.command == "embed-queries":
            questions = load_selection_questions()
            if args.questions:
                questions = questions + load_with_check(load_questions(args.questions))
            return cmd_embed_queries(load_with_check(questions), args.apply)
        return cmd_final(args.questions, args.force)
    except EvalError as exc:
        print(f"評価を続けられません: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
