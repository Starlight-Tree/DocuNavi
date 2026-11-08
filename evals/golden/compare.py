"""golden set の 2 つの結果（変更前 = baseline、変更後 = current）を比べ、回帰を検出する。API は呼ばない。

    python -m evals.golden.compare evals/golden/results/baseline.json evals/golden/results/after.json

回帰として終了コード 1 にするもの:
- baseline で全回とも通っていた（ターン, 評価器）が、current で 1 回でも落ちた（known_issue の評価器は除く）
- p95 レイテンシが baseline より `LATENCY_RATIO` 倍超、かつ `LATENCY_MIN_DELTA_S` 秒超 増えた
- 1 ターンあたりのトークンが `TOKEN_RATIO` 倍超 増えた
- 1 ターンあたりの LLM 呼び出しが `LLM_CALL_RATIO` 倍超 増えた
2 つとも、今の golden set・評価器で採点し直してから比べる（golden set を変えても、同じ物差しで比べられる）。
繰り返し数が違っても、1 ターンあたりで比べる。改善（落ちていたものが通った）は報告だけする。
"""

import argparse
import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

LATENCY_RATIO = 1.5
LATENCY_MIN_DELTA_S = 1.0
TOKEN_RATIO = 1.3
LLM_CALL_RATIO = 1.2


def _pass_map(report: dict[str, Any]) -> dict[tuple[str, str], list[bool]]:
    out: dict[tuple[str, str], list[bool]] = defaultdict(list)
    for turn in report["turns"]:
        known = set(turn.get("known_issue", []))
        for name, result in turn["evals"].items():
            if name in known:
                continue
            out[(turn["id"], name)].append(bool(result["passed"]))
    return out


def _per_turn(summary: dict[str, Any], key: str) -> float:
    return summary[key] / summary["turns"] if summary["turns"] else 0.0


def compare(baseline: dict[str, Any], current: dict[str, Any]) -> dict[str, Any]:
    regressions: list[str] = []
    improvements: list[str] = []
    base, cur = _pass_map(baseline), _pass_map(current)
    for key in sorted(base):
        if key not in cur:
            regressions.append(f"{key[0]}/{key[1]}: current に結果が無い")
        elif all(base[key]) and not all(cur[key]):
            regressions.append(
                f"{key[0]}/{key[1]}: baseline は全回通過、current は {cur[key].count(False)}/{len(cur[key])} 回失敗"
            )
        elif not all(base[key]) and all(cur[key]):
            improvements.append(f"{key[0]}/{key[1]}: baseline で失敗 → current は全回通過")

    b, c = baseline["summary"], current["summary"]
    if (
        c["latency_p95_s"] > b["latency_p95_s"] * LATENCY_RATIO
        and c["latency_p95_s"] - b["latency_p95_s"] > LATENCY_MIN_DELTA_S
    ):
        regressions.append(f"p95 レイテンシ: {b['latency_p95_s']:.2f}s → {c['latency_p95_s']:.2f}s")
    tok_b = _per_turn(b, "tokens_in") + _per_turn(b, "tokens_out")
    tok_c = _per_turn(c, "tokens_in") + _per_turn(c, "tokens_out")
    if tok_b and tok_c > tok_b * TOKEN_RATIO:
        regressions.append(f"1 ターンあたりのトークン: {tok_b:.0f} → {tok_c:.0f}")
    llm_b, llm_c = _per_turn(b, "llm_calls"), _per_turn(c, "llm_calls")
    if llm_b and llm_c > llm_b * LLM_CALL_RATIO:
        regressions.append(f"1 ターンあたりの LLM 呼び出し: {llm_b:.2f} → {llm_c:.2f}")
    return {"regressions": regressions, "improvements": improvements, "baseline": b, "current": c}


def render(result: dict[str, Any]) -> str:
    b, c = result["baseline"], result["current"]
    rows = [
        ("ターン数", "turns"),
        ("p50 レイテンシ(s)", "latency_p50_s"),
        ("p95 レイテンシ(s)", "latency_p95_s"),
        ("LLM 呼び出し", "llm_calls"),
        ("入力トークン", "tokens_in"),
        ("出力トークン", "tokens_out"),
        ("費用(USD)", "cost_usd"),
        ("検索の呼び出し", "search_calls"),
        ("実行エラー", "errors"),
    ]
    lines = ["| 指標 | baseline | current |", "|---|---|---|"]
    lines += [f"| {label} | {b.get(k)} | {c.get(k)} |" for label, k in rows]
    lines.append("")
    lines += [f"回帰: {r}" for r in result["regressions"]] or ["回帰: なし"]
    lines += [f"改善: {i}" for i in result["improvements"]]
    return "\n".join(lines)


def load_rescored(path: Path) -> dict[str, Any]:
    """保存済みの結果を、今の golden set・評価器で採点し直す（回答・検索・秒数・トークンの生の記録はそのまま）。"""
    from evals.golden.evaluators import Turn
    from evals.golden.runner import build_report, load_golden

    saved = json.loads(path.read_text("utf-8"))
    fields = Turn.__dataclass_fields__
    turns = [Turn(**{k: v for k, v in row.items() if k in fields}) for row in saved["turns"]]
    _, cases = load_golden()
    return build_report(
        cases, turns, label=saved["label"], repeat=saved["repeat"], max_llm_calls=saved.get("max_llm_calls", 0)
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("baseline", type=Path)
    parser.add_argument("current", type=Path)
    args = parser.parse_args(argv)
    result = compare(load_rescored(args.baseline), load_rescored(args.current))
    print(render(result))
    return 1 if result["regressions"] else 0


if __name__ == "__main__":
    sys.exit(main())
