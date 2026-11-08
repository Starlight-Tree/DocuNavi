"""グラフ全体（検索 → 回答）を golden set で流す（実 API を呼ぶ。課金される）。

    GOLDEN_REAL=1 python -m evals.golden.runner --label baseline --repeat 3 --trace

前提（どれかが欠けたら、何も呼ばずに止まる）:
- LLM と質問の埋め込みは Vertex AI（`GOOGLE_GENAI_USE_VERTEXAI=true`、`GOOGLE_CLOUD_PROJECT`、`EMBEDDING_USE_VERTEXAI=true`。ADC 認証）
- 検索はローカルの Postgres（`DB_BACKEND=pg`、`DATABASE_URL` のホストが localhost / 127.0.0.1 / host.docker.internal / db のどれか）。本番の DB には向けない
- LLM 呼び出しの上限（`--max-llm-calls`、既定 70）。超えたら残りを流さずに止める

会話履歴はメモリ（DB に書かない）。`--trace` で LangSmith にトレースを送る（`LANGSMITH_API_KEY` などは環境変数から。値は出さない）。
結果は JSON（質問・回答・検索語・検索結果・出典・秒数・トークン・評価）。既存のファイルは上書きしない。
"""

import argparse
import asyncio
import json
import os
import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage

from evals.golden.evaluators import EVALUATORS, Turn, evaluate_all, merged, summarize

GOLDEN_PATH = Path(__file__).with_name("chat_golden.yaml")
RESULTS_DIR = Path(__file__).with_name("results")
LOCAL_DB_HOSTS = frozenset({"localhost", "127.0.0.1", "host.docker.internal", "db"})
DEFAULT_MAX_LLM_CALLS = 70


class LLMCallLimitError(RuntimeError):
    """LLM 呼び出しが承認された上限を超える。"""


def load_golden(path: Path = GOLDEN_PATH) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    defaults = data["defaults"]
    return defaults, [merged(case, defaults) for case in data["cases"]]


def check_environment(env: dict[str, str] | None = None) -> list[str]:
    """実 API を呼ぶ前の安全確認。問題の一覧（空なら進んでよい）。値は含めない。"""
    env = dict(os.environ) if env is None else env
    problems: list[str] = []
    if env.get("GOOGLE_GENAI_USE_VERTEXAI", "").strip().lower() not in {"1", "true", "yes", "on"}:
        problems.append("GOOGLE_GENAI_USE_VERTEXAI=true が要ります（LLM は Vertex AI）")
    if not env.get("GOOGLE_CLOUD_PROJECT", "").strip():
        problems.append("GOOGLE_CLOUD_PROJECT が要ります")
    if env.get("EMBEDDING_USE_VERTEXAI", "").strip().lower() not in {"1", "true", "yes", "on"}:
        problems.append("EMBEDDING_USE_VERTEXAI=true が要ります（質問の埋め込みも Vertex AI）")
    if env.get("DB_BACKEND", "").strip().lower() != "pg":
        problems.append("DB_BACKEND=pg が要ります")
    url = env.get("DATABASE_URL", "").strip()
    host = urlparse(url).hostname if url else None
    if host not in LOCAL_DB_HOSTS:
        problems.append("DATABASE_URL がローカルの Postgres ではありません（本番の DB には向けません）")
    if env.get("CHAT_SMALLTALK_SKIP", "").strip():
        problems.append("CHAT_SMALLTALK_SKIP は既定（空）のまま測ります")
    return problems


