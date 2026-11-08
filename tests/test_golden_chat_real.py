"""golden set（evals/golden/chat_golden.yaml）を、グラフ全体の結果に当てる。ターン × 評価器の組ごとに 1 テスト。

結果の出どころは 2 つ（どちらも無ければ全部スキップ。通常の pytest・CI では何も呼ばない）:

1. 保存済みの結果（API を呼ばない）:  GOLDEN_RESULTS=evals/golden/results/baseline.json pytest tests/test_golden_chat_real.py
   `evals/golden/runner.py` が書いた JSON を、今の golden set・評価器で採点し直す（golden set や評価器を変えたときの確認）。
2. 実 API で流す（課金。承認を得てから）:  GOLDEN_REAL=1 pytest tests/test_golden_chat_real.py -m golden_real
   LLM と質問の埋め込みは Vertex AI、検索はローカルの Postgres（本番の DB は拒否）。`tests/conftest.py` が環境変数を空にするので、
   実行に要る値は `GOLDEN_` を付けた名前で渡す（`GOLDEN_GOOGLE_GENAI_USE_VERTEXAI` `GOLDEN_GOOGLE_CLOUD_PROJECT`
   `GOLDEN_GOOGLE_CLOUD_LOCATION`（任意） `GOLDEN_EMBEDDING_USE_VERTEXAI` `GOLDEN_DB_BACKEND` `GOLDEN_DATABASE_URL`）。
   `GOLDEN_REPEAT`（既定 1）、`GOLDEN_MAX_LLM_CALLS`（既定 70）、`GOLDEN_TRACE=1`（LangSmith へ送る）、`GOLDEN_LABEL`、
   `GOLDEN_SAVE=<パス>`（結果を JSON で保存。既存のファイルは上書きしない）。

`known_issue` に書いた評価器は xfail（今は満たせないと分かっているもの。直ったら golden set から外す）。
"""

import json
import os
from pathlib import Path
from typing import Any

import pytest

from evals.golden.evaluators import EVALUATORS, Turn, evaluate, summarize
from evals.golden.runner import (
    DEFAULT_MAX_LLM_CALLS,
    LLMCallLimitError,
    build_report,
    check_environment,
    load_golden,
    run_golden,
)

DEFAULTS, CASES = load_golden()
RUN_ENV_KEYS = (
    "GOOGLE_GENAI_USE_VERTEXAI",
    "GOOGLE_CLOUD_PROJECT",
    "GOOGLE_CLOUD_LOCATION",
    "EMBEDDING_USE_VERTEXAI",
    "DB_BACKEND",
    "DATABASE_URL",
)
P95_BUDGET_S = 8.0


def _turns_from_report(report: dict[str, Any]) -> list[Turn]:
    fields = Turn.__dataclass_fields__
    return [Turn(**{k: v for k, v in row.items() if k in fields}) for row in report["turns"]]


@pytest.fixture(scope="module")
def golden_turns() -> list[Turn]:
    saved = os.environ.get("GOLDEN_RESULTS")
    if saved:
        return _turns_from_report(json.loads(Path(saved).read_text("utf-8")))
    if os.environ.get("GOLDEN_REAL") != "1":
        pytest.skip("GOLDEN_RESULTS（保存済みの結果）か GOLDEN_REAL=1（実 API。課金）のどちらかを指定したときだけ動く")

    env = {key: os.environ.get(f"GOLDEN_{key}", "") for key in RUN_ENV_KEYS}
    problems = check_environment(env)
    if problems:
        pytest.fail("実行前の確認で止めました（API は呼んでいません）: " + "; ".join(problems), pytrace=False)
    repeat = int(os.environ.get("GOLDEN_REPEAT", "1"))
    max_calls = int(os.environ.get("GOLDEN_MAX_LLM_CALLS", str(DEFAULT_MAX_LLM_CALLS)))
    label = os.environ.get("GOLDEN_LABEL", "pytest")
    save = os.environ.get("GOLDEN_SAVE")
    if save and Path(save).exists():
        pytest.fail(f"{save} は既にあります（上書きしません）", pytrace=False)

    import asyncio

    with pytest.MonkeyPatch.context() as mp:
        for key, value in env.items():
            if value:
                mp.setenv(key, value)
        mp.delenv("GEMINI_API_KEY_PAID", raising=False)
        mp.delenv("CHAT_SMALLTALK_SKIP", raising=False)
        for name in ("SEARCH_HYBRID", "SEARCH_RERANK", "SEARCH_GRAPH"):  # 本番の既定（on）で測る
            mp.delenv(name, raising=False)
        try:
            turns = asyncio.run(
                run_golden(
                    CASES,
                    repeat=repeat,
                    label=label,
                    trace=os.environ.get("GOLDEN_TRACE") == "1",
                    max_llm_calls=max_calls,
                )
            )
        except LLMCallLimitError as exc:
            pytest.fail(str(exc), pytrace=False)
    if save:
        report = build_report(CASES, turns, label=label, repeat=repeat, max_llm_calls=max_calls)
        Path(save).write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return turns


def _params() -> list[Any]:
    params = []
    for case in CASES:
        known = (case.get("known_issue") or {}).get("evaluators", [])
        for name in EVALUATORS:
            marks = [pytest.mark.xfail(reason=case["known_issue"]["reason"], strict=False)] if name in known else []
            params.append(pytest.param(case, name, id=f"{case['id']}-{name}", marks=marks))
    return params


@pytest.mark.golden_real
@pytest.mark.parametrize(("case", "evaluator"), _params())
def test_turn_meets_the_golden_expectation(case: dict[str, Any], evaluator: str, golden_turns: list[Turn]) -> None:
    turns = [t for t in golden_turns if t.id == case["id"]]
    assert turns, f"{case['id']} の結果が無い"
    failures = []
    for t in turns:
        passed, detail = evaluate(case, t, evaluator)
        if not passed:
            failures.append(f"#{t.rep}: {detail}（質問: {case['question']} / 回答: {t.answer[:120]}）")
    assert not failures, "\n".join(failures)


@pytest.mark.golden_real
def test_no_turn_failed_to_run(golden_turns: list[Turn]) -> None:
    assert [(t.id, t.rep, t.error) for t in golden_turns if t.error] == []


@pytest.mark.golden_real
def test_the_whole_run_stays_within_the_latency_budget(golden_turns: list[Turn]) -> None:
    assert summarize(golden_turns)["latency_p95_s"] <= P95_BUDGET_S
