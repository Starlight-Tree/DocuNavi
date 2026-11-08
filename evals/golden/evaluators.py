"""グラフ全体の golden set の評価器。実 API・DB を使わない純粋な関数（`tests/test_golden_evaluators.py` が確かめる）。

評価器は 8 つ。LangSmith の既成の評価の型に対応させている（LLM を呼ばない、規則ベースの版）:
- hallucination（作り話）→ `grounding`（回答の数字が検索結果にあるか、期待する出典が当たったか）と `accuracy` の禁止事実
- tool-selection（ツールの選択）→ `search_need`（search_documents を呼んだか・回数が期待どおりか）
- trajectory（軌跡）→ `trajectory`（LLM・検索のステップの並びが期待どおりか）
残りは `accuracy`（必要な事実・禁止事実）、`conciseness`、`latency`、`cost`（トークン。単価を渡せば費用も）、`no_irrelevant_mention`。
"""

import math
import os
import re
import statistics
import unicodedata
from dataclasses import dataclass, field
from typing import Any

from app.metrics import IRRELEVANT_PHRASES  # 本番のログ（unrelated_source_mentioned）と同じ語の一覧

EVALUATORS = (
    "accuracy",
    "grounding",
    "conciseness",
    "search_need",
    "trajectory",
    "latency",
    "cost",
    "no_irrelevant_mention",
)

_NUMBER = re.compile(r"\d+(?:[./]\d+)?")


@dataclass
class Turn:
    """1 ターンの実行結果（runner が作る）。"""

    id: str
    rep: int
    question: str
    answer: str
    queries: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)  # llm / search_documents の順序
    sources: list[str] = field(default_factory=list)  # 検索結果のファイル名
    context: str = ""  # 検索結果の本文
    latency_s: float = 0.0  # 総時間（/chat/stream と同じ経路で、最後まで流し終えるまで）
    ttft_s: float | None = None  # 最初の本文（text-delta）を送るまで。ストリーミングで測ったときだけ
    llm_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    guard_reason: str | None = None
    error: str | None = None

    @property
    def search_calls(self) -> int:
        return sum(1 for s in self.steps if s == "search_documents")

    @property
    def tokens(self) -> int:
        return self.tokens_in + self.tokens_out


def _norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text).replace(",", "").replace("，", "")


def _numbers(text: str) -> set[str]:
    return set(_NUMBER.findall(_norm(text)))