class Meter(AsyncCallbackHandler):
    """LLM 呼び出し・検索・トークンを数える。全ターンの合計が上限を超える前に止める。"""

    raise_error = True

    def __init__(self, max_llm_calls: int) -> None:
        self.max_llm_calls = max_llm_calls
        self.total_llm_calls = 0
        self.reset()

    def reset(self) -> None:
        self.steps: list[str] = []
        self.llm_calls = 0
        self.tokens_in = 0
        self.tokens_out = 0

    async def on_chat_model_start(self, serialized: Any, messages: Any, **kwargs: Any) -> None:
        self.total_llm_calls += 1
        if self.total_llm_calls > self.max_llm_calls:
            raise LLMCallLimitError(f"LLM 呼び出しが上限（{self.max_llm_calls} 回）を超えるため止めます")
        self.llm_calls += 1
        self.steps.append("llm")

    async def on_tool_start(self, serialized: Any, input_str: str, **kwargs: Any) -> None:
        name = kwargs.get("name") or (serialized or {}).get("name") or "tool"
        self.steps.append(str(name))

    async def on_llm_end(self, response: Any, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                self.tokens_in += int(usage.get("input_tokens", 0))
                self.tokens_out += int(usage.get("output_tokens", 0))


async def run_golden(
    cases: list[dict[str, Any]],
    *,
    repeat: int = 1,
    label: str = "run",
    trace: bool = False,
    max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
    on_turn: Any = None,
) -> list[Turn]:
    """cases を順に流す（同じ thread のターンは同じ会話）。repeat 回繰り返す（会話は毎回新しい）。"""
    import langsmith

    from app.agents.graph import build_graph
    from app.api.routes import _turn_inputs
    from app.history import history_store
    from app.scope import ACCESS_SCOPE_KEY, Scope
    from app.search import vector as search_vector

    meter = Meter(max_llm_calls)
    turns: list[Turn] = []
    run_id = f"{label}-{int(time.time())}"
    async with history_store():
        try:
            await search_vector.verify()
            graph = build_graph()  # checkpointer なし = メモリ（履歴を DB に書かない）
            with langsmith.tracing_context(enabled=trace):
                for rep in range(1, repeat + 1):
                    for case in cases:
                        meter.reset()
                        config = {
                            "configurable": {
                                "thread_id": f"golden-{run_id}-{rep}-{case['thread']}",
                                ACCESS_SCOPE_KEY: Scope.ALL,
                            },
                            "callbacks": [meter],
                            "run_name": f"golden:{case['id']}",
                            "tags": ["golden", label, case["category"]],
                            "metadata": {"golden_id": case["id"], "rep": rep, "label": label},
                        }
                        started = time.monotonic()
                        error = None
                        out: dict[str, Any] = {}
                        try:
                            out = await graph.ainvoke(_turn_inputs(case["question"], None), config)
                        except LLMCallLimitError:
                            raise
                        except Exception as exc:  # noqa: BLE001 - 失敗は結果に残し、残りのターンは続ける
                            error = type(exc).__name__
                        latency = time.monotonic() - started
                        messages = out.get("messages", [])
                        last_human = max((i for i, m in enumerate(messages) if m.type == "human"), default=0)
                        queries = [
                            str(tc["args"].get("query"))
                            for m in messages[last_human:]
                            if isinstance(m, AIMessage)
                            for tc in m.tool_calls
                        ]
                        turn = Turn(
                            id=case["id"],
                            rep=rep,
                            question=case["question"],
                            answer=messages[-1].text if messages and not error else "",
                            queries=queries,
                            steps=list(meter.steps),
                            sources=sorted({str(s.get("name")) for s in out.get("sources", []) if isinstance(s, dict)}),
                            context=str(out.get("context") or ""),
                            latency_s=round(latency, 3),
                            llm_calls=meter.llm_calls,
                            tokens_in=meter.tokens_in,
                            tokens_out=meter.tokens_out,
                            guard_reason=out.get("guard_reason"),
                            error=error,
                        )
                        turns.append(turn)
                        if on_turn:
                            on_turn(case, turn)
        finally:
            await search_vector.close()
    return turns


def build_report(
    cases: list[dict[str, Any]], turns: list[Turn], *, label: str, repeat: int, max_llm_calls: int
) -> dict[str, Any]:
    by_id = {c["id"]: c for c in cases}
    rows = []
    for t in turns:
        case = by_id[t.id]
        row = {k: v for k, v in t.__dict__.items()}
        row["evals"] = evaluate_all(case, t)
        row["known_issue"] = (case.get("known_issue") or {}).get("evaluators", [])
        rows.append(row)
    return {
        "label": label,
        "created": datetime.now(UTC).isoformat(timespec="seconds"),
        "repeat": repeat,
        "max_llm_calls": max_llm_calls,
        "evaluators": list(EVALUATORS),
        "summary": summarize(turns),
        "turns": rows,
    }


def _print_turn(case: dict[str, Any], turn: Turn) -> None:
    flags = evaluate_all(case, turn)
    failed = [n for n, r in flags.items() if not r["passed"]]
    print(
        f"[{turn.id}#{turn.rep}] {turn.latency_s:.1f}s llm={turn.llm_calls} tok={turn.tokens} "
        f"steps={'>'.join(turn.steps) or '-'} {'FAIL ' + ','.join(failed) if failed else 'ok'}",
        flush=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--label", default="run", help="結果の名前（ファイル名・トレースのタグ）。英数字と - _ のみ")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--trace", action="store_true", help="LangSmith にトレースを送る")
    parser.add_argument("--max-llm-calls", type=int, default=DEFAULT_MAX_LLM_CALLS)
    parser.add_argument(
        "--out", type=Path, default=None, help="既定は evals/golden/results/<label>.json（既存は上書きしない）"
    )
    args = parser.parse_args(argv)

    if os.environ.get("GOLDEN_REAL") != "1":
        print("実 API を呼びます（課金）。承認を得てから GOLDEN_REAL=1 を付けて実行してください。", file=sys.stderr)
        return 2
    if not re.fullmatch(r"[A-Za-z0-9_-]+", args.label):
        print("--label は英数字と - _ だけです", file=sys.stderr)
        return 2
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parents[2] / ".env")  # アプリと同じ。既存の環境変数は上書きしない
    problems = check_environment()
    if problems:
        print("実行前の確認で止めました（API は呼んでいません）:\n- " + "\n- ".join(problems), file=sys.stderr)
        return 2
    out_path = args.out or RESULTS_DIR / f"{args.label}.json"
    if out_path.exists():
        print(f"{out_path} は既にあります（上書きしません。別の --label か --out を使ってください）", file=sys.stderr)
        return 2

    _, cases = load_golden()
    os.environ.pop("GEMINI_API_KEY_PAID", None)  # 有料キーは使わない
    try:
        turns = asyncio.run(
            run_golden(
                cases,
                repeat=args.repeat,
                label=args.label,
                trace=args.trace,
                max_llm_calls=args.max_llm_calls,
                on_turn=_print_turn,
            )
        )
    except LLMCallLimitError as exc:
        print(str(exc), file=sys.stderr)
        return 3
    report = build_report(cases, turns, label=args.label, repeat=args.repeat, max_llm_calls=args.max_llm_calls)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False))
    print(f"結果: {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
