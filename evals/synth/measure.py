"""GPU ジョブの実測の集計と、件数を増やしたときの見積もりの補正。

測るもの（rounds.jsonl の回ごとの記録から）: 起動込みの所要時間、モデルの読み込み時間、1 件あたりの生成時間、トークン数、
1 回目の合格率、費用（所要時間 × 時間単価）、**合格 PDF あたりの費用**。
見積もりは、測った 1 件あたりの生成時間と、回ごとの固定時間（起動 + 読み込み + 書き出し）から組み立てる。
注意: 8 件の測定は、バッチが小さく GPU を使い切らない。大きなバッチの生成は 1 件あたりが速くなりうるので、
`throughput_gain` = 1.0（測定のまま）は、1,000 件の見積もりでは高めに出る（安全側）。実際の倍率は、件数を増やして測らないと分からない。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class Measured:
    rounds: int
    requests: int
    results: int
    accepted: int  # 合格（受け入れ）した資料の数
    first_pass_rate: float
    job_seconds: float
    overhead_seconds_per_round: float  # 回ごとの固定時間（起動 + 読み込み + 書き出し）。job − generate
    load_seconds_per_round: float | None
    gen_seconds_per_request: float
    prompt_tokens_per_request: float
    completion_tokens_per_request: float
    cost_usd: float
    usd_per_accepted: float | None
    includes_container_start: bool  # job_seconds が Cloud Run の実行の時間（起動を含む）か


def measure(rounds: list[dict[str, Any]], accepted: int) -> Measured:
    if not rounds:
        raise ValueError("回の記録がありません（ingest を先に実行する）")
    results = sum(int(r.get("results", 0)) for r in rounds)
    job_seconds = sum(float(r.get("job_seconds", 0.0)) for r in rounds)
    gen = sum(float(r.get("generate_seconds") or 0.0) for r in rounds)
    loads = [float(r["load_seconds"]) for r in rounds if r.get("load_seconds") is not None]
    first = rounds[0]
    cost = sum(float(r.get("cost_usd", 0.0)) for r in rounds)
    return Measured(
        rounds=len(rounds),
        requests=sum(int(r.get("requests", 0)) for r in rounds),
        results=results,
        accepted=accepted,
        first_pass_rate=(int(first.get("accepted", 0)) / int(first["requests"]))
        if int(first.get("requests", 0))
        else 0.0,
        job_seconds=job_seconds,
        overhead_seconds_per_round=(job_seconds - gen) / len(rounds),
        load_seconds_per_round=(sum(loads) / len(loads)) if loads else None,
        gen_seconds_per_request=(gen / results) if results else 0.0,
        prompt_tokens_per_request=(sum(int(r.get("prompt_tokens", 0)) for r in rounds) / results) if results else 0.0,
        completion_tokens_per_request=(sum(int(r.get("completion_tokens", 0)) for r in rounds) / results)
        if results
        else 0.0,
        cost_usd=cost,
        usd_per_accepted=(cost / accepted) if accepted else None,
        includes_container_start=all(str(r.get("job_seconds_source")) == "given" for r in rounds),
    )


@dataclass(frozen=True)
class Estimate:
    docs: int
    expected_accepted: float
    requests: float
    jobs: int  # モデルを読み込む回数（タスク数の合計）
    seconds: float
    cost_usd: float
    usd_per_accepted: float | None


def estimate(
    m: Measured,
    docs: int,
    *,
    usd_per_hour: float,
    task_timeout_seconds: float,
    max_attempts: int,
    throughput_gain: float = 1.0,
) -> Estimate:
    """`docs` 件を作るときの、時間・費用・ジョブ数。落ちた分だけを次の回に回す（再試行の合格率も、1 回目と同じと仮定する）。"""
    usable = task_timeout_seconds - m.overhead_seconds_per_round
    if usable <= 0:
        raise ValueError("task-timeout が、固定時間（起動 + 読み込み）より短いです")
    remaining, seconds, requests, jobs, accepted = float(docs), 0.0, 0.0, 0, 0.0
    for _ in range(max_attempts):
        if remaining < 0.5:
            break
        gen = remaining * m.gen_seconds_per_request / throughput_gain
        shards = max(math.ceil(gen / usable), 1)  # 1 つのタスクが task-timeout に収まるよう、分ける
        seconds += shards * m.overhead_seconds_per_round + gen
        jobs += shards
        requests += remaining
        passed = remaining * m.first_pass_rate
        accepted += passed
        remaining -= passed
    cost = seconds / 3600 * usd_per_hour
    return Estimate(docs, accepted, requests, jobs, seconds, cost, (cost / accepted) if accepted else None)


def gemini_batch_cost(m: Measured, requests: float, *, price_in_per_m: float, price_out_per_m: float) -> float:
    """同じ件数の依頼を Gemini のバッチで流したときの費用（同じトークン数と仮定。品質の差・再試行の差は含まない）。"""
    return (
        requests
        * (m.prompt_tokens_per_request * price_in_per_m + m.completion_tokens_per_request * price_out_per_m)
        / 1e6
    )


def _money(value: float | None) -> str:
    return "—" if value is None else f"${value:,.2f}"


def format_report(
    m: Measured,
    *,
    usd_per_hour: float,
    task_timeout_seconds: float,
    max_attempts: int,
    docs: list[int],
    gains: list[float],
    gemini_prices: tuple[float, float] | None,
) -> str:
    lines = [
        "## 実測",
        f"- 回数 {m.rounds}、依頼 {m.requests} 件、結果 {m.results} 件、合格 {m.accepted} 件（1 回目の合格率 {m.first_pass_rate:.0%}）",
        f"- 所要時間 {m.job_seconds:,.0f} 秒"
        + (
            "（起動を含む）"
            if m.includes_container_start
            else "（**コンテナの起動・イメージの取得を含まない**: `--job-seconds` で実行の時間を渡すと補正される）"
        ),
        f"- 回ごとの固定時間 {m.overhead_seconds_per_round:,.0f} 秒"
        + (
            f"（うちモデルの読み込み {m.load_seconds_per_round:,.0f} 秒）"
            if m.load_seconds_per_round is not None
            else ""
        ),
        f"- 1 件あたりの生成 {m.gen_seconds_per_request:.1f} 秒、入力 {m.prompt_tokens_per_request:,.0f}・出力 {m.completion_tokens_per_request:,.0f} トークン",
        f"- 費用 {_money(m.cost_usd)}（時間単価 ${usd_per_hour}/時）、**合格 PDF あたり {_money(m.usd_per_accepted)}**",
        "",
        "## 見積もり（測定から補正。再試行の合格率は 1 回目と同じと仮定）",
        "| 件数 | 倍率 | 合格の見込み | 依頼 | ジョブ数 | 所要時間 | 費用 | 合格 PDF あたり |"
        + (" Gemini バッチ（同トークン） |" if gemini_prices else ""),
        "|---|---|---|---|---|---|---|---|" + ("---|" if gemini_prices else ""),
    ]
    for n in docs:
        for g in gains:
            e = estimate(
                m,
                n,
                usd_per_hour=usd_per_hour,
                task_timeout_seconds=task_timeout_seconds,
                max_attempts=max_attempts,
                throughput_gain=g,
            )
            row = (
                f"| {n:,} | ×{g:g} | {e.expected_accepted:,.0f} | {e.requests:,.0f} | {e.jobs} | {e.seconds / 3600:.1f} 時間 | "
                f"{_money(e.cost_usd)} | {_money(e.usd_per_accepted)} |"
            )
            if gemini_prices:
                row += f" {_money(gemini_batch_cost(m, e.requests, price_in_per_m=gemini_prices[0], price_out_per_m=gemini_prices[1]))} |"
            lines.append(row)
    return "\n".join(lines)
