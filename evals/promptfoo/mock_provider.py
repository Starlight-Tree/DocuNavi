"""API を呼ばない模擬プロバイダー。設定・判定（`assert_golden.py`）の流れだけを確かめる（回答の品質は測れない）。

    npx promptfoo eval -c promptfooconfig.yaml --providers file://mock_provider.py --no-cache --no-share

各ターンの回答を、期待する事実の 1 つ目の言い換えから組み立て、軌跡・出典は期待どおりにする。評価器は本物
（`evals.golden.evaluators`）。`PF_MOCK_BREAK=1` なら、最後のターンの回答を空にする（判定が落ちることの確認）。
"""

import json
import os
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))
from provider import ROOT, resolve_cases  # noqa: E402

sys.path.insert(0, str(ROOT))


def call_api(prompt: str, options: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    from evals.golden.evaluators import Turn, evaluate_all

    cases = resolve_cases(context["vars"]["cases"])
    rows = []
    for i, case in enumerate(cases):
        answer = "".join(group[0] for group in case.get("must_include", []))
        if os.environ.get("PF_MOCK_BREAK") == "1" and i == len(cases) - 1:
            answer = ""
        turn = Turn(
            id=case["id"],
            rep=1,
            question=case["question"],
            answer=answer,
            steps=list(case["trajectory"]) if case["expect_search"] else ["llm"],
            sources=list(case.get("expect_sources") or []),
            latency_s=0.1,
            llm_calls=len(case["trajectory"]),
            tokens_in=100,
            tokens_out=50,
        )
        row = {k: v for k, v in turn.__dict__.items() if k != "context"}
        row["evals"] = evaluate_all(case, turn)
        rows.append(row)
    return {"output": json.dumps({"turns": rows}, ensure_ascii=False)}
