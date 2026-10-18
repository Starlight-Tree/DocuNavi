"""ベクトル検索の評価（実験。アプリの検索経路は変えない）。同じ 68 チャンク・同じ質問で、BM25・ベクトル・ハイブリッドを比べる。

DB は読み取りだけ（読み取り専用の取引。データ・スキーマ・権限は変えない）。API を呼ぶのは `embed-queries --apply` だけで、
質問の埋め込み（LLM は呼ばない）。結果は evals/results/ と evals/vector/ に JSON で残す。手順と結果の読み方は evals/README.md。

    python -m scripts.eval_vector plan                      # 分割（調整用 / held-out）と正解チャンクを確定して evals/vector/plan.json に書く
    python -m scripts.eval_vector embed-queries             # 何を送るか・件数・概算費用を表示するだけ（API は呼ばない）
    python -m scripts.eval_vector embed-queries --apply     # 実 API。質問の埋め込みを evals/cache/ に保存する（Git 管理外）
    python -m scripts.eval_vector dev                       # 調整用の質問だけで 3 方式を測り、しきい値を決める -> evals/results/vector_dev.json
    python -m scripts.eval_vector final                     # 決めた方式・しきい値のまま held-out を 1 回だけ測る -> evals/results/vector_final.json

守ること（結果を見る前に固定した決まり。plan.json に写す）:
- 正解チャンクは、DB のチャンク本文と質問の根拠（evidence）だけから決める。検索の結果は使わない。
- held-out は、方式・重み・しきい値の調整に使わない。`final` は `dev` が書いた vector_dev.json のしきい値をそのまま使い、
  vector_final.json が既にあれば（既定では）上書きしない。
- API の上限: 送信 40 件・呼び出し 5 回（1 回 8 件）。失敗・再実行も数える（cache に累計を残す）。SDK の再試行は無効。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from scripts import bm25_baseline as tools
from app.ingestion import embedding
from app.ingestion.chunker import chunk_pages
from app.ingestion.pdf_parser import extract_pages
from scripts.eval_retrieval import (
    CORPORA_DIR,
    OUT_OF_SCOPE,
    Question,
    _contains,
    load_questions,
    search_params,
)

ROOT = Path(__file__).resolve().parents[1]
PLAN_PATH = ROOT / "evals" / "vector" / "plan.json"
CACHE_PATH = ROOT / "evals" / "cache" / "query_embeddings.json"
RESULTS_DIR = ROOT / "evals" / "results"
DEV_PATH = RESULTS_DIR / "vector_dev.json"
FINAL_PATH = RESULTS_DIR / "vector_final.json"
JST = timezone(timedelta(hours=9))

# --- 結果を見る前に固定した値（変えるときは plan を作り直し、dev からやり直す） ---
SPLIT_SALT = "vector-eval-v1"
HELD_OUT_RATIO = 0.4  # 層（期待ファイル / 資料外）ごとに round(n × 0.4) 件を held-out にする。選ぶ順は sha256(salt:id)
KS = (1, 3, 5)  # 上位何チャンクまで見るか
LIST_DEPTH = 20  # 順位（MRR）とハイブリッドの融合に使う各方式の上位の深さ
RRF_K = 60  # Reciprocal Rank Fusion の定数（一般的な既定値。調整しない）
METHODS = ("bm25", "vector", "hybrid")

MAX_TEXTS = 40  # 質問の埋め込みを送る件数の上限（失敗・再実行を含む累計）
MAX_CALLS = 5  # 呼び出し回数の上限（8 件ずつ 4 回 + 失敗した 1 回分の余裕）
BATCH = 8


class EvalError(RuntimeError):
    """評価の前提が崩れている（資料と DB の食い違いなど）。黙って続けず止める。"""


# ---------------------------------------------------------------- 質問・分割・正解

@dataclass(frozen=True)
class Chunk:
    id: int
    source: str
    chunk_index: int
    heading: str | None
    content: str
    kind: str
    content_hash: str
    input_hash: str  # 保存時の embedding_input_hash

    @property
    def text(self) -> str:
        """正解の判定に使う本文（見出しを含む）。"""
        return f"{self.heading}\n{self.content}" if self.heading else self.content


def load_all_questions() -> list[Question]:
    questions = [q for path in sorted(CORPORA_DIR.glob("*/questions.yaml")) for q in load_questions(path)]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise EvalError("質問の id が重なっています")
    return questions


def _split_key(question_id: str) -> str:
    return hashlib.sha256(f"{SPLIT_SALT}:{question_id}".encode()).hexdigest()


def stratum_of(q: Question) -> str:
    return OUT_OF_SCOPE if not q.answerable else q.expected_doc_ids[0]


def split_questions(questions: Sequence[Question]) -> dict[str, str]:
    """質問 id -> "dev" / "held_out"。層（期待ファイル、資料外）ごとに、ハッシュ順の先頭 round(n × 0.4) 件が held_out。"""
    strata: dict[str, list[Question]] = {}
    for q in questions:
        strata.setdefault(stratum_of(q), []).append(q)
    assignment: dict[str, str] = {}
    for members in strata.values():
        ordered = sorted(members, key=lambda q: _split_key(q.id))
        held = round(len(ordered) * HELD_OUT_RATIO)
        for position, q in enumerate(ordered):
            assignment[q.id] = "held_out" if position < held else "dev"
    return assignment


def gold_chunks(q: Question, chunks: Sequence[Chunk]) -> dict[str, list[int]]:
    """根拠の文ごとに、それを含む（期待ファイルの）チャンク id。検索の結果は使わない。"""
    return {
        ev: [c.id for c in chunks if c.source in q.expected_doc_ids and _contains(c.text, ev)]
        for ev in q.evidence
    }


def satisfied(q: Question, retrieved: Sequence[Chunk]) -> bool:
    """既存の採点（scripts/eval_retrieval.py の keyword_hit）と同じ意味を、チャンク単位で。

    any: 期待ファイルごとに、根拠のどれか 1 つが返ったチャンクにある。 all: 根拠の全部が、返ったチャンクのどれかにある。
    """
    if not q.answerable:
        return False
    mine = [c for c in retrieved if c.source in q.expected_doc_ids]
    if q.evidence_mode == "all":
        return all(any(_contains(c.text, ev) for c in mine) for ev in q.evidence)
    return all(
        any(c.source == name and _contains(c.text, ev) for c in mine for ev in q.evidence) for name in q.expected_doc_ids
    )


def first_gold_rank(q: Question, ranked: Sequence[Chunk]) -> int | None:
    """根拠を 1 つでも含む期待ファイルのチャンクが最初に現れる順位（1 始まり）。無ければ None。"""
    for rank, c in enumerate(ranked, start=1):
        if c.source in q.expected_doc_ids and any(_contains(c.text, ev) for ev in q.evidence):
            return rank
    return None


# ---------------------------------------------------------------- DB（読み取りだけ）

def connect() -> Any:
    from dotenv import find_dotenv, load_dotenv

    from app.history import database_url

    load_dotenv(find_dotenv(usecwd=True))
    url = database_url()
    if not url:
        raise EvalError("DATABASE_URL が要ります（未設定です）")
    import psycopg

    try:
        conn = psycopg.connect(url, prepare_threshold=None, connect_timeout=10)
    except psycopg.Error as exc:
        raise EvalError(f"DB に接続できません（{type(exc).__name__}）") from None  # 例外の文字列には接続先が入りうる
    conn.read_only = True  # この接続では書き込めない（取引ごとに READ ONLY で始まる）
    return conn


def load_chunks(conn: Any) -> list[Chunk]:
    rows = conn.execute(
        "SELECT c.id, d.source, c.chunk_index, c.heading, c.content, c.kind, c.content_hash, c.embedding_input_hash, "
        "c.embedding IS NOT NULL, c.embedding_model, c.embedding_dimensions "
        "FROM chunks c JOIN documents d ON d.id = c.document_id ORDER BY d.source, c.chunk_index"
    ).fetchall()
    chunks: list[Chunk] = []
    for cid, source, index, heading, content, kind, digest, stored_input, has_vector, model, dims in rows:
        chunk = Chunk(cid, source, index, heading, content, kind, digest, stored_input or "")
        current = embedding.input_hash(heading, content)
        if not has_vector or model != embedding.MODEL or dims != embedding.DIMENSIONS or stored_input != current:
            raise EvalError(f"チャンク {cid} の埋め込みが無い、または現在の本文・モデルと合っていません")
        chunks.append(chunk)
    if not chunks:
        raise EvalError("チャンクがありません")
    return chunks


def corpus_fingerprint(chunks: Sequence[Chunk]) -> str:
    digest = hashlib.sha256()
    for c in chunks:
        digest.update(f"{c.id}\0{c.source}\0{c.chunk_index}\0{c.content_hash}\0{c.input_hash}\n".encode())
    return digest.hexdigest()


def pdf_collections() -> dict[str, dict[str, Path]]:
    """storage/pdf/<資料群>/ -> {ファイル名: パス}（アプリが検索する PDF）。"""
    found: dict[str, dict[str, Path]] = {}
    for directory in sorted(p for p in tools.PDF_DIR.iterdir() if p.is_dir()):
        files = {p.name: p for p in sorted(directory.glob("*.pdf"))}
        if files:
            found[directory.name] = files
    return found


def bm25_collections(chunks: Sequence[Chunk]) -> dict[str, dict[str, list[Any]]]:
    """アプリの検索（app/ingestion/corpus.py の load_pdf_corpus）と同じ単位の列を、チャンクを鍵にして作る。

    採点する文の集合は、アプリと同じ（チャンク化と見出しの入れ方が同じ）。違うのは、順位をつける単位がファイルでなく
    チャンクになる点だけ（鍵 "<ファイル名>#<chunk_index>"）。DB のチャンクが PDF から作り直したものと一致することも確かめる。
    """
    by_key = {(c.source, c.chunk_index): c for c in chunks}
    used: set[tuple[str, int]] = set()
    collections: dict[str, dict[str, list[Any]]] = {}
    for name, files in pdf_collections().items():
        corpus: dict[str, list[Any]] = {}
        for filename, path in files.items():
            last_heading: str | None = None
            for chunk in chunk_pages(extract_pages(path)):
                stored = by_key.get((filename, chunk.index))
                if stored is None or stored.content_hash != chunk.content_hash:
                    raise EvalError(f"{filename} のチャンク {chunk.index} が DB と一致しません（取り込み直しが要ります）")
                used.add((filename, chunk.index))
                units: list[Any] = []
                if chunk.heading and chunk.heading != last_heading:
                    units.append(chunk.heading)
                last_heading = chunk.heading
                lines = chunk.text.split("\n")
                if chunk.kind == "table":
                    units.append(tuple(lines))
                else:
                    units.extend(lines)
                corpus[f"{filename}#{chunk.index}"] = units
        collections[name] = corpus
    if used != set(by_key):
        raise EvalError("DB にあって PDF から作れないチャンクがあります")
    return collections


# ---------------------------------------------------------------- 検索 3 方式

def query_text_of(q: Question) -> str:
    return embedding.query_text(q.question)


def search_bm25(
    q: Question, collections: dict[str, dict[str, list[Any]]], by_key: dict[str, Chunk], depth: int
) -> tuple[list[tuple[Chunk, str]], float]:
    """アプリの採点で、チャンクを順位づけして返す（(チャンク, 返る本文)）。何も当たらなければ空。"""
    started = time.perf_counter()
    hits = tools.search_collections(q.question, depth, collections)
    elapsed = time.perf_counter() - started
    return [(by_key[key], text) for key, text in hits], elapsed


def search_vector(conn: Any, vector: Sequence[float], depth: int, by_id: dict[int, Chunk]) -> tuple[list[tuple[Chunk, float]], float]:
    """コサイン類似度の高い順。pgvector の `<=>`（コサイン距離）を、スキーマ付きで使う（アプリの検索と同じ書き方）。"""
    literal = embedding.vector_literal(vector)
    started = time.perf_counter()
    rows = conn.execute(
        "SELECT id, 1 - (embedding OPERATOR(extensions.<=>) %s::extensions.vector(768)) AS sim FROM chunks "
        "WHERE embedding IS NOT NULL ORDER BY embedding OPERATOR(extensions.<=>) %s::extensions.vector(768), id LIMIT %s",
        (literal, literal, depth),
    ).fetchall()
    elapsed = time.perf_counter() - started
    return [(by_id[cid], float(sim)) for cid, sim in rows], elapsed


def rrf(rankings: Sequence[Sequence[int]], *, k: int = RRF_K) -> list[int]:
    """Reciprocal Rank Fusion。各ランキング（チャンク id の順）の 1/(k+順位) を足し、同点は id の小さい方。"""
    score: dict[int, float] = {}
    for ranking in rankings:
        for rank, cid in enumerate(ranking, start=1):
            score[cid] = score.get(cid, 0.0) + 1.0 / (k + rank)
    return sorted(score, key=lambda cid: (-score[cid], cid))


def choose_tau(items: Sequence[tuple[bool, bool, float]]) -> tuple[float, list[dict[str, Any]]]:
    """しきい値の決め方（結果を見る前に固定）。

    items: (答えがある質問か, BM25 が何か返したか, ベクトルの 1 位の類似度)。しきい値より類似度が低く、かつ BM25 も空なら「答えなし」。
    （ベクトル単独の方式は BM25 を使わないので、呼び出し側が BM25 の項を False にして渡す。）
    候補は「一度も答えなしにしない」と、隣り合う類似度の中点。目的は、答えのある質問を答えなしにしなかった率と、資料外を答えなしに
    できた率の平均（balanced accuracy）の最大。同点なら小さいしきい値（答える側）。
    """
    sims = sorted({s for _, _, s in items})
    candidates = [-math.inf, *((a + b) / 2 for a, b in zip(sims, sims[1:], strict=False)), math.inf]
    answerable = [i for i in items if i[0]]
    absent = [i for i in items if not i[0]]
    curve: list[dict[str, Any]] = []
    for tau in candidates:
        kept = sum(1 for _, nonempty, s in answerable if nonempty or s >= tau)
        refused = sum(1 for _, nonempty, s in absent if not nonempty and s < tau)
        tpr = kept / len(answerable) if answerable else 0.0
        tnr = refused / len(absent) if absent else 0.0
        curve.append({"tau": None if math.isinf(tau) else round(tau, 6), "inf": "-inf" if tau == -math.inf else ("+inf" if tau == math.inf else None), "answerable_kept": kept, "absent_refused": refused, "balanced_accuracy": round((tpr + tnr) / 2, 4)})
    best = max(range(len(candidates)), key=lambda i: (curve[i]["balanced_accuracy"], -i))
    return candidates[best], curve


# ---------------------------------------------------------------- 質問の埋め込み（API。上限つき・キャッシュつき）

def _load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("model") != embedding.MODEL or data.get("dimensions") != embedding.DIMENSIONS:
            raise EvalError("キャッシュのモデル・次元が現在と違います（別の場所に退避してください）")
        return data
    return {"model": embedding.MODEL, "dimensions": embedding.DIMENSIONS, "texts_sent": 0, "calls": 0, "log": [], "entries": {}}


def _save_cache(data: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _entry_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cached_vector(cache: dict[str, Any], q: Question) -> list[float]:
    entry = cache["entries"].get(_entry_key(query_text_of(q)))
    if entry is None:
        raise EvalError(f"{q.id} の質問の埋め込みがキャッシュにありません（embed-queries --apply が要ります）")
    return entry["vector"]


def cmd_embed_queries(apply: bool) -> int:
    questions = load_all_questions()
    cache = _load_cache()
    todo = [q for q in questions if _entry_key(query_text_of(q)) not in cache["entries"]]
    chars = sum(len(query_text_of(q)) for q in todo)
    calls_needed = math.ceil(len(todo) / BATCH)
    print(f"質問 {len(questions)} 件、キャッシュ済み {len(questions) - len(todo)} 件、今回送る {len(todo)} 件（{chars} 文字、前置きを含む）")
    print(f"必要な呼び出し {calls_needed} 回（1 回 {BATCH} 件）。概算費用 約 ${chars * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000:.5f}（1 文字 1 トークンの推計）")
    print(f"累計の送信 {cache['texts_sent']} 件・{cache['calls']} 回（上限 {MAX_TEXTS} 件・{MAX_CALLS} 回）")
    if not todo:
        print("送るものはありません。")
        return 0
    if cache["texts_sent"] + len(todo) > MAX_TEXTS or cache["calls"] + calls_needed > MAX_CALLS:
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
    embedder = embedding.GeminiEmbedder(api_key, embedding.ApiBudget(MAX_CALLS - cache["calls"]))
    for start in range(0, len(todo), BATCH):
        batch = todo[start : start + BATCH]
        texts = [query_text_of(q) for q in batch]
        # 送る前に数えて保存する（途中で落ちても、送った分は上限に数える）
        cache["texts_sent"] += len(texts)
        cache["calls"] += 1
        record: dict[str, Any] = {"n_texts": len(texts), "at": datetime.now(JST).isoformat(timespec="seconds")}
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


# ---------------------------------------------------------------- plan

def cmd_plan() -> int:
    questions = load_all_questions()
    with connect() as conn:
        chunks = load_chunks(conn)
    assignment = split_questions(questions)
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
            "split_salt": SPLIT_SALT, "held_out_ratio": HELD_OUT_RATIO, "ks": list(KS), "list_depth": LIST_DEPTH,
            "rrf_k": RRF_K, "methods": list(METHODS), "model": embedding.MODEL, "dimensions": embedding.DIMENSIONS,
            "tau_rule": "しきい値は dev だけで、balanced accuracy 最大（同点は小さい方）。held-out では変えない",
            "hybrid_abstain_rule": "BM25 が空、かつ ベクトルの 1 位の類似度がしきい値未満のとき答えなし",
        },
        "corpus_fingerprint": corpus_fingerprint(chunks),
        "chunk_count": len(chunks),
        "questions": {q.id: {"split": assignment[q.id], "stratum": stratum_of(q), "category": q.category, "scope_kind": q.scope_kind} for q in questions},
        "gold": gold,
    }
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    counts: dict[str, dict[str, int]] = {}
    for q in questions:
        counts.setdefault(stratum_of(q), {"dev": 0, "held_out": 0})[assignment[q.id]] += 1
    print(f"チャンク {len(chunks)} 件、質問 {len(questions)} 問（答えあり {len(gold)}、資料外 {len(questions) - len(gold)}）")
    for stratum, c in sorted(counts.items()):
        print(f"  {stratum}: dev {c['dev']} / held_out {c['held_out']}")
    print(f"  合計: dev {sum(1 for v in assignment.values() if v == 'dev')} / held_out {sum(1 for v in assignment.values() if v == 'held_out')}")
    print("正解チャンクの数（質問ごと）:", {qid: {e[:12]: len(ids) for e, ids in g['gold_by_evidence'].items()} for qid, g in gold.items()})
    print(f"-> {PLAN_PATH.relative_to(ROOT)}")
    return 0


# ---------------------------------------------------------------- dev / final

def _git_head() -> str:
    try:
        return subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _median_ms(values: Sequence[float]) -> float | None:
    return round(statistics.median(values) * 1000, 2) if values else None


def run_split(split: str, taus: dict[str, float] | None) -> dict[str, Any]:
    """split の質問だけで 3 方式を測る。taus が None なら、ここで（dev のデータから）決める。"""
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    questions = [q for q in load_all_questions() if plan["questions"][q.id]["split"] == split]
    cache = _load_cache()
    with connect() as conn:
        chunks = load_chunks(conn)
        if corpus_fingerprint(chunks) != plan["corpus_fingerprint"]:
            raise EvalError("DB のチャンクが plan を作ったときと違います（plan から作り直してください）")
        by_id = {c.id: c for c in chunks}
        by_key = {f"{c.source}#{c.chunk_index}": c for c in chunks}
        collections = bm25_collections(chunks)
        rows: list[dict[str, Any]] = []
        for q in questions:
            bm_hits, t_bm = search_bm25(q, collections, by_key, LIST_DEPTH)
            vec_hits, t_vec = search_vector(conn, cached_vector(cache, q), LIST_DEPTH, by_id)
            bm_ids = [c.id for c, _ in bm_hits]
            vec_ids = [c.id for c, _ in vec_hits]
            hy_ids = rrf([bm_ids, vec_ids])
            ranked = {"bm25": bm_ids, "vector": vec_ids, "hybrid": hy_ids}
            rows.append({
                "q": q, "ranked": ranked, "bm25_text": {c.id: text for c, text in bm_hits},
                "top1_sim": vec_hits[0][1] if vec_hits else 0.0, "sims": {c.id: s for c, s in vec_hits},
                "bm25_nonempty": bool(bm_ids), "t_bm25": t_bm, "t_vector": t_vec,
            })

    if taus is None:
        vec_tau, vec_curve = choose_tau([(r["q"].answerable, False, r["top1_sim"]) for r in rows])
        hyb_tau, hyb_curve = choose_tau([(r["q"].answerable, r["bm25_nonempty"], r["top1_sim"]) for r in rows])
        # 保存する桁（6 桁）に丸めてから使う。dev と final で、同じ値で判定するため
        taus = {k: (v if math.isinf(v) else round(v, 6)) for k, v in (("vector", vec_tau), ("hybrid", hyb_tau))}
        curves: dict[str, Any] | None = {"vector": vec_curve, "hybrid": hyb_curve}
    else:
        curves = None

    def abstains(method: str, r: dict[str, Any]) -> bool:
        if method == "bm25":
            return not r["bm25_nonempty"]
        if method == "vector":
            return r["top1_sim"] < taus["vector"]
        return (not r["bm25_nonempty"]) and r["top1_sim"] < taus["hybrid"]

    per_question: list[dict[str, Any]] = []
    for r in rows:
        q: Question = r["q"]
        entry: dict[str, Any] = {"id": q.id, "question": q.question, "category": q.category, "scope_kind": q.scope_kind,
                                 "top1_sim": round(r["top1_sim"], 4), "bm25_nonempty": r["bm25_nonempty"], "methods": {}}
        for method in METHODS:
            ids = r["ranked"][method]
            ranked_chunks = [by_id[i] for i in ids]
            m: dict[str, Any] = {"abstained": abstains(method, r), "top_ids": ids[:max(KS)]}
            if q.answerable:
                m["first_gold_rank"] = first_gold_rank(q, ranked_chunks)
                m["hit"] = {str(k): satisfied(q, ranked_chunks[:k]) for k in KS}
                m["hit_depth"] = satisfied(q, ranked_chunks)
            if method == "bm25" and q.answerable:
                # BM25 が実際に返す本文（採点した文とその前後）に根拠があるか。チャンクの一致より厳しい
                texts = [Chunk(c.id, c.source, c.chunk_index, None, r["bm25_text"][c.id], c.kind, c.content_hash, c.input_hash) for c in ranked_chunks]
                m["text_hit"] = {str(k): satisfied(q, texts[:k]) for k in KS}
            if method != "bm25":
                m["sims_top"] = [round(r["sims"].get(i, 0.0), 4) for i in ids[:max(KS)] if i in r["sims"]]
            entry["methods"][method] = m
        per_question.append(entry)

    answerable = [e for e in per_question if e["category"] != OUT_OF_SCOPE]
    absent = [e for e in per_question if e["category"] == OUT_OF_SCOPE]
    summary: dict[str, Any] = {"n_answerable": len(answerable), "n_out_of_scope": len(absent)}
    for method in METHODS:
        ms = [e["methods"][method] for e in answerable]
        stats: dict[str, Any] = {
            "mrr": round(sum(1 / m["first_gold_rank"] if m["first_gold_rank"] else 0.0 for m in ms) / len(ms), 4) if ms else None,
            "hit_at": {str(k): sum(m["hit"][str(k)] for m in ms) for k in KS},
            "hit_depth": sum(m["hit_depth"] for m in ms),
            "not_abstained_and_hit_at": {str(k): sum(m["hit"][str(k)] and not m["abstained"] for m in ms) for k in KS},
            "answerable_abstained": sum(m["abstained"] for m in ms),
            "out_of_scope_abstained": sum(e["methods"][method]["abstained"] for e in absent),
        }
        if method == "bm25":
            stats["text_hit_at"] = {str(k): sum(m["text_hit"][str(k)] for m in ms) for k in KS}
        summary[method] = stats
    top1_answerable = [e["top1_sim"] for e in answerable]
    top1_absent = [e["top1_sim"] for e in absent]
    summary["top1_similarity"] = {
        "answerable": {"min": min(top1_answerable, default=None), "median": statistics.median(top1_answerable) if top1_answerable else None, "max": max(top1_answerable, default=None)},
        "out_of_scope": {"min": min(top1_absent, default=None), "median": statistics.median(top1_absent) if top1_absent else None, "max": max(top1_absent, default=None)},
    }
    summary["latency_ms_median"] = {
        "bm25_search": _median_ms([r["t_bm25"] for r in rows]),
        "vector_db_query": _median_ms([r["t_vector"] for r in rows]),
        "query_embedding_batch_per_text": _median_ms([
            e["batch_elapsed_s"] / e["batch_size"] for q in questions if (e := cache["entries"].get(_entry_key(query_text_of(q))))
        ]),
    }
    log = cache["log"]
    cost_chars = sum(len(query_text_of(q)) for q in load_all_questions())
    report = {
        "split": split,
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "plan_sha256": hashlib.sha256(PLAN_PATH.read_bytes()).hexdigest(),
        "corpus_fingerprint": plan["corpus_fingerprint"],
        "model": embedding.MODEL, "dimensions": embedding.DIMENSIONS,
        "ks": list(KS), "list_depth": LIST_DEPTH, "rrf_k": RRF_K,
        "bm25_params": search_params(),
        "tau": {k: (None if math.isinf(v) else round(v, 6)) for k, v in taus.items()},
        "tau_inf": {k: ("-inf" if v == -math.inf else "+inf" if v == math.inf else None) for k, v in taus.items()},
        "api_usage": {"texts_sent": cache["texts_sent"], "calls": cache["calls"], "limit_texts": MAX_TEXTS, "limit_calls": MAX_CALLS,
                      "failed_calls": sum(1 for e in log if e.get("status") != "ok"), "query_chars_total": cost_chars,
                      "estimated_cost_usd": round(cost_chars * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000, 6)},
        "summary": summary,
        "results": per_question,
    }
    if curves is not None:
        report["tau_curves"] = curves
    return report


def _tau_from_report(report: dict[str, Any]) -> dict[str, float]:
    def one(method: str) -> float:
        inf = report["tau_inf"][method]
        return -math.inf if inf == "-inf" else math.inf if inf == "+inf" else float(report["tau"][method])

    return {"vector": one("vector"), "hybrid": one("hybrid")}


def print_report(report: dict[str, Any]) -> None:
    s = report["summary"]
    print(f"[{report['split']}] 答えあり {s['n_answerable']} 問 / 資料外 {s['n_out_of_scope']} 問、しきい値 {report['tau']} {report['tau_inf']}")
    print(f"{'方式':8} {'MRR':>6} " + " ".join(f"hit@{k}" for k in KS) + " | 答えあり→答えなし | 資料外→答えなし")
    for method in METHODS:
        m = s[method]
        print(f"{method:8} {m['mrr']:>6} " + " ".join(f"{m['hit_at'][str(k)]:>5}" for k in KS) + f" | {m['answerable_abstained']:>16} | {m['out_of_scope_abstained']:>14}")
    print("BM25 の返す本文での hit:", s["bm25"]["text_hit_at"])
    print("1 位の類似度:", s["top1_similarity"])
    print("中央値の遅延(ms):", s["latency_ms_median"])
    print("API:", report["api_usage"])


def cmd_dev() -> int:
    report = run_split("dev", None)
    DEV_PATH.parent.mkdir(parents=True, exist_ok=True)
    DEV_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_report(report)
    print(f"-> {DEV_PATH.relative_to(ROOT)}")
    return 0


def cmd_final(force: bool) -> int:
    if not DEV_PATH.exists():
        print("先に dev を実行してください（しきい値は dev で決めます）。", file=sys.stderr)
        return 2
    if FINAL_PATH.exists() and not force:
        print(f"{FINAL_PATH.relative_to(ROOT)} が既にあります。held-out は 1 回だけ測るので、上書きしません。", file=sys.stderr)
        return 2
    dev = json.loads(DEV_PATH.read_text(encoding="utf-8"))
    report = run_split("held_out", _tau_from_report(dev))
    report["tau_from"] = {"file": DEV_PATH.relative_to(ROOT).as_posix(), "sha256": hashlib.sha256(DEV_PATH.read_bytes()).hexdigest()}
    FINAL_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_report(report)
    print(f"-> {FINAL_PATH.relative_to(ROOT)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="ベクトル検索の評価（DB は読み取りだけ。API は embed-queries --apply だけ）")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    emb = sub.add_parser("embed-queries")
    emb.add_argument("--apply", action="store_true")
    sub.add_parser("dev")
    fin = sub.add_parser("final")
    fin.add_argument("--force", action="store_true", help="既にある vector_final.json を上書きする（通常は使わない）")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return cmd_plan()
        if args.command == "embed-queries":
            return cmd_embed_queries(args.apply)
        if args.command == "dev":
            return cmd_dev()
        return cmd_final(args.force)
    except EvalError as exc:
        print(f"評価を続けられません: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
