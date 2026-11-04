"""OSS 埋め込みモデルの、日本語の検索性能の比較（実験。アプリの検索経路は変えない。実 API は呼ばない）。

`eval_pipeline` と同じ資料（PDF から作ったチャンク 421 件）・同じ質問（64 問）で、各モデルの top-k の正解率を測る。
Gemini の値は、`embed --apply` で保存済みの埋め込み（evals/cache/pipeline_embeddings.json）から、同じ採点で出す（API は呼ばない）。

2 段に分ける。モデル（torch）は、リポジトリの依存に入れず、別の仮想環境で動かすため。

    python -m scripts.eval_oss_embeddings export --out <dir>/dataset.json         # リポジトリの環境。チャンク・質問・正解の対応・Gemini の保存済みの埋め込み
    <ML の仮想環境>/bin/python scripts/eval_oss_embeddings.py run --dataset <dir>/dataset.json --model ruri-v3-310m --out evals/results/oss_embeddings_ruri-v3-310m.json

`run` は numpy と sentence-transformers だけで動く（`app` を import しない）。モデルの重みは Hugging Face から取得する（初回のみ）。

守ること:
- 採点は `eval_vector.satisfied`（根拠の文が、返ったチャンクに含まれるか）と同じ意味。`run` の中の再実装は、`export` が Gemini の値を両方で出して一致を確かめる。
- 類似度のしきい値（`MIN_SIMILARITY` = 0.65 は Gemini 用に調整した値）は使わない。順位だけで測る。資料外の質問は、この比較に含めない。
- 質問・資料の前置きは、各モデルの公式の使い方に従う（`MODELS`）。Gemini の前置き（`title: … | text: …`）は流用しない。
- 元の質問・期待値は変えない。
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path
from typing import Any

KS = (1, 2, 3, 5)

# name -> (HF のモデル ID, 質問の前置き, 資料の前置き, 本来の次元)
MODELS: dict[str, dict[str, Any]] = {
    "ruri-v3-310m": {"id": "cl-nagoya/ruri-v3-310m", "query": "検索クエリ: ", "doc": "検索文書: ", "dims": 768},
    "ruri-v3-130m": {"id": "cl-nagoya/ruri-v3-130m", "query": "検索クエリ: ", "doc": "検索文書: ", "dims": 512},
    "multilingual-e5-base": {
        "id": "intfloat/multilingual-e5-base",
        "query": "query: ",
        "doc": "passage: ",
        "dims": 768,
    },
    "multilingual-e5-large": {
        "id": "intfloat/multilingual-e5-large",
        "query": "query: ",
        "doc": "passage: ",
        "dims": 1024,
    },
    "bge-m3": {"id": "BAAI/bge-m3", "query": "", "doc": "", "dims": 1024},
    "qwen3-embedding-0.6b": {
        "id": "Qwen/Qwen3-Embedding-0.6B",
        "query": "Instruct: Given a web search query, retrieve relevant passages that answer the query\nQuery:",
        "doc": "",
        "dims": 1024,
    },
}


# ---------------------------------------------------------------- 採点（app を使わない。export の一致確認で検証する）


def satisfied(
    q: dict[str, Any], retrieved: list[int], chunks: list[dict[str, Any]], contains: dict[str, list[bool]]
) -> bool:
    if not q["answerable"]:
        return False
    mine = [i for i in retrieved if chunks[i]["source"] in q["expected_doc_ids"]]
    if q["evidence_mode"] == "all":
        return all(any(contains[ev][i] for i in mine) for ev in q["evidence"])
    return all(
        any(chunks[i]["source"] == name and contains[ev][i] for i in mine for ev in q["evidence"])
        for name in q["expected_doc_ids"]
    )


def first_gold_rank(
    q: dict[str, Any], ranked: list[int], chunks: list[dict[str, Any]], contains: dict[str, list[bool]]
) -> int | None:
    for rank, i in enumerate(ranked, start=1):
        if chunks[i]["source"] in q["expected_doc_ids"] and any(contains[ev][i] for ev in q["evidence"]):
            return rank
    return None


def score(dataset: dict[str, Any], ranking: dict[str, list[int]]) -> dict[str, Any]:
    chunks = dataset["chunks"]
    hits: dict[str, dict[str, list[str]]] = {}
    ranks: dict[str, int | None] = {}
    for q in dataset["questions"]:
        if not q["answerable"]:
            continue
        ranked = ranking[q["id"]]
        contains = {ev: dataset["contains"][q["id"]][ev] for ev in q["evidence"]}
        ranks[q["id"]] = first_gold_rank(q, ranked, chunks, contains)
        for k in KS:
            if satisfied(q, ranked[:k], chunks, contains):
                hits.setdefault(q["set"], {}).setdefault(str(k), []).append(q["id"])
    answerable = [q for q in dataset["questions"] if q["answerable"]]
    n_by_set: dict[str, int] = {}
    for q in answerable:
        n_by_set[q["set"]] = n_by_set.get(q["set"], 0) + 1
    total = {str(k): sum(len(hits.get(s, {}).get(str(k), [])) for s in n_by_set) for k in KS}
    rr = [1 / r if r else 0.0 for r in ranks.values()]
    return {
        "n": {**n_by_set, "total": len(answerable)},
        "hit": {"total": total, **{s: {str(k): len(hits.get(s, {}).get(str(k), [])) for k in KS} for s in n_by_set}},
        "mrr": round(sum(rr) / len(rr), 4),
        "missed_at_3": sorted(
            q["id"] for q in answerable if q["id"] not in {i for s in hits.values() for i in s.get("3", [])}
        ),
    }


# ---------------------------------------------------------------- export（リポジトリの環境）


def cmd_export(out: Path) -> int:
    import asyncio  # noqa: F401

    from scripts import eval_pipeline as ep
    from scripts import eval_vector as ev
    from scripts.eval_retrieval import _contains

    corpus = ep.load_corpus()
    questions = ep.load_eval_questions()
    cache = ep._load_cache()
    old = ep._old_query_vectors()
    eval_chunks = ep.as_eval_chunks(corpus)
    chunks = [{"source": c.source, "heading": c.heading, "content": c.content, "text": c.text} for c in corpus]
    qs, contains = [], {}
    for q in questions:
        qs.append(
            {
                "id": q.id,
                "question": q.question,
                "answerable": q.answerable,
                "expected_doc_ids": list(q.expected_doc_ids),
                "evidence": list(q.evidence),
                "evidence_mode": q.evidence_mode,
                "set": "guides" if q.id[0] in "at" else "exact",
            }
        )
        contains[q.id] = {ev_: [_contains(c.text, ev_) for c in corpus] for ev_ in q.evidence}
    doc_vecs = [cache["docs"][ep._sha(c.doc_input)] for c in corpus]
    q_vecs = {}
    for q in questions:
        h = ep._sha(ep.embedding.query_text(q.question))
        q_vecs[q.id] = cache["queries"].get(h) or old.get(h)
    # Gemini（保存済みの埋め込み）の順位。しきい値なし
    import math

    def cos(a, b):
        return sum(x * y for x, y in zip(a, b, strict=True)) / (
            math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
        )

    gemini_rank = {}
    ref = {"hit": {k: 0 for k in KS}}
    for q in questions:
        scores = sorted(range(len(corpus)), key=lambda i: -cos(q_vecs[q.id], doc_vecs[i]))
        gemini_rank[q.id] = scores[:20]
        if q.answerable:
            for k in KS:
                ref["hit"][k] += ev.satisfied(q, [eval_chunks[i] for i in scores[:k]])
    dataset = {
        "chunks": chunks,
        "questions": qs,
        "contains": contains,
        "gemini_rank": gemini_rank,
        "reference_hit_from_app_logic": ref["hit"],
    }
    mine = score(dataset, gemini_rank)
    print("Gemini（保存済み・しきい値なし）: アプリの採点", ref["hit"], "/ 再実装の採点", mine["hit"]["total"])
    if {str(k): v for k, v in ref["hit"].items()} != mine["hit"]["total"]:
        print("採点の再実装が、アプリの採点と一致しません", file=sys.stderr)
        return 1
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataset, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"チャンク {len(chunks)} 件・質問 {len(qs)} 問（答えあり {sum(q['answerable'] for q in qs)}）-> {out}")
    return 0


# ---------------------------------------------------------------- run（ML の仮想環境）


def cmd_run(
    dataset_path: Path, name: str, out: Path, truncate: int | None, max_seq: int, device: str, dtype: str | None
) -> int:
    import numpy as np
    from sentence_transformers import SentenceTransformer

    spec = MODELS[name]
    dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
    model_kwargs: dict[str, Any] = {}
    if dtype:
        import torch

        model_kwargs["dtype"] = getattr(torch, dtype)
    model = SentenceTransformer(spec["id"], device=device, model_kwargs=model_kwargs)
    model.max_seq_length = min(model.max_seq_length or max_seq, max_seq)

    def text_of(c: dict[str, Any]) -> str:
        return f"{c['heading']}\n{c['content']}" if c["heading"] else c["content"]

    docs = [spec["doc"] + text_of(c) for c in dataset["chunks"]]
    if (
        device != "cpu"
    ):  # 初回の呼び出しにはカーネルの準備が入る。CPU の既存の測定と同じ手順を保つため、CPU では行わない
        model.encode(
            [spec["query"] + dataset["questions"][0]["question"]], convert_to_numpy=True, show_progress_bar=False
        )
    started = time.monotonic()
    doc_vecs = model.encode(
        docs, batch_size=8, normalize_embeddings=False, convert_to_numpy=True, show_progress_bar=False
    )
    doc_seconds = time.monotonic() - started

    def prep(v):
        if truncate:
            v = v[..., :truncate]
        return v / np.linalg.norm(v, axis=-1, keepdims=True)

    doc_vecs = prep(doc_vecs)
    ranking, latencies = {}, []
    for q in dataset["questions"]:
        t = time.monotonic()
        qv = prep(model.encode([spec["query"] + q["question"]], convert_to_numpy=True, show_progress_bar=False))[0]
        latencies.append(time.monotonic() - t)
        ranking[q["id"]] = [int(i) for i in np.argsort(-(doc_vecs @ qv))[:20]]
    result = {
        "model": spec["id"],
        "name": name + (f"@{truncate}" if truncate else ""),
        "dims": int(doc_vecs.shape[1]),
        "max_seq_length": model.max_seq_length,
        "device": device,
        "dtype": dtype or "model default",
        "torch": __import__("torch").__version__,
        "prefixes": {"query": spec["query"], "doc": spec["doc"]},
        "chunks": len(docs),
        "encode_docs_seconds": round(doc_seconds, 1),
        "query_latency_ms": {
            "median": round(statistics.median(latencies) * 1000, 1),
            "max": round(max(latencies) * 1000, 1),
            "note": ("8 コアの CPU" if device == "cpu" else f"{device}（Mac の GPU）")
            + "・1 本ずつ。本番の機器の値ではない",
        },
        **score(dataset, ranking),
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    h = result["hit"]["total"]
    print(
        f"{result['name']:28} dims={result['dims']:4} n={result['n']['total']} "
        + " ".join(f"@{k}={h[str(k)]}" for k in KS)
        + f" MRR={result['mrr']} docs={result['encode_docs_seconds']}s q_med={result['query_latency_ms']['median']}ms"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    e = sub.add_parser("export")
    e.add_argument("--out", type=Path, required=True)
    r = sub.add_parser("run")
    r.add_argument("--dataset", type=Path, required=True)
    r.add_argument("--model", choices=sorted(MODELS), required=True)
    r.add_argument("--out", type=Path, required=True)
    r.add_argument(
        "--truncate", type=int, help="先頭の N 次元に切り詰めて正規化（Matryoshka 対応モデルのみ意味がある）"
    )
    r.add_argument(
        "--max-seq", type=int, default=2048, help="入力の最大トークン数の上限（モデルの上限が小さければそちら）"
    )
    r.add_argument("--device", default="cpu", help="cpu / mps（Mac の GPU。Metal）など。既定は cpu（既存の測定と同じ）")
    r.add_argument(
        "--dtype", choices=["float32", "float16", "bfloat16"], help="モデルの重みの型。未指定ならモデルの既定"
    )
    args = parser.parse_args(argv)
    if args.cmd == "export":
        return cmd_export(args.out)
    return cmd_run(args.dataset, args.model, args.out, args.truncate, args.max_seq, args.device, args.dtype)


if __name__ == "__main__":
    raise SystemExit(main())
