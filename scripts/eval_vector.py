"""ベクトル検索の評価の部品（質問の分割・チャンク単位の採点・融合・しきい値）と、質問の埋め込みのキャッシュ。

Postgres（pgvector）を読む部分（`connect` `load_chunks` `search_vector` と、それを使う `plan` `dev` `final`）は、
本番が D1 + Vectorize に移った（2026-11-08）ので削除した。当時の実装と結果の読み方は git 履歴と evals/README.md。
`eval_pipeline.py` `eval_graph.py` `eval_oss_embeddings.py` が、ここの純粋な関数（`Chunk` `gold_chunks` `satisfied`）を使う。

    python -m scripts.eval_vector embed-queries             # 何を送るか・件数・概算費用を表示するだけ（API は呼ばない）
    python -m scripts.eval_vector embed-queries --apply     # 実 API。質問の埋め込みを evals/cache/ に保存する（Git 管理外）

API の上限: 送信 40 件・呼び出し 5 回（1 回 8 件）。失敗・再実行も数える（cache に累計を残す）。SDK の再試行は無効。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from app.ingestion import embedding
from scripts.eval_retrieval import (
    CORPORA_DIR,
    OUT_OF_SCOPE,
    Question,
    _contains,
    load_questions,
)

ROOT = Path(__file__).resolve().parents[1]
CACHE_PATH = ROOT / "evals" / "cache" / "query_embeddings.json"
JST = timezone(timedelta(hours=9))

# --- 結果を見る前に固定した値（変えるときは plan を作り直し、dev からやり直す） ---
SPLIT_SALT = "vector-eval-v1"
HELD_OUT_RATIO = 0.4  # 層（期待ファイル / 資料外）ごとに round(n × 0.4) 件を held-out にする。選ぶ順は sha256(salt:id)
RRF_K = 60  # Reciprocal Rank Fusion の定数（一般的な既定値。調整しない）

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
    return {ev: [c.id for c in chunks if c.source in q.expected_doc_ids and _contains(c.text, ev)] for ev in q.evidence}


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
        any(c.source == name and _contains(c.text, ev) for c in mine for ev in q.evidence)
        for name in q.expected_doc_ids
    )


def first_gold_rank(q: Question, ranked: Sequence[Chunk]) -> int | None:
    """根拠を 1 つでも含む期待ファイルのチャンクが最初に現れる順位（1 始まり）。無ければ None。"""
    for rank, c in enumerate(ranked, start=1):
        if c.source in q.expected_doc_ids and any(_contains(c.text, ev) for ev in q.evidence):
            return rank
    return None


# ---------------------------------------------------------------- 検索 3 方式


def query_text_of(q: Question) -> str:
    return embedding.query_text(q.question)


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
        curve.append(
            {
                "tau": None if math.isinf(tau) else round(tau, 6),
                "inf": "-inf" if tau == -math.inf else ("+inf" if tau == math.inf else None),
                "answerable_kept": kept,
                "absent_refused": refused,
                "balanced_accuracy": round((tpr + tnr) / 2, 4),
            }
        )
    best = max(range(len(candidates)), key=lambda i: (curve[i]["balanced_accuracy"], -i))
    return candidates[best], curve


# ---------------------------------------------------------------- 質問の埋め込み（API。上限つき・キャッシュつき）


def _load_cache() -> dict[str, Any]:
    if CACHE_PATH.exists():
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        if data.get("model") != embedding.MODEL or data.get("dimensions") != embedding.DIMENSIONS:
            raise EvalError("キャッシュのモデル・次元が現在と違います（別の場所に退避してください）")
        return data
    return {
        "model": embedding.MODEL,
        "dimensions": embedding.DIMENSIONS,
        "texts_sent": 0,
        "calls": 0,
        "log": [],
        "entries": {},
    }


def _save_cache(data: dict[str, Any]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(CACHE_PATH)


def _entry_key(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def cmd_embed_queries(apply: bool) -> int:
    questions = load_all_questions()
    cache = _load_cache()
    todo = [q for q in questions if _entry_key(query_text_of(q)) not in cache["entries"]]
    chars = sum(len(query_text_of(q)) for q in todo)
    calls_needed = math.ceil(len(todo) / BATCH)
    print(
        f"質問 {len(questions)} 件、キャッシュ済み {len(questions) - len(todo)} 件、今回送る {len(todo)} 件（{chars} 文字、前置きを含む）"
    )
    print(
        f"必要な呼び出し {calls_needed} 回（1 回 {BATCH} 件）。概算費用 約 ${chars * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000:.5f}（1 文字 1 トークンの推計）"
    )
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
            cache["entries"][_entry_key(text)] = {
                "id": q.id,
                "vector": vector,
                "batch_elapsed_s": round(elapsed, 3),
                "batch_size": len(texts),
            }
        _save_cache(cache)
        print(f"  送信 {len(texts)} 件、{elapsed:.2f} 秒")
    print(f"完了。累計の送信 {cache['texts_sent']} 件・{cache['calls']} 回")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="質問の埋め込みのキャッシュ（API は embed-queries --apply だけ）")
    sub = parser.add_subparsers(dest="command", required=True)
    emb = sub.add_parser("embed-queries")
    emb.add_argument("--apply", action="store_true")
    args = parser.parse_args(argv)
    try:
        return cmd_embed_queries(args.apply)
    except EvalError as exc:
        print(f"評価を続けられません: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
