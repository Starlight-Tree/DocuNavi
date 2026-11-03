"""同時数の比較（ベンチ）。同じ依頼・同じ出力上限で、同時数 4・8・16 を別々の GPU ジョブとして流し、結果を比べる。

本番の台帳（ledger.jsonl）には触れない。比べるのは、全件が終わるまでの時間・不合格の数・エラー（メモリ不足など）・
**合格した文書あたりの費用**。ベンチの本文は台帳に入れない（比較専用）。

  bench-prepare   同じ依頼を `bench/requests.jsonl` に書く（全部の試行で共有する。違う入力では比べられない）
  ── GPU ジョブ（同時数ごとに 1 回。`--max-num-seqs` だけ変える）→ `bench/results.<label>.jsonl` と `.meta.json` ──
  bench-ingest    1 つの試行の結果を検証し、集計を `bench/trials.jsonl` に追記する（CPU）
  bench-report    試行を並べ、同時数の推奨を出す

推奨の規則（事前に決めておく）: エラー・欠落のある試行は除く。残りのうち、不合格の数が最小の同時数の試行より増えないものを候補にし、
候補の中で合格文書あたりの費用が最小のものを選ぶ。費用の差が tolerance（既定 5%）以内なら、同時数が小さい方を選ぶ
（メモリ不足・効率悪化のときに下げる、という方針を、そのまま規則にした）。
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

from evals.synth.bible import Bible
from evals.synth.ledger import atomic_write
from evals.synth.pipeline import check_out_dir
from evals.synth.plan import Brief
from evals.synth.validate import find_duplicate, shingles, validate_body

BENCH_DIR = "bench"


def requests_file(out_dir: Path) -> Path:
    return out_dir / BENCH_DIR / "requests.jsonl"


def results_file(out_dir: Path, label: str) -> Path:
    return out_dir / BENCH_DIR / f"results.{label}.jsonl"


def meta_file(out_dir: Path, label: str) -> Path:
    return out_dir / BENCH_DIR / f"results.{label}.meta.json"


def trials_file(out_dir: Path) -> Path:
    return out_dir / BENCH_DIR / "trials.jsonl"


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class BenchError(ValueError):
    pass


def prepare_bench(out_dir: Path, requests: list[dict[str, Any]], *, force: bool = False) -> Path:
    out_dir = check_out_dir(out_dir)
    path = requests_file(out_dir)
    data = "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in requests).encode()
    if path.exists() and path.read_bytes() != data and not force:
        raise BenchError(
            "bench/requests.jsonl が、今回の入力と違います（比較の途中で入力を変えない。作り直すなら --force）"
        )
    atomic_write(path, data)
    return path


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[min(int(q * len(ordered)), len(ordered) - 1)]


def evaluate_trial(
    *,
    out_dir: Path,
    bibles: dict[str, Bible],
    briefs: list[Brief],
    label: str,
    usd_per_hour: float,
    job_seconds: float | None,
    dedupe_threshold: float,
    reference_texts: dict[str, str],
) -> dict[str, Any]:
    out_dir = check_out_dir(out_dir)
    req_path, res_path, m_path = requests_file(out_dir), results_file(out_dir, label), meta_file(out_dir, label)
    if not req_path.exists() or not res_path.exists():
        raise FileNotFoundError(f"依頼か結果がありません: {req_path} / {res_path}")
    meta: dict[str, Any] = json.loads(m_path.read_text(encoding="utf-8")) if m_path.exists() else {}
    requests = _read_jsonl(req_path)
    results = {r["id"]: r for r in _read_jsonl(res_path)}
    by_id = {b.id: b for b in briefs}

    seen = {k: shingles(v) for k, v in reference_texts.items()}
    codes: Counter[str] = Counter()
    valid = invalid = missing = 0
    latencies: list[float] = []
    for req in sorted(requests, key=lambda r: r["id"]):
        res = results.get(req["id"])
        if res is None:
            missing += 1
            continue
        if "latency_seconds" in res:
            latencies.append(float(res["latency_seconds"]))
        brief = by_id[req["id"]]
        bible = bibles[brief.target]
        kind = next(k for k in bible.kinds if k.id == brief.kind)
        text = str(res.get("text", ""))
        issues = validate_body(text, bible, min_chars=kind.min_chars, max_chars=kind.max_chars)
        problems = {i.code for i in issues}
        if res.get("finish_reason") == "length":
            problems.add("truncated")
        if not problems:
            sig = shingles(text)
            if find_duplicate(sig, seen, dedupe_threshold):
                problems.add("duplicate")
            else:
                seen[req["id"]] = sig
        if problems:
            invalid += 1
            codes.update(problems)
        else:
            valid += 1

    given = job_seconds is not None
    seconds = job_seconds if given else float(meta.get("total_seconds", 0.0))
    generate = float(meta.get("generate_seconds") or 0.0)
    completion = int(meta.get("completion_tokens", 0))
    cost = seconds / 3600 * usd_per_hour
    engine = meta.get("engine") or {}
    record = {
        "label": label,
        "concurrency": engine.get("max_num_seqs"),
        "requests": len(requests),
        "requests_sha256": sha256_file(req_path),
        "max_tokens": max((int(r.get("max_tokens", 0)) for r in requests), default=0),
        "results": len(results),
        "valid": valid,
        "invalid": invalid,
        "missing": missing,
        "invalid_codes": dict(codes),
        "error": meta.get("error"),
        "stopped": meta.get("stopped"),
        "load_seconds": meta.get("load_seconds"),
        "generate_seconds": generate,
        "job_seconds": seconds,
        "job_seconds_given": given,
        "completion_tokens": completion,
        "tokens_per_second": (completion / generate) if generate else None,
        "latency_p50": _percentile(latencies, 0.5),
        "latency_p95": _percentile(latencies, 0.95),
        "usd_per_hour": usd_per_hour,
        "cost_usd": cost,
        "usd_per_valid": (cost / valid) if valid else None,
        "model": meta.get("model"),
        "engine": engine,
    }
    path = trials_file(out_dir)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
    return record


def latest_trials(out_dir: Path) -> list[dict[str, Any]]:
    path = trials_file(out_dir)
    if not path.exists():
        raise FileNotFoundError(f"試行の記録がありません: {path}")
    by_label: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(path):
        by_label[row["label"]] = row  # 同じラベルは、最後の記録だけ
    return sorted(by_label.values(), key=lambda r: (r.get("concurrency") or 0, r["label"]))


def recommend(trials: list[dict[str, Any]], tolerance: float = 0.05) -> tuple[str | None, str]:
    usable = [t for t in trials if not t["error"] and t["missing"] == 0 and t["valid"] > 0]
    excluded = [t["label"] for t in trials if t not in usable]
    note = f"（除外: {', '.join(excluded)}。エラー・欠落・合格 0）" if excluded else ""
    if not usable:
        return None, "使える試行がありません" + note
    fewest_invalid = min(t["invalid"] for t in usable)
    candidates = [t for t in usable if t["invalid"] <= fewest_invalid]
    best = min(t["usd_per_valid"] for t in candidates)
    near = [t for t in candidates if t["usd_per_valid"] <= best * (1 + tolerance)]
    pick = min(near, key=lambda t: (t.get("concurrency") or 0, t["label"]))
    return pick["label"], (
        f"不合格が最小（{fewest_invalid} 件）の試行のうち、合格 1 件あたりの費用が最小の ${best:.3f} から {tolerance:.0%} 以内で、"
        f"同時数が最も小さいもの{note}"
    )


def _fmt(value: float | None, spec: str = ".0f") -> str:
    return "—" if value is None else format(value, spec)


def format_report(trials: list[dict[str, Any]], tolerance: float = 0.05) -> str:
    warnings = []
    if len({t["requests_sha256"] for t in trials}) > 1:
        warnings.append("**警告: 試行ごとに依頼が違います。同じ入力ではないので、比較できません。**")
    if len({t["max_tokens"] for t in trials}) > 1:
        warnings.append("**警告: 出力の上限（max_tokens）が試行ごとに違います。**")
    if len({t["usd_per_hour"] for t in trials}) > 1:
        warnings.append("**警告: 時間単価が試行ごとに違います。**")
    lines = [
        *warnings,
        "| ラベル | 同時数 | 依頼 | 合格 | 不合格 | 欠落 | エラー | 読み込み(s) | 生成(s) | 全体(s) | 出力tok/s | 1件の遅延 p50/p95(s) | 費用 | 合格1件あたり |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in trials:
        suffix = "" if t["job_seconds_given"] else "*"
        lines.append(
            f"| {t['label']} | {t['concurrency'] if t['concurrency'] is not None else '—'} | {t['requests']} | {t['valid']} | {t['invalid']} | "
            f"{t['missing']} | {t['error'] or '—'} | {_fmt(t['load_seconds'])} | {_fmt(t['generate_seconds'])} | {_fmt(t['job_seconds'])}{suffix} | "
            f"{_fmt(t['tokens_per_second'])} | {_fmt(t['latency_p50'], '.1f')} / {_fmt(t['latency_p95'], '.1f')} | "
            f"${t['cost_usd']:.2f} | {'—' if t['usd_per_valid'] is None else '$' + format(t['usd_per_valid'], '.3f')} |"
        )
    if any(not t["job_seconds_given"] for t in trials):
        lines.append(
            "\n\\* 全体の時間が、プロセス内の経過時間です（コンテナの起動・イメージの取得を含まない）。`--job-seconds` で実行の時間を渡すと補正されます。"
        )
    if any(t["invalid_codes"] for t in trials):
        lines.append(
            "\n不合格の理由（試行ごと）: "
            + "; ".join(f"{t['label']}: {t['invalid_codes']}" for t in trials if t["invalid_codes"])
        )
    pick, why = recommend(trials, tolerance)
    lines.append(f"\n推奨: **{pick or 'なし'}** — {why}")
    return "\n".join(lines)
