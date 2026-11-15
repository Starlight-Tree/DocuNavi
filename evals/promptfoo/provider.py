"""Promptfoo のカスタムプロバイダー。golden set の runner（`evals.golden.runner`）を、そのまま呼ぶ。

1 つのテスト = 1 つの会話。`vars.cases` に、`chat_golden.yaml` と同じ形のターンを順に書く。
経路・安全確認・指標・評価器は runner のものを再利用し、ここでは複製しない（実 API を呼ぶ = 課金）。

実行前の確認（どれかが欠けたら、何も呼ばずに `error` を返す）は、runner と同じ:
- `GOLDEN_REAL=1`（承認を得てから付ける）
- LLM・質問の埋め込みは Vertex AI、検索はローカルの Postgres（`runner.check_environment`）
- LLM 呼び出しの全体の上限 `PROMPTFOO_GOLDEN_MAX_LLM_CALLS`（既定 30）。超える前に残りの会話を流さない

LangSmith には送らない（`LANGSMITH_TRACING=false`）。有料キーは使わない。
"""

import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

DEFAULT_MAX_LLM_CALLS = 30
_used_llm_calls = 0  # このプロセス（= 1 回の `promptfoo eval`）で使った LLM 呼び出しの合計


def resolve_cases(raw_cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """golden set の既定値を足し、`$not_found` を golden set の「資料に無い」の言い換えの一覧に置き換える。"""
    import yaml

    from evals.golden.evaluators import merged
    from evals.golden.runner import GOLDEN_PATH

    golden = yaml.safe_load(GOLDEN_PATH.read_text(encoding="utf-8"))
    cases = []
    for raw in raw_cases:
        for group in raw.get("must_include", []):
            if not isinstance(group, list):  # 「[[...]]」の書き間違い（文字列は 1 文字ずつ照合されてしまう）
                raise ValueError(f"{raw.get('id')}: must_include の各要素は、言い換えの一覧（リスト）です: {group!r}")
        case = merged(raw, golden["defaults"])
        case["must_include"] = [
            golden["not_found"] if group == ["$not_found"] else group for group in case.get("must_include", [])
        ]
        cases.append(case)
    return cases


def report_of(cases: list[dict[str, Any]], turns: list[Any]) -> dict[str, Any]:
    from evals.golden.evaluators import evaluate_all

    by_id = {c["id"]: c for c in cases}
    rows = []
    for t in turns:
        row = {k: v for k, v in t.__dict__.items() if k != "context"}  # 検索結果の本文は出力に載せない（長い）
        row["evals"] = evaluate_all(by_id[t.id], t)
        rows.append(row)
    return {"turns": rows}


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    global _used_llm_calls
    if os.environ.get("GOLDEN_REAL") != "1":
        return {"error": "実 API を呼びます（課金）。承認を得てから GOLDEN_REAL=1 を付けて実行してください"}
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env")  # アプリ・runner と同じ。既存の環境変数は上書きしない
    os.environ["LANGSMITH_TRACING"] = "false"
    os.environ.pop("GEMINI_API_KEY_PAID", None)

    from evals.golden.runner import LLMCallLimitError, check_environment, run_golden

    problems = check_environment()
    if problems:
        return {"error": "実行前の確認で止めました（API は呼んでいません）: " + " / ".join(problems)}
    limit = int(os.environ.get("PROMPTFOO_GOLDEN_MAX_LLM_CALLS", DEFAULT_MAX_LLM_CALLS))
    remaining = limit - _used_llm_calls
    if remaining <= 0:
        return {"error": f"LLM 呼び出しの全体の上限（{limit} 回）に達したため流しません"}

    cases = resolve_cases(context["vars"]["cases"])
    try:
        turns = asyncio.run(
            run_golden(cases, repeat=1, label=str(context["vars"].get("name", "pf")), max_llm_calls=remaining)
        )
    except LLMCallLimitError as exc:
        _used_llm_calls = limit
        return {"error": str(exc)}
    _used_llm_calls += sum(t.llm_calls for t in turns)
    tokens_in = sum(t.tokens_in for t in turns)
    tokens_out = sum(t.tokens_out for t in turns)
    return {
        "output": json.dumps(report_of(cases, turns), ensure_ascii=False),
        "tokenUsage": {"prompt": tokens_in, "completion": tokens_out, "total": tokens_in + tokens_out},
    }