def merged(case: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    return {**defaults, **case}


def evaluate_accuracy(case: dict[str, Any], turn: Turn) -> tuple[bool, str]:
    answer = _norm(turn.answer)
    missing = [group for group in case.get("must_include", []) if not any(_norm(alt) in answer for alt in group)]
    forbidden = [w for w in case.get("must_not_include", []) if _norm(w) in answer]
    if missing:
        return False, f"必要な事実が無い: {['/'.join(g) for g in missing]}"
    if forbidden:
        return False, f"禁止事実が出ている: {forbidden}"
    return True, "ok"


def evaluate_grounding(case: dict[str, Any], turn: Turn) -> tuple[bool, str]:
    """回答の数字は、検索結果か質問に無ければならない。期待する出典が 1 つも当たっていなければ失敗。"""
    expected = case.get("expect_sources") or []
    if expected and not any(src in turn.sources for src in expected):
        return False, f"期待する出典が当たっていない: 期待 {expected} / 実際 {turn.sources}"
    if not turn.context:
        return True, "検索結果なし（数字の照合は省略）"
    allowed = _numbers(turn.context) | _numbers(turn.question)
    ungrounded = sorted(_numbers(turn.answer) - allowed)
    if ungrounded:
        return False, f"回答の数字が検索結果に無い: {ungrounded}"
    return True, "ok"


def evaluate_conciseness(case: dict[str, Any], turn: Turn) -> tuple[bool, str]:
    limit = int(case["max_chars"])
    size = len(turn.answer)
    return size <= limit, f"{size} 文字（上限 {limit}）"


def evaluate_search_need(case: dict[str, Any], turn: Turn) -> tuple[bool, str]:
    expect = bool(case["expect_search"])
    calls = turn.search_calls
    if expect != (calls > 0):
        return False, f"search_documents の呼び出し {calls} 回（期待: {'あり' if expect else 'なし'}）"
    if expect and calls > int(case["max_search_calls"]):
        return False, f"search_documents の呼び出しが多い: {calls} 回（上限 {case['max_search_calls']}）"
    return True, f"{calls} 回"


def evaluate_trajectory(case: dict[str, Any], turn: Turn) -> tuple[bool, str]:
    expected = list(case["trajectory"])
    return turn.steps == expected, f"実際 {turn.steps} / 期待 {expected}"


def evaluate_latency(case: dict[str, Any], turn: Turn) -> tuple[bool, str]:
    limit = float(case["max_latency_s"])
    ttft_limit = float(case["max_ttft_s"])
    detail = f"総時間 {turn.latency_s:.1f} 秒（上限 {limit:g}）"
    ok = turn.latency_s <= limit
    if turn.ttft_s is not None:
        ok = ok and turn.ttft_s <= ttft_limit
        detail += f" / TTFT {turn.ttft_s:.1f} 秒（上限 {ttft_limit:g}）"
    return ok, detail


def price_from_env() -> tuple[float | None, float | None]:
    """USD / 100 万トークンの単価。確かめた単価を渡したときだけ費用を出す（既定は未設定）。"""
    try:
        return (
            float(os.environ["GOLDEN_PRICE_IN_PER_MTOK"]),
            float(os.environ["GOLDEN_PRICE_OUT_PER_MTOK"]),
        )
    except (KeyError, ValueError):
        return None, None


def cost_usd(turn: Turn, price_in: float | None, price_out: float | None) -> float | None:
    if price_in is None or price_out is None:
        return None
    return (turn.tokens_in * price_in + turn.tokens_out * price_out) / 1_000_000


def evaluate_cost(case: dict[str, Any], turn: Turn) -> tuple[bool, str]:
    limit = int(case["max_tokens"])
    usd = cost_usd(turn, *price_from_env())
    extra = f" / ${usd:.6f}" if usd is not None else ""
    return turn.tokens <= limit, f"{turn.tokens} トークン（上限 {limit}）{extra}"


def evaluate_no_irrelevant_mention(case: dict[str, Any], turn: Turn) -> tuple[bool, str]:
    hit = [p for p in IRRELEVANT_PHRASES if p in turn.answer]
    return not hit, f"無関係な結果への言及: {hit}" if hit else "ok"


_FUNCS = {
    "accuracy": evaluate_accuracy,
    "grounding": evaluate_grounding,
    "conciseness": evaluate_conciseness,
    "search_need": evaluate_search_need,
    "trajectory": evaluate_trajectory,
    "latency": evaluate_latency,
    "cost": evaluate_cost,
    "no_irrelevant_mention": evaluate_no_irrelevant_mention,
}


def evaluate(case: dict[str, Any], turn: Turn, name: str) -> tuple[bool, str]:
    if turn.error:  # 失敗したターンは、どの評価器も通らない（失敗時の挙動は error に残る）
        return False, f"実行に失敗: {turn.error}"
    return _FUNCS[name](case, turn)


def evaluate_all(case: dict[str, Any], turn: Turn) -> dict[str, dict[str, Any]]:
    return {name: dict(zip(("passed", "detail"), evaluate(case, turn, name), strict=True)) for name in EVALUATORS}


def percentile(values: list[float], q: float) -> float:
    """最近傍法（小さい標本向け）。値が無ければ 0。"""
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, min(len(ordered) - 1, math.ceil(q * len(ordered)) - 1))]


def summarize(turns: list[Turn]) -> dict[str, Any]:
    """速度・コスト・検索の集計（ガードが拒否して LLM を呼ばなかったターンは、レイテンシから除く）。

    latency_* は総時間、ttft_* は最初の本文までの時間（ストリーミングで測ったターンだけ）。
    """
    timed = [t.latency_s for t in turns if t.llm_calls > 0]
    ttfts = [t.ttft_s for t in turns if t.llm_calls > 0 and t.ttft_s is not None]
    price_in, price_out = price_from_env()
    costs = [cost_usd(t, price_in, price_out) for t in turns]
    return {
        "turns": len(turns),
        "latency_p50_s": round(statistics.median(timed), 3) if timed else 0.0,
        "latency_p95_s": round(percentile(timed, 0.95), 3),
        "latency_p99_s": round(percentile(timed, 0.99), 3),
        "latency_max_s": round(max(timed), 3) if timed else 0.0,
        "ttft_p50_s": round(statistics.median(ttfts), 3) if ttfts else None,
        "ttft_p95_s": round(percentile(ttfts, 0.95), 3) if ttfts else None,
        "ttft_p99_s": round(percentile(ttfts, 0.99), 3) if ttfts else None,
        "llm_calls": sum(t.llm_calls for t in turns),
        "tokens_in": sum(t.tokens_in for t in turns),
        "tokens_out": sum(t.tokens_out for t in turns),
        "cost_usd": round(sum(c for c in costs if c is not None), 6) if all(c is not None for c in costs) else None,
        "search_calls": sum(t.search_calls for t in turns),
        "errors": sum(1 for t in turns if t.error),
    }
