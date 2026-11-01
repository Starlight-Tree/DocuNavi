"""GraphRAG の効果を測る評価（実験。アプリの検索経路・本番のチャンク分割・本番 DB は変えない）。

`scripts/eval_pipeline.py` の比較は、資料に「第N条」の参照が無く、GraphRAG の辺が 0 本だったので、効果を測れなかった。
ここでは、評価専用の資料（`evals/fixtures/graph_rules/`。`storage/pdf/` の外）を、**1 条 = 1 チャンク**で読み、
同じ 5 構成（ベクトルのみ / +ハイブリッド / +リランカー / +GraphRAG / 全部）を、質問の種類（`kind`）ごとに測る。
`app/search/pipeline.py` をそのまま呼び、置き換えるのは `eval_pipeline.py` と同じ 2 つだけ（DB を使わない）。

    python -m scripts.eval_graph plan             # 資料・辺・質問・固定する値を確定して evals/pipeline/graph_plan.json に書く（API は呼ばない）
    python -m scripts.eval_graph embed            # 何を送るか・回数・概算費用を表示するだけ（API は呼ばない）
    python -m scripts.eval_graph embed --apply    # 実 API。埋め込みを evals/cache/ に保存する（Git 管理外）
    python -m scripts.eval_graph run              # 保存した埋め込みだけで測る（API は呼ばない）-> evals/results/graph_rules_compare.json

評価セットは `--set` で選ぶ（どのコマンドにも付ける）。既定は `graph_rules`（最初のセット。上の出力先）。`graph_rules_ext` は、遠い参照（ref_far）と
3 条またぎ（chain3）を増やした別の資料・質問で、計画・キャッシュ・結果も別のファイルに書く（`graph_ext_plan.json`・`graph_rules_ext_embeddings.json`・
`graph_rules_ext_compare.json`）。最初のセットの質問・計画・結果は読まず、書かない。

守ること:
- キーは環境変数 GEMINI_API_KEY からだけ読む。`.env` は読まない（dotenv を使わない）。
- API は `embed --apply` だけ（埋め込みのみ。LLM は呼ばない）。計画は 2 回（資料 1 回、質問 1 回）、累計の上限は 3 回
  （失敗も数える。SDK の再試行は無効。自分でも再試行しない。失敗は 429 を含めて、その場で止める）。
- RRF・リランカー・識別子の重みは `plan` が書いた値から変えない（`embed --apply` と `run` は、現在の値が plan と違えば止まる）。
- 既存の `evals/results/pipeline_compare.json`・`evals/cache/pipeline_embeddings.json`・`evals/pipeline/plan.json` には触れない。
- `CorpusChunk.page` には条の番号を入れる（ログの `graph from=` と結果の `graph` の項目で、どの条かが分かるように）。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import os
import re
import sys
import time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from app.ingestion import embedding
from app.search import pipeline as search_pipeline
from app.search import vector as search_vector
from app.search.graph import build_edges
from scripts import eval_vector as ev
from scripts.eval_pipeline import (
    CONFIGS,
    JST,
    REPORT_KS,
    CorpusChunk,
    VectorIndex,
    _git_head,
    _sha,
    as_eval_chunks,
    corpus_fingerprint,
    doc_inputs,
    fixed_params,
    measure,
    query_inputs,
)
from scripts.eval_retrieval import Question, load_questions

ROOT = Path(__file__).resolve().parents[1]
CORPUS_DIR = ROOT / "evals" / "fixtures" / "graph_rules"
QUESTIONS_PATH = CORPUS_DIR / "questions.yaml"
COLLECTION = CORPUS_DIR.name
PLAN_PATH = ROOT / "evals" / "pipeline" / "graph_plan.json"
CACHE_PATH = ROOT / "evals" / "cache" / "graph_rules_embeddings.json"
RESULT_PATH = ROOT / "evals" / "results" / "graph_rules_compare.json"

MAX_PLANNED_CALLS = 2
HARD_CALL_LIMIT = 3

# 評価セット。既定の `graph_rules` の値は変えない。`graph_rules_ext` は資料 69 チャンク（32 件ずつ 3 回）と質問 62 問（32 件ずつ 2 回）で、計画は 5 回、累計の上限は 6 回
SETS: dict[str, dict[str, Any]] = {
    "graph_rules": {
        "corpus_dir": CORPUS_DIR,
        "plan": PLAN_PATH,
        "cache": CACHE_PATH,
        "result": RESULT_PATH,
        "planned_calls": MAX_PLANNED_CALLS,
        "hard_limit": HARD_CALL_LIMIT,
    },
    "graph_rules_ext": {
        "corpus_dir": ROOT / "evals" / "fixtures" / "graph_rules_ext",
        "plan": ROOT / "evals" / "pipeline" / "graph_ext_plan.json",
        "cache": ROOT / "evals" / "cache" / "graph_rules_ext_embeddings.json",
        "result": ROOT / "evals" / "results" / "graph_rules_ext_compare.json",
        "planned_calls": 5,
        "hard_limit": 6,
    },
}
BATCH = embedding.BATCH_SIZE
PAUSE_SECONDS = 20  # 成功した呼び出しの後に待つ秒数（最後の呼び出しの後は待たない）
_PLACEHOLDER_KEYS = {"", "your_api_key_here"}


def _shown(path: Path) -> str:
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.name


def use_set(name: str) -> None:
    """評価セットを選ぶ（資料・質問・計画・キャッシュ・結果・呼び出しの上限）。選ぶ前の既定は `graph_rules`。"""
    global \
        CORPUS_DIR, \
        QUESTIONS_PATH, \
        COLLECTION, \
        PLAN_PATH, \
        CACHE_PATH, \
        RESULT_PATH, \
        MAX_PLANNED_CALLS, \
        HARD_CALL_LIMIT
    if name not in SETS:
        raise ev.EvalError(f"評価セットがありません: {name}（{', '.join(SETS)}）")
    chosen = SETS[name]
    CORPUS_DIR = chosen["corpus_dir"]
    QUESTIONS_PATH = CORPUS_DIR / "questions.yaml"
    COLLECTION = CORPUS_DIR.name
    PLAN_PATH, CACHE_PATH, RESULT_PATH = chosen["plan"], chosen["cache"], chosen["result"]
    MAX_PLANNED_CALLS, HARD_CALL_LIMIT = chosen["planned_calls"], chosen["hard_limit"]


# 質問の種類（questions.yaml の kind）。GraphRAG の効果を見るのは「参照が要る」質問
DEPENDENT_KINDS = ("ref_far", "ref_near", "chain3")
KIND_GROUPS: dict[str, tuple[str, ...]] = {
    "dependent": DEPENDENT_KINDS,
    "ref_far": ("ref_far",),
    "ref_near": ("ref_near",),
    "chain3": ("chain3",),
    "ref_unneeded": ("ref_unneeded",),
    "single": ("single",),
}

_HEADING = re.compile(r"^第(\d+)条（(.+)）\s*$")
_ARTICLE_AT_LINE_START = re.compile(r"^\s*第\d+条")


# ---------------------------------------------------------------- 資料（1 条 = 1 チャンク）


def parse_articles(path: Path) -> list[tuple[int, str, str]]:
    """条の列（番号、見出し「第N条（題）」、本文）。題名の 1 行は捨てる。本文の行の頭に「第N条」を置けない（辺の定義と取り違える）。"""
    articles: list[tuple[int, str, list[str]]] = []
    title: str | None = None
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        match = _HEADING.match(line)
        if match:
            articles.append((int(match.group(1)), line, []))
        elif not line:
            continue
        elif not articles:
            if title is not None:
                raise ev.EvalError(f"{path.name}: 最初の条の前に、題名の 1 行以外があります")
            title = line
        elif _ARTICLE_AT_LINE_START.match(line):
            raise ev.EvalError(
                f"{path.name}: 本文の行が「第N条」で始まっています（見出しの書式でもありません）: {line[:20]}"
            )
        else:
            articles[-1][2].append(line)
    if not articles:
        raise ev.EvalError(f"{path.name}: 条がありません")
    numbers = [number for number, _, _ in articles]
    if numbers != list(range(1, len(articles) + 1)):
        raise ev.EvalError(f"{path.name}: 条の番号が 1 から続いていません: {numbers}")
    empty = [number for number, _, body in articles if not body]
    if empty:
        raise ev.EvalError(f"{path.name}: 本文が空の条があります: {empty}")
    return [(number, heading, "\n".join(body)) for number, heading, body in articles]


def load_corpus(directory: Path | None = None) -> list[CorpusChunk]:
    directory = directory or CORPUS_DIR  # 既定値は呼ぶときに決める（use_set で変わる）
    chunks: list[CorpusChunk] = []
    for path in sorted(directory.glob("*.txt")):
        for index, (number, heading, content) in enumerate(parse_articles(path)):
            chunks.append(
                CorpusChunk(
                    path.name,
                    directory.name,
                    index,
                    number,
                    None,
                    heading,
                    content,
                    "text",
                    _sha(f"{heading}\n{content}"),
                )
            )
    if not chunks:
        raise ev.EvalError(f"資料がありません: {directory}")
    return chunks


def load_eval_questions(path: Path | None = None) -> list[Question]:
    return load_questions(path or QUESTIONS_PATH)


def edges_of(corpus: Sequence[CorpusChunk]) -> dict[int, list[int]]:
    return build_edges([c.source for c in corpus], [c.text for c in corpus])


def describe_edges(corpus: Sequence[CorpusChunk], edges: dict[int, list[int]]) -> list[str]:
    return [
        f"{corpus[src].source} 第{corpus[src].page}条 -> 第{corpus[dst].page}条"
        for src, targets in sorted(edges.items())
        for dst in targets
    ]


# ---------------------------------------------------------------- plan


def cmd_plan() -> int:
    corpus = load_corpus()
    questions = load_eval_questions()
    chunks = as_eval_chunks(corpus)
    problems = [
        f"{q.id}: どのチャンクにも無い根拠があります {[e[:20] for e, ids in ev.gold_chunks(q, chunks).items() if not ids]}"
        for q in questions
        if q.answerable and any(not ids for ids in ev.gold_chunks(q, chunks).values())
    ]
    if problems:
        print("正解を決められない質問があります（止めます。評価セットと資料のずれ）:", file=sys.stderr)
        for line in problems:
            print(f"  {line}", file=sys.stderr)
        return 2
    edges = edges_of(corpus)
    if not edges:
        print("辺が 0 本です。この資料では GraphRAG の効果を測れません。", file=sys.stderr)
        return 2
    docs, queries = doc_inputs(corpus), query_inputs(questions)
    chars = sum(len(t) for t in docs.values()) + sum(len(t) for t in queries.values())
    calls = math.ceil(len(docs) / BATCH) + math.ceil(len(queries) / BATCH)
    plan = {
        "created_at": datetime.now(JST).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "note": "検索の結果を見る前に作った。ここに写した値は、この測定の中で変えない。",
        "fixed": {
            **fixed_params(),
            "chunking": "1 条 = 1 チャンク（見出し「第N条（題）」+ 本文）",
            "collection": COLLECTION,
        },
        "corpus_fingerprint": corpus_fingerprint(corpus),
        "chunk_count": len(corpus),
        "sources": sorted({c.source for c in corpus}),
        "edges": describe_edges(corpus, edges),
        "api": {
            "doc_texts": len(docs),
            "query_texts": len(queries),
            "calls": calls,
            "chars": chars,
            "estimated_cost_usd": round(chars * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000, 6),
            "planned_call_limit": MAX_PLANNED_CALLS,
            "hard_call_limit": HARD_CALL_LIMIT,
        },
        "questions": [
            {
                "id": q.id,
                "text": q.question,
                "kind": q.kind,
                "category": q.category,
                "answerable": q.answerable,
                "expected": list(q.expected_doc_ids),
            }
            for q in questions
        ],
    }
    PLAN_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLAN_PATH.write_text(json.dumps(plan, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    answerable = sum(1 for q in questions if q.answerable)
    print(
        f"チャンク {len(corpus)} 件（{len(plan['sources'])} ファイル、1 条 1 チャンク）、辺 {len(plan['edges'])} 本、"
        f"質問 {len(questions)} 問（答えあり {answerable}、資料外 {len(questions) - answerable}）"
    )
    for line in plan["edges"]:
        print(f"  辺: {line}")
    print(f"-> {_shown(PLAN_PATH)}")
    return 0


def check_plan(corpus: Sequence[CorpusChunk], questions: Sequence[Question]) -> dict[str, Any]:
    if not PLAN_PATH.exists():
        raise ev.EvalError("先に plan を実行してください（固定する値を、結果を見る前に写します）")
    plan = json.loads(PLAN_PATH.read_text(encoding="utf-8"))
    current = json.loads(
        json.dumps({**fixed_params(), "chunking": plan["fixed"].get("chunking"), "collection": COLLECTION})
    )
    if plan["fixed"] != current:
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


def cmd_embed(apply: bool) -> int:
    corpus = load_corpus()
    questions = load_eval_questions()
    check_plan(corpus, questions)
    cache = _load_cache()
    docs, queries = doc_inputs(corpus), query_inputs(questions)
    pending = {
        "docs": {h: t for h, t in docs.items() if h not in cache["docs"]},
        "queries": {h: t for h, t in queries.items() if h not in cache["queries"]},
    }
    chars = sum(len(t) for group in pending.values() for t in group.values())
    calls_needed = sum(math.ceil(len(group) / BATCH) for group in pending.values())
    too_long = [h[:8] for group in pending.values() for h, t in group.items() if len(t) > embedding.MAX_INPUT_CHARS]
    print(
        f"資料 {len(corpus)} チャンク、キャッシュ済み {len(docs) - len(pending['docs'])} 件、今回送る {len(pending['docs'])} 件"
    )
    print(
        f"質問 {len(queries)} 件、キャッシュ済み {len(queries) - len(pending['queries'])} 件、今回送る {len(pending['queries'])} 件"
    )
    print(
        f"今回の呼び出し {calls_needed} 回（1 回 {BATCH} 件まで）、{chars} 文字、"
        f"概算費用 約 ${chars * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000:.5f}（1 文字 1 トークンの推計）"
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
    if not any(pending.values()):
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
    remaining = calls_needed
    for kind, group in pending.items():
        items = list(group.items())
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
            except embedding.EmbeddingError as exc:  # 429 を含め、再試行せずに止める（本文・キーは例外に入らない）
                record.update(status="error", error=str(exc), elapsed_s=round(time.perf_counter() - began, 3))
                _save_cache(cache)
                print(f"中断: {exc}", file=sys.stderr)
                return 1
            record.update(status="ok", elapsed_s=round(time.perf_counter() - began, 3))
            for (digest, _), vector in zip(batch, vectors, strict=True):
                cache[kind][digest] = vector
            _save_cache(cache)
            print(f"  {kind}: 送信 {len(texts)} 件、{record['elapsed_s']:.2f} 秒", flush=True)
            remaining -= 1
            if remaining > 0:
                time.sleep(PAUSE_SECONDS)
    cost = cache["chars_sent"] * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000
    print(
        f"完了。累計の送信 {cache['texts_sent']} 件・{cache['calls']} 回・{cache['chars_sent']} 文字、概算費用 約 ${cost:.5f}"
    )
    return 0


# ---------------------------------------------------------------- 測定


def _gold_pages(q: Question, corpus: Sequence[CorpusChunk]) -> set[tuple[str, int]]:
    """質問の根拠を含むチャンクの（資料, 条の番号）。1 条 = 1 チャンクなので、追加された条と突き合わせられる。"""
    chunks = as_eval_chunks(corpus)
    return {(corpus[i].source, corpus[i].page) for ids in ev.gold_chunks(q, chunks).values() for i in ids}


def _false_additions(
    per: dict[str, Any], key: str, questions: Sequence[Question], gold: dict[str, set[tuple[str, int]]]
) -> dict[str, Any]:
    """GraphRAG の追加のうち、質問に要らなかったもの（誤追加）。

    - useful: 参照が要る質問で、追加された条が根拠のチャンクだった数
    - off_target: 参照が要る質問で、追加された条が根拠ではなかった数（本体の上位に別の参照元が入って、その参照先が足された場合など）
    - on_unneeded: 参照が要らない質問（ref_unneeded・single）への追加。根拠は参照元だけなので、追加は全部要らない
    - on_out_of_scope: 資料外の質問への追加
    """
    useful = off_target = 0
    off_target_questions: list[str] = []
    unneeded: dict[str, int] = {}
    out_of_scope: dict[str, int] = {}
    for q in questions:
        extras = per[q.id][key]["graph"]
        if not extras:
            continue
        if not q.answerable:
            out_of_scope[q.id] = len(extras)
        elif q.kind in DEPENDENT_KINDS:
            wrong = [e for e in extras if (e["source"], e["page"]) not in gold[q.id]]
            useful += len(extras) - len(wrong)
            off_target += len(wrong)
            if wrong:
                off_target_questions.append(q.id)
        else:
            unneeded[q.id] = len(extras)
    return {
        "useful_extras_on_dependent": useful,
        "off_target_extras_on_dependent": off_target,
        "off_target_questions": off_target_questions,
        "extras_on_unneeded": {"questions": sorted(unneeded), "extras": sum(unneeded.values())},
        "extras_on_out_of_scope": {"questions": sorted(out_of_scope), "extras": sum(out_of_scope.values())},
    }


def summarize(
    rows: dict[str, dict[str, Any]], questions: Sequence[Question], corpus: Sequence[CorpusChunk] | None = None
) -> dict[str, Any]:
    by_id = {q.id: q for q in questions}
    answerable = [q for q in questions if q.answerable]
    absent = [q for q in questions if not q.answerable]
    gold = {q.id: _gold_pages(q, corpus) for q in answerable} if corpus is not None else None
    groups = {name: [q for q in answerable if q.kind in kinds] for name, kinds in KIND_GROUPS.items()}
    groups["answerable"] = answerable
    summary: dict[str, Any] = {
        "n": {name: len(qs) for name, qs in groups.items()},
        "n_out_of_scope": len(absent),
        "configs": {},
    }
    for name in CONFIGS:
        per = rows[name]
        cfg: dict[str, Any] = {"hit_main": {}, "hit_with_graph": {}, "graph": {}, "out_of_scope_returned": {}}
        for group, qs in groups.items():
            for field in ("hit_main", "hit_with_graph"):
                cfg[field][group] = {str(k): sum(1 for q in qs if per[q.id][str(k)][field]) for k in REPORT_KS}
        for k in REPORT_KS:
            key = str(k)
            with_extra = [q for q in answerable if per[q.id][key]["graph"]]
            cfg["graph"][key] = {
                "questions_with_extra": [q.id for q in with_extra],
                "extras_total": sum(len(per[q.id][key]["graph"]) for q in answerable),
                "extra_made_it_hit": [
                    q.id for q in with_extra if per[q.id][key]["hit_with_graph"] and not per[q.id][key]["hit_main"]
                ],
                "extra_but_already_hit": [q.id for q in with_extra if per[q.id][key]["hit_main"]],
                "extra_but_still_miss": [q.id for q in with_extra if not per[q.id][key]["hit_with_graph"]],
                "out_of_scope_with_extra": [q.id for q in absent if per[q.id][key]["graph"]],
                "extra_by_kind": {
                    kind: sum(1 for q in with_extra if by_id[q.id].kind == kind)
                    for kind in sorted({q.kind or "-" for q in answerable})
                },
            }
            cfg["out_of_scope_returned"][key] = sum(1 for q in absent if per[q.id][key]["n_main"] > 0)
            cfg["mean_returned"] = cfg.get("mean_returned", {})
            cfg["mean_returned"][key] = round(
                sum(per[q.id][key]["n_main"] + len(per[q.id][key]["graph"]) for q in questions) / len(questions), 3
            )
            if gold is not None:
                cfg.setdefault("false_additions", {})[key] = _false_additions(per, key, questions, gold)
        summary["configs"][name] = cfg
    # GraphRAG あり・なしの質問ごとの比較（同じ構成から graph だけを足した組）。悪化・本体の変化が無いことの確認
    summary["graph_vs_without"] = {}
    for without, with_graph in (("hybrid", "hybrid+graph"), ("hybrid+rerank", "all")):
        per_k: dict[str, Any] = {}
        for k in REPORT_KS:
            key = str(k)
            a, b = rows[without], rows[with_graph]
            per_k[key] = {
                "gained": [q.id for q in answerable if not a[q.id][key]["hit_main"] and b[q.id][key]["hit_with_graph"]],
                "lost": [q.id for q in answerable if a[q.id][key]["hit_main"] and not b[q.id][key]["hit_with_graph"]],
                "main_changed": [
                    q.id
                    for q in questions
                    if a[q.id][key]["n_main"] != b[q.id][key]["n_main"]
                    or a[q.id][key]["origins"] != b[q.id][key]["origins"]
                    or a[q.id][key].get("hit_main") != b[q.id][key].get("hit_main")
                ],
            }
        summary["graph_vs_without"][f"{without} -> {with_graph}"] = per_k
    return summary


def _pct(hit: int, n: int) -> str:
    return f"{hit}/{n}" if n else "-"


def print_table(summary: dict[str, Any]) -> None:
    n = summary["n"]
    ks = [str(k) for k in REPORT_KS]
    for field, title in (("hit_main", "本体の上位 k 件だけ"), ("hit_with_graph", "上位 k 件 + GraphRAG の追加")):
        print(f"\n[{title}] 根拠が全部そろった質問の数")
        for group in ("dependent", "ref_far", "ref_near", "chain3", "ref_unneeded", "single", "answerable"):
            print(f"  {group}（{n[group]} 問）")
            print(f"    {'構成':14} " + " ".join(f"{'k=' + k:>6}" for k in ks))
            for name in CONFIGS:
                if field == "hit_with_graph" and "graph" not in name and name != "all":
                    continue
                hits = summary["configs"][name][field][group]
                print(f"    {name:14} " + " ".join(f"{_pct(hits[k], n[group]):>6}" for k in ks))


def print_additions(summary: dict[str, Any]) -> None:
    """GraphRAG の追加の内訳（誤追加）と、あり・なしの質問ごとの比較。`false_additions` が無い（corpus なしの）要約では何も出さない。"""
    n = summary["n"]
    for name in ("hybrid+graph", "all"):
        cfg = summary["configs"][name]
        if "false_additions" not in cfg:
            return
        without = "hybrid" if name == "hybrid+graph" else "hybrid+rerank"
        print(
            f"\n[{name}] GraphRAG の追加の内訳（参照が要る {n['dependent']} 問、要らない {n['ref_unneeded'] + n['single']} 問、資料外 {summary['n_out_of_scope']} 問）"
        )
        print(
            "    k  追加が根拠だった / 根拠でなかった（問数）  参照が不要な質問への追加（問数）  資料外への追加（問数）  返却数の平均 なし→あり"
        )
        for k in REPORT_KS:
            key = str(k)
            fa = cfg["false_additions"][key]
            print(
                f"  {k:>3}  {fa['useful_extras_on_dependent']:>8} / {fa['off_target_extras_on_dependent']:>3}（{len(fa['off_target_questions'])} 問）"
                f"  {fa['extras_on_unneeded']['extras']:>18}（{len(fa['extras_on_unneeded']['questions'])} 問）"
                f"  {fa['extras_on_out_of_scope']['extras']:>12}（{len(fa['extras_on_out_of_scope']['questions'])} 問）"
                f"  {summary['configs'][without]['mean_returned'][key]:>6.2f} → {cfg['mean_returned'][key]:.2f}"
            )
    print("\nGraphRAG あり・なしの質問ごとの比較（なしで根拠がそろっていたのに、ありで欠けた質問 = 悪化）")
    for pair, per_k in summary["graph_vs_without"].items():
        for k in REPORT_KS:
            row = per_k[str(k)]
            print(
                f"  {pair:24} k={k}: 増えた {len(row['gained'])} 問、悪化 {len(row['lost'])} 問、本体の結果が変わった {len(row['main_changed'])} 問"
            )


def cmd_run() -> int:
    if RESULT_PATH.exists():
        raise ev.EvalError(f"{_shown(RESULT_PATH)} が既にあります（上書きしません。退避してから実行してください）")
    corpus = load_corpus()
    questions = load_eval_questions()
    plan = check_plan(corpus, questions)
    cache = _load_cache()
    missing_docs = [h for h in doc_inputs(corpus) if h not in cache["docs"]]
    missing_queries = [h for h in query_inputs(questions) if h not in cache["queries"]]
    if missing_docs or missing_queries:
        raise ev.EvalError(
            f"埋め込みがキャッシュにありません（資料 {len(missing_docs)} 件、質問 {len(missing_queries)} 件。embed --apply が要ります）"
        )
    index = VectorIndex(corpus, [cache["docs"][_sha(c.doc_input)] for c in corpus], dict(cache["queries"]))

    names = ("SEARCH_HYBRID", "SEARCH_RERANK", "SEARCH_GRAPH", "SEARCH_CORPUS_TTL_SECONDS")
    saved_env = {name: os.environ.get(name) for name in names}
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
    summary = summarize(measured["rows"], questions, corpus)
    sent = cache["chars_sent"]
    report = {
        "set": CORPUS_DIR.name,
        "generated_at": datetime.now(JST).isoformat(timespec="seconds"),
        "git_head": _git_head(),
        "plan_sha256": hashlib.sha256(PLAN_PATH.read_bytes()).hexdigest(),
        "fixed": plan["fixed"],
        "chunk_count": len(corpus),
        "edges": plan["edges"],
        "api_usage": {
            "texts_sent": cache["texts_sent"],
            "calls": cache["calls"],
            "failed_calls": sum(1 for e in cache["log"] if e.get("status") != "ok"),
            "chars_sent": sent,
            "estimated_cost_usd": round(sent * embedding.PRICE_PER_MILLION_TOKENS / 1_000_000, 6),
        },
        "log_check": measured["log"],
        "summary": summary,
        "rows": measured["rows"],
    }
    RESULT_PATH.parent.mkdir(parents=True, exist_ok=True)
    RESULT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print_table(summary)
    print_additions(summary)
    print("\nログ:", {k: v for k, v in measured["log"].items() if k.startswith("lines")})
    print("API:", report["api_usage"])
    print(f"-> {_shown(RESULT_PATH)}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GraphRAG の評価（API は embed --apply だけ。キーは環境変数から）")
    parser.add_argument(
        "--set", dest="set_name", choices=sorted(SETS), default="graph_rules", help="評価セット（既定: graph_rules）"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    emb = sub.add_parser("embed")
    emb.add_argument("--apply", action="store_true")
    sub.add_parser("run")
    args = parser.parse_args(argv)
    try:
        use_set(args.set_name)
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
