"""一括の経路（第一候補）の CPU 側。GPU ジョブ（gpu_worker.py）へ渡す依頼を作り、返ってきた結果を取り込む。

  prepare   台帳を見て、まだ要る依頼を `requests/round-NN.jsonl` に書く（CPU・ローカル）
  ──────── GPU ジョブ: モデルを 1 回読み込み、依頼を全部生成し、`results/round-NN.jsonl` と `.meta.json` を書いて終了 ────────
  ingest    結果を検証・重複判定・受け入れる（CPU）。落ちたものは台帳に `retry` で残り、次の prepare が次の回の依頼にする

GPU に載せるのは生成だけ。検証・PDF 化・マニフェストは、すべて CPU 側（この経路の外）で行う。
再試行は「回（round）」で数える: 1 つの資料の試行は `max_attempts` まで。落ちた資料だけを次の回に回す（回ごとにモデルを読み込み直す）。
`prepare` は、台帳の `queued`（依頼済みで結果が未取り込み）の資料を、`requeue_queued` なしでは再び依頼しない（重複した GPU 実行を防ぐ）。
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from evals.synth.bible import Bible
from evals.synth.budget import BudgetExceeded
from evals.synth.ledger import Ledger, atomic_write, exclusive
from evals.synth.pipeline import Acceptor, check_out_dir, classify, md_path, reference_shingles, total_spent_usd
from evals.synth.plan import Brief
from evals.synth.prompt import build_messages
from evals.synth.validate import shingles

_ROUND_FILE = re.compile(r"^round-(\d+)\.jsonl$")


def requests_path(out_dir: Path, round_no: int) -> Path:
    return out_dir / "requests" / f"round-{round_no:02d}.jsonl"


def results_path(out_dir: Path, round_no: int) -> Path:
    return out_dir / "results" / f"round-{round_no:02d}.jsonl"


def meta_path(out_dir: Path, round_no: int) -> Path:
    return out_dir / "results" / f"round-{round_no:02d}.meta.json"


def next_round(out_dir: Path) -> int:
    rounds = [int(m.group(1)) for p in (out_dir / "requests").glob("round-*.jsonl") if (m := _ROUND_FILE.match(p.name))]
    return max(rounds, default=0) + 1


@dataclass
class PrepareConfig:
    out_dir: Path
    bibles: dict[str, Bible]
    briefs: list[Brief]
    max_attempts: int = 2  # 1 つの資料の試行の上限（= 回の上限）
    max_requests: int = 8  # 1 回の GPU ジョブに入れる依頼の上限
    max_tokens_per_doc: int = 3000
    retry_failed: bool = False
    regenerate_stale: bool = False
    requeue_queued: bool = False
    # 費用の事前の見積もり（上限に収まる件数まで依頼を減らす）。単価が 0 なら見積もらない
    usd_per_hour: float = 0.0
    assumed_tokens_per_second: float = 300.0  # 未測定の仮定。測定後に差し替える
    assumed_overhead_seconds: float = 900.0  # 起動 + モデルの読み込み + 書き出しの仮定
    max_total_cost_usd: float | None = None


@dataclass
class PrepareReport:
    round_no: int = 0
    path: Path | None = None
    requests: int = 0
    already_done: list[str] = field(default_factory=list)
    stale: list[str] = field(default_factory=list)
    skipped_failed: list[str] = field(default_factory=list)
    skipped_queued: list[str] = field(default_factory=list)
    cut_by_limit: list[str] = field(default_factory=list)
    projected_seconds: float = 0.0
    projected_cost_usd: float = 0.0
    spent_usd: float = 0.0


def make_request(bibles: dict[str, Bible], brief: Brief, attempt: int, max_tokens: int) -> dict[str, Any]:
    system, user = build_messages(bibles[brief.target], brief)
    return {
        "id": brief.id,
        "attempt": attempt,
        "seed": brief.seed + attempt - 1,
        "key": brief.key,
        "system": system,
        "user": user,
        "max_tokens": max_tokens,
    }


def allowed_requests(cfg: PrepareConfig, spent_usd: float) -> int:
    """費用の上限に収まる依頼の最大数（単価・上限が無ければ制限なし）。0 なら GPU を起動する価値がない。"""
    if cfg.max_total_cost_usd is None or cfg.usd_per_hour <= 0:
        return cfg.max_requests
    remaining_seconds = max(cfg.max_total_cost_usd - spent_usd, 0.0) / cfg.usd_per_hour * 3600
    usable = remaining_seconds - cfg.assumed_overhead_seconds
    if usable <= 0:
        return 0
    per_request = cfg.max_tokens_per_doc / cfg.assumed_tokens_per_second
    return min(cfg.max_requests, int(usable // per_request))


def prepare_round(cfg: PrepareConfig) -> PrepareReport:
    out_dir = check_out_dir(cfg.out_dir)
    report = PrepareReport()
    with exclusive(out_dir / ".lock"):
        ledger = Ledger(out_dir / "ledger.jsonl")
        report.spent_usd = total_spent_usd(out_dir, ledger)
        todo: list[tuple[Brief, int]] = []
        for brief in cfg.briefs:
            kind = classify(
                brief, ledger, out_dir, retry_failed=cfg.retry_failed, regenerate_stale=cfg.regenerate_stale
            )
            if kind == "queued" and cfg.requeue_queued:
                kind = "todo"
            bucket = {
                "done": report.already_done,
                "stale": report.stale,
                "failed": report.skipped_failed,
                "queued": report.skipped_queued,
            }.get(kind)
            if bucket is not None:
                bucket.append(brief.id)
                continue
            state = ledger.get(brief.id)
            used = (
                int(state.get("attempts", 0))
                if state.get("key") == brief.key and state.get("status") != "failed"
                else 0
            )
            if used >= cfg.max_attempts:
                report.skipped_failed.append(brief.id)
                continue
            todo.append((brief, used + 1))

        limit = allowed_requests(cfg, report.spent_usd)
        if limit <= 0 and todo:
            raise BudgetExceeded("max-total-cost-usd（見積もりの起動費用だけで上限を超える）")
        chosen, report.cut_by_limit = todo[:limit], [b.id for b, _ in todo[limit:]]
        if not chosen:
            return report

        round_no = next_round(out_dir)
        lines = [
            json.dumps(make_request(cfg.bibles, b, a, cfg.max_tokens_per_doc), ensure_ascii=False) for b, a in chosen
        ]
        path = requests_path(out_dir, round_no)
        atomic_write(path, ("\n".join(lines) + "\n").encode())
        for brief, attempt in chosen:
            ledger.append(
                brief.id,
                status="queued",
                key=brief.key,
                target=brief.target,
                queued_round=round_no,
                queued_attempt=attempt,
            )
        report.round_no, report.path, report.requests = round_no, path, len(chosen)
        if cfg.usd_per_hour > 0:
            report.projected_seconds = (
                cfg.assumed_overhead_seconds + len(chosen) * cfg.max_tokens_per_doc / cfg.assumed_tokens_per_second
            )
            report.projected_cost_usd = report.projected_seconds / 3600 * cfg.usd_per_hour
    return report


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # 書きかけの末尾の行
    return rows


def recorded_rounds(out_dir: Path) -> list[dict[str, Any]]:
    path = out_dir / "rounds.jsonl"
    return _read_jsonl(path) if path.exists() else []


@dataclass
class IngestConfig:
    out_dir: Path
    bibles: dict[str, Bible]
    briefs: list[Brief]
    round_no: int
    max_attempts: int = 2
    dedupe_threshold: float = 0.5
    usd_per_hour: float = 0.0
    job_seconds: float | None = (
        None  # Cloud Run の実行の開始〜終了（起動を含む）。無ければ meta の経過時間（起動前を含まない）
    )
    reference_texts: dict[str, str] = field(default_factory=dict)


@dataclass
class IngestReport:
    round_no: int
    accepted: list[str] = field(default_factory=list)
    rejected: list[str] = field(default_factory=list)  # 検証で落ち、次の回で再試行できるもの
    failed: list[str] = field(default_factory=list)  # 試行の上限に達して、これ以上作らないもの
    lost: list[str] = field(default_factory=list)  # 結果が無かったもの（台帳では queued のまま）
    ignored: list[str] = field(default_factory=list)  # bible の版が変わっていた・既に取り込み済み
    rejected_codes: dict[str, int] = field(default_factory=dict)
    round_record: dict[str, Any] = field(default_factory=dict)


def ingest_round(cfg: IngestConfig) -> IngestReport:
    out_dir = check_out_dir(cfg.out_dir)
    report = IngestReport(cfg.round_no)
    req_path, res_path = requests_path(out_dir, cfg.round_no), results_path(out_dir, cfg.round_no)
    if not req_path.exists() or not res_path.exists():
        raise FileNotFoundError(f"依頼か結果がありません: {req_path} / {res_path}")
    meta: dict[str, Any] = {}
    if meta_path(out_dir, cfg.round_no).exists():
        meta = json.loads(meta_path(out_dir, cfg.round_no).read_text(encoding="utf-8"))
    requests = {r["id"]: r for r in _read_jsonl(req_path)}
    results = {r["id"]: r for r in _read_jsonl(res_path)}
    briefs = {b.id: b for b in cfg.briefs}

    with exclusive(out_dir / ".lock"):
        ledger = Ledger(out_dir / "ledger.jsonl")
        seen = reference_shingles(cfg.reference_texts)
        for state in ledger.state.values():
            if state.get("status") == "accepted" and state.get("md") and (out_dir / state["md"]).exists():
                seen[state["id"]] = shingles((out_dir / state["md"]).read_text(encoding="utf-8"))
        acceptor = Acceptor(
            out_dir,
            cfg.bibles,
            ledger,
            seen,
            threshold=cfg.dedupe_threshold,
            backend_name="vllm-batch",
            model=str(meta.get("model", "?")),
        )
        codes: Counter[str] = Counter()
        prompt_tokens = completion_tokens = 0
        for doc_id, req in sorted(requests.items()):
            brief, state = briefs.get(doc_id), ledger.get(doc_id)
            if (
                brief is None
                or brief.key != req["key"]
                or state.get("status") != "queued"
                or state.get("queued_round") != cfg.round_no
            ):
                report.ignored.append(doc_id)
                continue
            res = results.get(doc_id)
            if res is None:
                report.lost.append(doc_id)
                continue
            prompt_tokens += int(res.get("prompt_tokens", 0))
            completion_tokens += int(res.get("completion_tokens", 0))
            acceptor.forget(doc_id)
            issues = acceptor.judge(
                brief,
                attempt=int(req["attempt"]),
                text=str(res.get("text", "")),
                finish_reason=str(res.get("finish_reason", "stop")),
                prompt_tokens=int(res.get("prompt_tokens", 0)),
                completion_tokens=int(res.get("completion_tokens", 0)),
                cost_usd=0.0,  # 費用は GPU ジョブ単位（rounds.jsonl）。資料ごとには割らない
                system=req["system"],
                user=req["user"],
            )
            if not issues:
                report.accepted.append(doc_id)
                continue
            codes.update({i.code for i in issues})
            if int(req["attempt"]) >= cfg.max_attempts:
                ledger.append(doc_id, status="failed")
                report.failed.append(doc_id)
            else:
                report.rejected.append(doc_id)
        report.rejected_codes = dict(codes)

        if any(r.get("round") == cfg.round_no for r in recorded_rounds(out_dir)):
            return report  # 同じ回の記録は 1 回だけ（二重取り込みで費用を数え直さない）
        job_seconds = cfg.job_seconds if cfg.job_seconds is not None else float(meta.get("total_seconds", 0.0))
        report.round_record = {
            "round": cfg.round_no,
            "requests": len(requests),
            "results": len(results),
            "accepted": len(report.accepted),
            "rejected": len(report.rejected),
            "failed": len(report.failed),
            "rejected_codes": report.rejected_codes,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "model": meta.get("model"),
            "load_seconds": meta.get("load_seconds"),
            "generate_seconds": meta.get("generate_seconds"),
            "meta_total_seconds": meta.get("total_seconds"),
            "stopped": meta.get("stopped"),
            "job_seconds": job_seconds,
            "job_seconds_source": "given"
            if cfg.job_seconds is not None
            else "meta（コンテナの起動・イメージの取得を含まない）",
            "usd_per_hour": cfg.usd_per_hour,
            "cost_usd": job_seconds / 3600 * cfg.usd_per_hour,
        }
        with open(out_dir / "rounds.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(report.round_record, ensure_ascii=False, sort_keys=True) + "\n")
    return report


def accepted_count(out_dir: Path) -> int:
    ledger = Ledger(out_dir / "ledger.jsonl")
    return sum(
        1
        for s in ledger.state.values()
        if s.get("status") == "accepted" and md_path(out_dir, s["target"], s["id"]).exists()
    )
