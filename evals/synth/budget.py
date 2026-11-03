"""生成量の制限。上限に届いたら、新しい試行を始めずに止める（途中の結果は台帳に残る）。

上限は 4 つ: 件数（`max_docs`）、トークン（`max_tokens`）、この実行の費用（`max_cost_usd`）、
これまでの合計の費用（`max_total_cost_usd`。台帳の累計 + この実行）。費用は、トークン単価と GPU の時間単価の合計の見積もり。
並行して呼ぶので、試行の前に最大の見込みを予約し、結果が来たら実測へ置き換える。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass


class BudgetExceeded(Exception):
    def __init__(self, which: str) -> None:
        super().__init__(f"上限に達しました: {which}")
        self.which = which


@dataclass(frozen=True)
class Limits:
    max_docs: int
    max_tokens: int
    max_cost_usd: float
    max_total_cost_usd: float | None = None
    price_in_per_m: float = 0.0  # 入力 100 万トークンあたりの USD
    price_out_per_m: float = 0.0  # 出力 100 万トークンあたりの USD
    usd_per_hour: float = 0.0  # GPU など、呼び出しの経過時間で課金されるもの


class Budget:
    def __init__(self, limits: Limits, *, spent_before_usd: float = 0.0) -> None:
        self.limits = limits
        self._spent_before = spent_before_usd
        self._lock = threading.Lock()
        self.docs_started = 0
        self.tokens = 0  # 確定した実測
        self.cost_usd = 0.0
        self._reserved_tokens = 0
        self._reserved_cost = 0.0

    def cost_of(self, prompt_tokens: int, completion_tokens: int, seconds: float) -> float:
        lim = self.limits
        return (
            prompt_tokens * lim.price_in_per_m / 1e6
            + completion_tokens * lim.price_out_per_m / 1e6
            + seconds * lim.usd_per_hour / 3600
        )

    def start_doc(self) -> None:
        """新しい資料に着手する前に呼ぶ。件数の上限を超えるなら BudgetExceeded。"""
        with self._lock:
            if self.docs_started >= self.limits.max_docs:
                raise BudgetExceeded("max-docs")
            self.docs_started += 1

    def reserve(self, prompt_tokens: int, max_completion_tokens: int, max_seconds: float) -> tuple[int, float]:
        """試行 1 回の最大の見込みを予約する。超えるなら BudgetExceeded（予約はしない）。"""
        tokens = prompt_tokens + max_completion_tokens
        cost = self.cost_of(prompt_tokens, max_completion_tokens, max_seconds)
        with self._lock:
            lim = self.limits
            if self.tokens + self._reserved_tokens + tokens > lim.max_tokens:
                raise BudgetExceeded("max-tokens")
            if self.cost_usd + self._reserved_cost + cost > lim.max_cost_usd:
                raise BudgetExceeded("max-cost-usd")
            if lim.max_total_cost_usd is not None and (
                self._spent_before + self.cost_usd + self._reserved_cost + cost > lim.max_total_cost_usd
            ):
                raise BudgetExceeded("max-total-cost-usd")
            self._reserved_tokens += tokens
            self._reserved_cost += cost
        return tokens, cost

    def settle(
        self, reservation: tuple[int, float], prompt_tokens: int, completion_tokens: int, seconds: float
    ) -> float:
        """予約を実測に置き換える。確定した費用を返す。呼び出しが失敗して実測が無いときは 0 を渡す。"""
        actual = self.cost_of(prompt_tokens, completion_tokens, seconds)
        with self._lock:
            self._reserved_tokens -= reservation[0]
            self._reserved_cost -= reservation[1]
            self.tokens += prompt_tokens + completion_tokens
            self.cost_usd += actual
        return actual
