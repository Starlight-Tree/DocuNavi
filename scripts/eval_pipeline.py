"""検索パイプライン（ハイブリッド・リランカー・GraphRAG）の実 API の埋め込みでの比較（実験。アプリの検索経路は変えない）。

同じ資料・同じ質問で、5 つの構成（既存のベクトル検索 / ハイブリッドのみ / +リランカー / +GraphRAG / 3 つ全部）を測り、
根拠が上位 1・3・5 件に入る率を出す。アプリの `app/search_pipeline.py` をそのまま呼ぶ。置き換えるのは次の 2 つだけ（DB を使わない）。

- `search_vector.search`: 実 API で作った埋め込みでコサイン類似度を計算し、`MIN_SIMILARITY` の下限と範囲の絞り込みは本物と同じ規則。
  本番の SQL（pgvector）そのものは通らない。
- `search_pipeline._fetch_chunks`: 資料の写し（キーワード・グラフ側）を、手元の PDF から作ったチャンクで返す。

    python -m scripts.eval_pipeline plan             # 資料・質問・固定する値を確定して evals/pipeline/plan.json に書く（API は呼ばない）
    python -m scripts.eval_pipeline embed            # 何を送るか・回数・概算費用を表示するだけ（API は呼ばない）
    python -m scripts.eval_pipeline embed --apply    # 実 API。埋め込みを evals/cache/ に保存する（Git 管理外）
    python -m scripts.eval_pipeline run              # 保存した埋め込みだけで 5 構成を測る（API は呼ばない）-> evals/results/pipeline_compare.json

守ること:
- キーは環境変数 GEMINI_API_KEY からだけ読む。`.env` は読まない（dotenv を使わない）。
- API は `embed --apply` だけ。LLM は呼ばない。呼び出しの計画は 17 回以内、累計の上限は 20 回（失敗・再実行も数える。SDK の再試行は無効）。
- RRF・リランカー・識別子の重みは `plan` が書いた値から変えない（`embed --apply` と `run` は、現在の値が plan と違えば止まる）。
  調整は、この比較に使った質問とは別の質問セットで行う。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import math
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app import search_keyword, search_pipeline, search_rerank, search_vector
from app.ingestion import embedding
from app.ingestion.chunker import chunk_pages
from app.ingestion.pdf_parser import extract_pages
from app.scope import Scope, require_scope
from scripts import eval_vector as ev
from scripts.eval_retrieval import CORPORA_DIR, Question, load_questions

ROOT = Path(__file__).resolve().parents[1]
FIXTURES_DIR = ROOT / "evals" / "fixtures"
PDF_DIR = ROOT / "storage" / "pdf"
PLAN_PATH = ROOT / "evals" / "pipeline" / "plan.json"
CACHE_PATH = ROOT / "evals" / "cache" / "pipeline_embeddings.json"
OLD_QUERY_CACHE_PATH = (
    ROOT / "evals" / "cache" / "query_embeddings.json"
)  # eval_vector の質問の埋め込み（読むだけ。書かない）
RESULT_PATH = ROOT / "evals" / "results" / "pipeline_compare.json"
JST = timezone(timedelta(hours=9))

QUESTION_FILES = (FIXTURES_DIR / "exact" / "questions.yaml", CORPORA_DIR / "guides" / "questions.yaml")
KS = (1, 3, 5)  # 報告する順位
REPORT_KS = (1, 2, 3, 5)  # 計算する順位（2 は本番の既定 `app/agents/tools.py` の TOP_K。表には出さず JSON に残す）
# 構成名 -> (SEARCH_HYBRID, SEARCH_RERANK, SEARCH_GRAPH)
CONFIGS: dict[str, tuple[bool, bool, bool]] = {
    "vector": (False, False, False),
    "hybrid": (True, False, False),
    "hybrid+rerank": (True, True, False),
    "hybrid+graph": (True, False, True),
    "all": (True, True, True),
}
MAX_PLANNED_CALLS = 17
HARD_CALL_LIMIT = 20
BATCH = embedding.BATCH_SIZE
PAUSE_SECONDS = 20  # 成功した呼び出しの後に待つ秒数（最後の呼び出しの後は待たない）
_PLACEHOLDER_KEYS = {"", "your_api_key_here"}


# ---------------------------------------------------------------- 資料と質問


@dataclass(frozen=True)
class CorpusChunk:
    source: str
    collection: str
    index: int
    page: int
    page_end: int | None
    heading: str | None
    content: str
    kind: str
    content_hash: str

    @property
    def text(self) -> str:
        return search_vector._chunk_text(self.heading, self.content)

    @property
    def doc_input(self) -> str:
        return embedding.document_text(self.heading, self.content)


def load_corpus() -> list[CorpusChunk]:
    """評価用の PDF と storage/pdf の PDF を、取り込みと同じ経路（抽出・チャンク化）で読む。同じ中身のファイルは 1 回だけ。"""
    seen: dict[str, Path] = {}
    names: dict[str, str] = {}
    chunks: list[CorpusChunk] = []
    for root in (CORPORA_DIR, FIXTURES_DIR, PDF_DIR):
        for path in sorted(root.glob("*/*.pdf")):
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
            if digest in seen:
                continue
            if names.setdefault(path.name, digest) != digest:
                raise ev.EvalError(f"同じ名前で中身の違う PDF があります: {path.name}")
            seen[digest] = path
            for chunk in chunk_pages(extract_pages(path)):
                chunks.append(
                    CorpusChunk(
                        path.name,
                        path.parent.name,
                        chunk.index,
                        chunk.page,
                        chunk.page_end,
                        chunk.heading,
                        chunk.text,
                        chunk.kind,
                        chunk.content_hash,
                    )
                )
    return chunks


def load_eval_questions() -> list[Question]:
    questions = [q for path in QUESTION_FILES for q in load_questions(path)]
    ids = [q.id for q in questions]
    if len(set(ids)) != len(ids):
        raise ev.EvalError("質問の id が重なっています")
    return questions


def as_eval_chunks(corpus: Sequence[CorpusChunk]) -> list[ev.Chunk]:
    return [
        ev.Chunk(i, c.source, c.index, c.heading, c.content, c.kind, c.content_hash, "") for i, c in enumerate(corpus)
    ]


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def corpus_fingerprint(corpus: Sequence[CorpusChunk]) -> str:
    digest = hashlib.sha256()
    for c in corpus:
        digest.update(f"{c.source}\0{c.index}\0{c.content_hash}\0{_sha(c.doc_input)}\n".encode())
    return digest.hexdigest()


def doc_inputs(corpus: Sequence[CorpusChunk]) -> dict[str, str]:
    """入力全体のハッシュ -> 送る本文（同じ入力は 1 回だけ送る）。"""
    return {_sha(c.doc_input): c.doc_input for c in corpus}


def query_inputs(questions: Sequence[Question]) -> dict[str, str]:
    return {_sha(embedding.query_text(q.question)): embedding.query_text(q.question) for q in questions}


# ---------------------------------------------------------------- 固定する値（結果を見る前に plan へ写す）


def fixed_params() -> dict[str, Any]:
    flag_names = ("SEARCH_HYBRID", "SEARCH_RERANK", "SEARCH_GRAPH", "SEARCH_CANDIDATES", "SEARCH_GRAPH_MAX_EXTRA")
    saved = {name: os.environ.pop(name, None) for name in flag_names}
    try:
        defaults = search_pipeline.Flags.from_env()
    finally:
        for name, value in saved.items():
            if value is not None:
                os.environ[name] = value
    return {
        "rrf_k": search_pipeline.RRF_K,
        "rerank_rrf_share": search_pipeline.RERANK_RRF_SHARE,
        "rerank_lexical_share": 1 - search_pipeline.RERANK_RRF_SHARE,
        "identifier_weight": search_rerank.IDENTIFIER_WEIGHT,
        "bm25_k1": search_keyword.K1,
        "bm25_b": search_keyword.B,
        "candidates": defaults.candidates,
        "graph_max_extra": defaults.graph_extra,
        "min_similarity": search_vector.MIN_SIMILARITY,
        "app_top_k": 2,
        "ks": list(REPORT_KS),
        "configs": {name: list(flags) for name, flags in CONFIGS.items()},
        "query": "質問文そのまま（LLM による言い換えは使わない）",
        "scope": "ALL（全グループ。collection の絞り込みなし）",
        "model": embedding.MODEL,
        "dimensions": embedding.DIMENSIONS,
    }


def _git_head() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def cmd_plan() -> int:
    corpus = load_corpus()
    questions = load_eval_questions()
    chunks = as_eval_chunks(corpus)
    problems: list[str] = []
    for q in questions:
        if not q.answerable:
            continue
        missing = [e for e, ids in ev.gold_chunks(q, chunks).items() if not ids]
        if missing:
            problems.append(f"{q.id}: どのチャンクにも無い根拠があります {[m[:20] for m in missing]}")
    if problems:
        print("正解を決められない質問があります（止めます。評価セットと資料のずれ）:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 2
    plan = {
        "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "note": "検索の結果を見る前に作った。ここに写した値は、この比較の中で変えない。",
        "fixed": fixed_params(),
        "corpus_fingerprint": corpus_fingerprint(corpus),
        "chunk_count": len(corpus),
        "sources": sorted({c.source for c in corpus}),
        "question_files": [p.relative_to(ROOT).as_posix() for p in QUESTION_FILES],
        "questions": [
            {"id": q.id, "answerable": q.answerable, "category": q.category, "expected": list(q.expected_doc_ids)}
            for q in questions
        ],
    }
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    answerable = sum(1 for q in questions if q.answerable)
    print(
        f"チャンク {len(corpus)} 件（{len(plan['sources'])} ファイル）、質問 {len(questions)} 問（答えあり {answerable}、資料外 {len(questions) - answerable}）"
    )
    print(
        f"固定した値: {json.dumps({k: v for k, v in plan['fixed'].items() if k in ('rrf_k', 'rerank_rrf_share', 'identifier_weight', 'candidates', 'graph_max_extra', 'min_similarity')}, ensure_ascii=False)}"
    )
    print(f"-> {PLAN_PATH.relative_to(ROOT)}")
    return 0


def check_plan(corpus: Sequence[CorpusChunk], questions: Sequence[Question]) -> dict[str, Any]:
    if not PLAN_PATH.exists():
        raise ev.EvalError("先に plan を実行してください（固定する値を、結果を見る前に写します）")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    if plan["fixed"] != json.loads(json.dumps(fixed_params())):
        raise ev.EvalError(
            "固定した値が plan と違います（重み・しきい値を変えたなら、別の質問セットで調整し、plan を作り直す）"
        )
    if plan["corpus_fingerprint"] != corpus_fingerprint(corpus):
        raise ev.EvalError("資料のチャンクが plan を作ったときと違います（plan から作り直してください）")
    if [q["id"] for q in plan["questions"]] != [q.id for q in questions]:
        raise ev.EvalError("質問が plan を作ったときと違います")
    return plan


# ---------------------------------------------------------------- 埋め込み（API。上限つき・キャッシュつき）


def _load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("model") != embedding.MODEL or data.get("dimensions") != embedding.DIMENSIONS:
            raise ev.EvalError("キャッシュのモデル・次元が現在と違います（別の場所に退避してください）")
        return data
    return {
        "model": embedding.MODEL,
        "dimensions": embedding.DIMENSIONS,
        "texts_sent": 0,
        "chars_sent": 0,
        "calls": 0,
        "log": [],
        "docs": {},
        "queries": {},
    }


def _save_cache(data: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _old_query_vectors() -> dict[str, list[float]]:
    """eval_vector が保存した質問の埋め込み（同じモデル・次元、同じ前置きのときだけ使う。読むだけ）。"""
    if not OLD_QUERY_CACHE_PATH.exists():
        return {}
    data = json.loads(OLD_QUERY_CACHE_PATH.read_text(encoding="utf-8"))
    if data.get("model") != embedding.MODEL or data.get("dimensions") != embedding.DIMENSIONS:
        return {}
    return {key: entry["vector"] for key, entry in data.get("entries", {}).items()}


def cmd_embed(apply: bool) -> int:
    corpus = load_corpus()
    questions = load_eval_questions()
    check_plan(corpus, questions)
    cache = _load_cache()
    old = _old_query_vectors()
    docs = doc_inputs(corpus)
    queries = query_inputs(questions)
    pending_docs = {h: t for h, t in docs.items() if h not in cache["docs"]}
    reused = [h for h in queries if h not in cache["queries"] and h in old]
    pending_queries = {h: t for h, t in queries.items() if h not in cache["queries"] and h not in old}
    chars = sum(len(t) for t in pending_docs.values()) + sum(len(t) for t in pending_queries.values())
    calls_needed = math.ceil(len(pending_docs) / BATCH) + math.ceil(len(pending_queries) / BATCH)
    too_long = [h[:8] for h, t in pending_docs.items() if len(t) > embedding.MAX_INPUT_CHARS]
    print(
        f"資料 {len(corpus)} チャンク（同じ入力をまとめて {len(docs)} 件）、キャッシュ済み {len(docs) - len(pending_docs)} 件、今回送る {len(pending_docs)} 件"
    )
    print(
        f"質問 {len(queries)} 件、キャッシュ済み {len(queries) - len(pending_queries) - len(reused)} 件、eval_vector のキャッシュを流用 {len(reused)} 件、今回送る {len(pending_queries)} 件"
    )
    print(
        f"今回の呼び出し {calls_needed} 回（1 回 {BATCH} 件まで）、{chars} 文字、概算費用 約 ${chars * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000:.5f}（1 文字 1 トークンの推計）"
    )
    print(
        f"累計: 送信 {cache['texts_sent']} 件・{cache['calls']} 回（計画の上限 {MAX_PLANNED_CALLS} 回、累計の上限 {HARD_CALL_LIMIT} 回）"
    )
    if too_long:
        print(
            f"入力が {embedding.MAX_INPUT_CHARS} 文字を超えるものがあります（{too_long[:5]}）。送りません。",
            file=sys.stderr,
        )
        return 2
    if not pending_docs and not pending_queries:
        print("送るものはありません。")
        return 0
    if calls_needed > MAX_PLANNED_CALLS or cache["calls"] + calls_needed > HARD_CALL_LIMIT:
        print("上限を超えるため送りません。", file=sys.stderr)
        return 2
    if not apply:
        print("（確認だけ。API は呼んでいません。--apply で送ります）")
        return 0

    api_key = os.environ.get("GEMINI_API_KEY", "").strip()  # .env は読まない（dotenv を使わない）
    if api_key in _PLACEHOLDER_KEYS:
        print("環境変数 GEMINI_API_KEY が要ります（未設定です）。API は呼んでいません。", file=sys.stderr)
        return 2
    embedder = embedding.GeminiEmbedder(api_key, embedding.ApiBudget(HARD_CALL_LIMIT - cache["calls"]))
    remaining_calls = calls_needed
    for kind, pending in (("docs", pending_docs), ("queries", pending_queries)):
        items = list(pending.items())
        for start in range(0, len(items), BATCH):
            batch = items[start : start + BATCH]
            texts = [t for _, t in batch]
            # 送る前に数えて保存する（途中で落ちても、送った分は上限に数える）
            cache["texts_sent"] += len(texts)
            cache["chars_sent"] += sum(len(t) for t in texts)
            cache["calls"] += 1
            record: dict[str, Any] = {
                "kind": kind,
                "n_texts": len(texts),
                "at": datetime.now(JST).isoformat(timespec="seconds"),
            }
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
            record.update(status="ok", elapsed_s=round(time.perf_counter() - began, 3))
            for (digest, _), vector in zip(batch, vectors, strict=True):
                cache[kind][digest] = vector
            _save_cache(cache)
            print(f"  {kind}: 送信 {len(texts)} 件、{record['elapsed_s']:.2f} 秒", flush=True)
            remaining_calls -= 1
            if remaining_calls > 0:
                # 続けて送ると 429（無料枠の毎分の上限）になったため、成功のたびに待つ。失敗したら上の except で止まる（再試行しない）
                time.sleep(PAUSE_SECONDS)
    cost = cache["chars_sent"] * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000
    print(
        f"完了。累計の送信 {cache['texts_sent']} 件・{cache['calls']} 回・{cache['chars_sent']} 文字、概算費用 約 ${cost:.5f}"
    )
    return 0


# ---------------------------------------------------------------- 測定


class VectorIndex:
    """実 API の埋め込みでのコサイン類似度。`search_vector.search` と同じ規則（上位 N 件を取ってから下限で切る）。"""

    def __init__(
        self, corpus: Sequence[CorpusChunk], doc_vectors: Sequence[list[float]], query_vectors: dict[str, list[float]]
    ) -> None:
        self.corpus = corpus
        self.docs = [(v, math.sqrt(sum(x * x for x in v))) for v in doc_vectors]
        self.queries = query_vectors
        self._sims: dict[str, list[float]] = {}

    def sims(self, query: str) -> list[float]:
        cached = self._sims.get(query)
        if cached is None:
            vector = self.queries[_sha(embedding.query_text(query))]
            norm = math.sqrt(sum(x * x for x in vector))
            cached = [sum(a * b for a, b in zip(v, vector, strict=True)) / (n * norm) for v, n in self.docs]
            self._sims[query] = cached
        return cached

    async def search(
        self, query: str, top_k: int, collection: str | None = None, *, scope: Scope
    ) -> list[search_vector.Hit]:
        scope = require_scope(scope)
        if top_k <= 0 or not query.strip():
            return []
        if scope.is_empty or (collection is not None and not scope.allows(collection)):
            return []
        sims = self.sims(query)
        eligible = [
            i
            for i, c in enumerate(self.corpus)
            if scope.allows(c.collection) and (collection is None or c.collection == collection)
        ]
        eligible.sort(key=lambda i: (-sims[i], self.corpus[i].source, self.corpus[i].index))
        hits = []
        for i in eligible[:top_k]:
            if sims[i] < search_vector.MIN_SIMILARITY:
                continue
            c = self.corpus[i]
            hits.append(search_vector.Hit(c.source, c.text, c.page, c.page_end, sims[i], c.collection))
        return hits

    async def fetch_chunks(self, scope: Scope, collection: str | None) -> list[search_pipeline.Chunk]:
        return [
            search_pipeline.Chunk(c.source, c.text, c.page, c.page_end, c.collection)
            for c in self.corpus
            if scope.allows(c.collection) and (collection is None or c.collection == collection)
        ]


class _LogCollector(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


def _eval_chunks(hits: Sequence[search_vector.Hit]) -> list[ev.Chunk]:
    return [ev.Chunk(i, h.source, 0, None, h.text, "text", "", "") for i, h in enumerate(hits)]


async def measure(index: VectorIndex, questions: Sequence[Question]) -> dict[str, Any]:
    collector = _LogCollector()
    logger = logging.getLogger("app.search_pipeline")
    previous_level = logger.level
    logger.setLevel(logging.INFO)
    logger.addHandler(collector)
    rows: dict[str, dict[str, Any]] = {name: {} for name in CONFIGS}
    leaked_query = 0
    try:
        for name, (hybrid, rerank, graph) in CONFIGS.items():
            os.environ["SEARCH_HYBRID"] = "on" if hybrid else "off"
            os.environ["SEARCH_RERANK"] = "on" if rerank else "off"
            os.environ["SEARCH_GRAPH"] = "on" if graph else "off"
            search_pipeline.clear_cache()
            for q in questions:
                per_k: dict[str, Any] = {}
                for k in REPORT_KS:
                    before = len(collector.lines)
                    hits = await search_pipeline.search(q.question, k, None, scope=Scope.ALL)
                    leaked_query += sum(1 for line in collector.lines[before:] if q.question in line)
                    main = [h for h in hits if h.origin != "graph"]
                    extras = [h for h in hits if h.origin == "graph"]
                    entry: dict[str, Any] = {
                        "n_main": len(main),
                        "origins": dict(Counter(h.origin or "vector" for h in main)),
                        "graph": [{"source": h.source, "page": h.page} for h in extras],
                    }
                    if q.answerable:
                        entry["hit_main"] = ev.satisfied(q, _eval_chunks(main[:k]))
                        entry["hit_with_graph"] = ev.satisfied(q, _eval_chunks(hits))
                    per_k[str(k)] = entry
                rows[name][q.id] = per_k
    finally:
        logger.removeHandler(collector)
        logger.setLevel(previous_level)
    graph_lines = [line for line in collector.lines if " graph from=" in line]
    return {
        "rows": rows,
        "log": {
            "lines": len(collector.lines),
            "lines_with_graph_hit": len(graph_lines),
            "lines_containing_query_text": leaked_query,
            "sample_graph_lines": graph_lines[:2],
            "sample_line": collector.lines[-1] if collector.lines else None,
        },
    }


def summarize(rows: dict[str, dict[str, Any]], questions: Sequence[Question]) -> dict[str, Any]:
    groups = {
        "exact": [q for q in questions if q.answerable and q.id.startswith("e")],
        "guides": [q for q in questions if q.answerable and not q.id.startswith("e")],
    }
    groups["total"] = groups["exact"] + groups["guides"]
    absent = [q for q in questions if not q.answerable]
    summary: dict[str, Any] = {
        "n": {name: len(qs) for name, qs in groups.items()},
        "n_out_of_scope": len(absent),
        "configs": {},
    }
    for name in CONFIGS:
        per = rows[name]
        cfg: dict[str, Any] = {
            "hit_main": {},
            "hit_with_graph": {},
            "vs_vector": {},
            "out_of_scope_returned": {},
            "graph": {},
        }
        for group, qs in groups.items():
            for field in ("hit_main", "hit_with_graph"):
                cfg[field][group] = {str(k): sum(1 for q in qs if per[q.id][str(k)][field]) for k in REPORT_KS}
        for k in REPORT_KS:
            gained = [
                q.id
                for q in groups["total"]
                if per[q.id][str(k)]["hit_main"] and not rows["vector"][q.id][str(k)]["hit_main"]
            ]
            lost = [
                q.id
                for q in groups["total"]
                if not per[q.id][str(k)]["hit_main"] and rows["vector"][q.id][str(k)]["hit_main"]
            ]
            cfg["vs_vector"][str(k)] = {"gained": gained, "lost": lost}
            cfg["out_of_scope_returned"][str(k)] = sum(1 for q in absent if per[q.id][str(k)]["n_main"] > 0)
            with_extra = [q for q in groups["total"] if per[q.id][str(k)]["graph"]]
            turned = [
                q.id for q in with_extra if per[q.id][str(k)]["hit_with_graph"] and not per[q.id][str(k)]["hit_main"]
            ]
            cfg["graph"][str(k)] = {
                "questions_with_extra": len(with_extra),
                "extras_total": sum(len(per[q.id][str(k)]["graph"]) for q in groups["total"]),
                "extra_made_it_hit": turned,
                "out_of_scope_with_extra": sum(1 for q in absent if per[q.id][str(k)]["graph"]),
            }
        summary["configs"][name] = cfg
    return summary


def _pct(hit: int, n: int) -> str:
    return f"{hit}/{n} ({100 * hit / n:.0f}%)" if n else "-"


def print_table(summary: dict[str, Any]) -> None:
    n = summary["n"]
    for group in ("total", "exact", "guides"):
        print(f"\n[{group}] 答えあり {n[group]} 問（根拠が上位 k 件に入った数。本体の上位 k 件だけ）")
        print(f"{'構成':14} " + " ".join(f"{'k=' + str(k):>12}" for k in KS))
        for name in CONFIGS:
            hits = summary["configs"][name]["hit_main"][group]
            print(f"{name:14} " + " ".join(f"{_pct(hits[str(k)], n[group]):>12}" for k in KS))
    print("\nGraphRAG の追加文脈を含めたとき（本体の上位 k 件 + graph の追加）[total]")
    for name in ("hybrid+graph", "all"):
        hits = summary["configs"][name]["hit_with_graph"]["total"]
        print(f"{name:14} " + " ".join(f"{_pct(hits[str(k)], n['total']):>12}" for k in KS))


def cmd_run() -> int:
    corpus = load_corpus()
    questions = load_eval_questions()
    plan = check_plan(corpus, questions)
    cache = _load_cache()
    old = _old_query_vectors()
    missing_docs = [h for h in doc_inputs(corpus) if h not in cache["docs"]]
    queries: dict[str, list[float]] = {}
    for h in query_inputs(questions):
        vector = cache["queries"].get(h) or old.get(h)
        if vector is None:
            raise ev.EvalError("質問の埋め込みがキャッシュにありません（embed --apply が要ります）")
        queries[h] = vector
    if missing_docs:
        raise ev.EvalError(
            f"資料の埋め込みがキャッシュにありません（{len(missing_docs)} 件。embed --apply が要ります）"
        )
    index = VectorIndex(corpus, [cache["docs"][_sha(c.doc_input)] for c in corpus], queries)

    saved_env = {
        name: os.environ.get(name)
        for name in ("SEARCH_HYBRID", "SEARCH_RERANK", "SEARCH_GRAPH", "SEARCH_CORPUS_TTL_SECONDS")
    }
    saved_search, saved_fetch = search_vector.search, search_pipeline._fetch_chunks
    os.environ["SEARCH_CORPUS_TTL_SECONDS"] = "3600"
    search_vector.search = index.search  # type: ignore[assignment]
    search_pipeline._fetch_chunks = index.fetch_chunks  # type: ignore[assignment]
    try:
        measured = asyncio.run(measure(index, questions))
    finally:
        search_vector.search = saved_search  # type: ignore[assignment]
        search_pipeline._fetch_chunks = saved_fetch  # type: ignore[assignment]
        search_pipeline.clear_cache()
        for name, value in saved_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    summary = summarize(measured["rows"], questions)
    sent = cache["chars_sent"]
    report = {
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "plan_sha256": hashlib.sha256(PLAN_PATH.read_bytes()).hexdigest(),
        "fixed": plan["fixed"],
        "chunk_count": len(corpus),
        "api_usage": {
            "texts_sent": cache["texts_sent"],
            "calls": cache["calls"],
            "failed_calls": sum(1 for e in cache["log"] if e.get("status") != "ok"),
            "chars_sent": sent,
            "estimated_cost_usd": round(sent * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000, 6),
            "queries_reused_from_eval_vector_cache": sum(
                1 for h in query_inputs(questions) if h not in cache["queries"]
            ),
        },
        "log_check": measured["log"],
        "summary": summary,
        "rows": measured["rows"],
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_table(summary)
    print("\nログ:", {k: v for k, v in measured["log"].items() if k.startswith("lines")})
    print("API:", report["api_usage"])
    print(f"-> {RESULT_PATH.relative_to(ROOT)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="検索パイプラインの比較（API は embed --apply だけ。キーは環境変数から）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    emb = sub.add_parser("embed")
    emb.add_argument("--apply", action="store_true")
    sub.add_parser("run")
    args = parser.parse_args(argv)
    try:
        if args.command == "plan":
            return cmd_plan()
        if args.command == "embed":
            return cmd_embed(args.apply)
        return cmd_run()
    except ev.EvalError as exc:
        print(f"続けられません: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
