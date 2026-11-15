"""Promptfoo の判定。プロバイダーが付けた golden set の評価器の結果（8 つ × 全ターン）が、すべて通っていれば合格。

LLM は呼ばない。失敗したときは、どのターンのどの評価器かを理由に出す。
"""

import json
from typing import Any


def get_assert(output: str, context: dict[str, Any]) -> dict[str, Any]:
    report = json.loads(output)
    failures = [
        f"[{row['id']}] {name}: {result['detail']}"
        for row in report["turns"]
        for name, result in row["evals"].items()
        if not result["passed"]
    ]
    total = sum(len(row["evals"]) for row in report["turns"])
    return {
        "pass": not failures,
        "score": 1.0 if total and not failures else 1 - len(failures) / max(total, 1),
        "reason": "ok" if not failures else "; ".join(failures),
    }
