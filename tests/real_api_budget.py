"""実 API 検証（tests/real_api_server.py）の、1 回の実行あたりの予算。**時間あたりのレート制限ではない。**

数えるのは、Gemini への外部 API 呼び出しの総数（httpx の送信の位置。LLM の回答、質問・資料の埋め込み、429 などの
再試行・ヘッジの追加の試行を、すべて 1 回として含む）。偽の 503 に差し替えた要求は、ネットワークに出ないので数えない。
上限に達したら、送信の前に止める（ネットワークには出ない）。質問（/chat・/chat/stream）も、上限に達したら受け付けない。

既定と天井: 呼び出し 20 回、質問 10 件、実行全体 300 秒。環境変数（REAL_API_MAX_CALLS・REAL_API_MAX_QUESTIONS・
REAL_API_MAX_SECONDS）で下げられるが、天井（呼び出し 20・質問 10・600 秒）は超えられない。
通常の画面（localhost:3000）で、利用者が実 API に質問する回数には、上限を付けない（この予算はテストの実行だけ）。
"""

import threading
import time
from collections.abc import Callable, Mapping
from typing import Any

import httpx

from tests.real_api_faults import GEMINI_HOST

CALLS_DEFAULT = CALLS_CEILING = 20
QUESTIONS_DEFAULT = QUESTIONS_CEILING = 10
SECONDS_DEFAULT = 300.0
SECONDS_CEILING = 600.0

# 有料枠の料金（100 万トークンあたり。2026-11-11 に公式の料金表で確認、証拠等級 B）と、1 回の呼び出しの上限の見積もり。
# トークン数は測っていない見積もり（入力 10,000・出力 1,000 を上限と置く）。無料枠なら請求は 0 だが、入力は製品の改善に使われる。
PRICES_PER_MILLION = {
    "gemini-3.1-flash-lite": (0.25, 1.50),
    "gemini-3.5-flash-lite": (0.30, 2.50),
}
ASSUMED_INPUT_TOKENS = 10_000
ASSUMED_OUTPUT_TOKENS = 1_000


class BudgetExceeded(RuntimeError):
    """予算に達したので、Gemini への要求を送らない（ネットワークには出ていない）。"""


def _limit(env: Mapping[str, str], name: str, default: float, ceiling: float) -> float:
    try:
        value = float(env.get(name, "").strip())
    except ValueError:
        return default
    return min(value, ceiling) if value > 0 else default


def is_gemini(url: httpx.URL) -> bool:
    return url.host == GEMINI_HOST or url.host.endswith("aiplatform.googleapis.com")


class RealApiBudget:
    def __init__(
        self,
        max_calls: int = CALLS_DEFAULT,
        max_questions: int = QUESTIONS_DEFAULT,
        max_seconds: float = SECONDS_DEFAULT,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.max_calls = min(max_calls, CALLS_CEILING)
        self.max_questions = min(max_questions, QUESTIONS_CEILING)
        self.max_seconds = min(max_seconds, SECONDS_CEILING)
        self._clock = clock
        self._lock = threading.Lock()
        self._started: float | None = None
        self.calls = 0
        self.questions = 0
        self.refused_calls = 0
        self.refused_questions = 0
        self._originals: tuple[Any, Any] | None = None

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> "RealApiBudget":
        return cls(
            max_calls=int(_limit(env, "REAL_API_MAX_CALLS", CALLS_DEFAULT, CALLS_CEILING)),
            max_questions=int(_limit(env, "REAL_API_MAX_QUESTIONS", QUESTIONS_DEFAULT, QUESTIONS_CEILING)),
            max_seconds=_limit(env, "REAL_API_MAX_SECONDS", SECONDS_DEFAULT, SECONDS_CEILING),
        )

    def start(self) -> None:
        self._started = self._clock()

    @property
    def elapsed(self) -> float:
        return 0.0 if self._started is None else self._clock() - self._started

    @property
    def stopped_reason(self) -> str | None:
        """これ以上、質問も要求も出さない理由。まだ使えるなら None。"""
        if self.calls >= self.max_calls:
            return f"外部 API の呼び出しが上限（{self.max_calls} 回）に達した"
        if self.questions >= self.max_questions:
            return f"質問が上限（{self.max_questions} 件）に達した"
        if self._started is not None and self.elapsed >= self.max_seconds:
            return f"実行全体の時間が上限（{self.max_seconds:.0f} 秒）に達した"
        return None

    def take_question(self) -> str | None:
        """質問を 1 件受け付ける。受け付けられないときは、その理由（受け付けない）。"""
        with self._lock:
            reason = self.stopped_reason
            if reason is not None:
                self.refused_questions += 1
                return reason
            self.questions += 1
            return None

    def count_call(self) -> None:
        """Gemini への要求を 1 回数える。上限（回数・時間）に達していれば、数えずに BudgetExceeded。"""
        with self._lock:
            if self.calls >= self.max_calls or (self._started is not None and self.elapsed >= self.max_seconds):
                self.refused_calls += 1
                raise BudgetExceeded(self.stopped_reason or "予算に達した")
            self.calls += 1

    def snapshot(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "max_calls": self.max_calls,
            "questions": self.questions,
            "max_questions": self.max_questions,
            "elapsed_seconds": round(self.elapsed, 1),
            "max_seconds": self.max_seconds,
            "refused_calls": self.refused_calls,
            "refused_questions": self.refused_questions,
            "stopped_reason": self.stopped_reason,
        }

    def install(self) -> None:
        """httpx の送信（非同期・同期の両方）に、数える処理を重ねる。偽の 503 の差し込みより先に入れる（偽は数えない）。"""
        if self._originals is not None:
            return
        async_original, sync_original = httpx.AsyncClient.send, httpx.Client.send
        budget = self

        async def async_send(
            client: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any
        ) -> httpx.Response:
            if is_gemini(request.url):
                budget.count_call()
            return await async_original(client, request, *args, **kwargs)

        def sync_send(client: httpx.Client, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
            if is_gemini(request.url):
                budget.count_call()
            return sync_original(client, request, *args, **kwargs)

        self._originals = (async_original, sync_original)
        httpx.AsyncClient.send = async_send  # type: ignore[method-assign]
        httpx.Client.send = sync_send  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self._originals is not None:
            httpx.AsyncClient.send, httpx.Client.send = self._originals  # type: ignore[method-assign]
            self._originals = None


def estimated_max_cost_usd(budget: RealApiBudget, models: tuple[str, ...]) -> float:
    """上限まで使い切ったときの、有料枠での費用の概算の上限（高い方のモデルの料金で全部の呼び出しを見積もる）。"""
    rates = [PRICES_PER_MILLION[m] for m in models if m in PRICES_PER_MILLION]
    if not rates:
        return float("nan")
    per_call = max(i * ASSUMED_INPUT_TOKENS / 1e6 + o * ASSUMED_OUTPUT_TOKENS / 1e6 for i, o in rates)
    return per_call * budget.max_calls


def require_purpose(env: Mapping[str, str]) -> str:
    """REAL_API_PURPOSE（この実行の目的）が空なら、起動を止める。実 API は課金なので、目的を言わずには起動しない。"""
    purpose = env.get("REAL_API_PURPOSE", "").strip()
    if not purpose:
        raise SystemExit(
            "実 API の検証は、目的を REAL_API_PURPOSE に書いたときだけ起動します（課金のため。例: "
            "REAL_API_PURPOSE='フォールバックの確認' npm run test:e2e:real-api）。"
            "キーは scripts/dev-local.sh with-key で渡します。"
        )
    return purpose


def banner(purpose: str, budget: RealApiBudget, models: tuple[str, ...]) -> str:
    cost = estimated_max_cost_usd(budget, models)
    cost_text = "不明（モデルの料金が表にない）" if cost != cost else f"約 ${cost:.2f} 以下"
    return (
        "【実 API の検証】本物の Gemini を呼びます（課金されることがあります）。\n"
        f"  目的: {purpose}\n"
        f"  モデル: {', '.join(models)}\n"
        f"  1 回の実行の予算: 外部 API 呼び出し {budget.max_calls} 回（LLM・埋め込み・再試行を含む）、質問 {budget.max_questions} 件、"
        f"全体 {budget.max_seconds:.0f} 秒。上限に達したら、追加の質問・再試行はしません。\n"
        f"  概算費用（有料枠の上限の見積もり。入力 {ASSUMED_INPUT_TOKENS:,}・出力 {ASSUMED_OUTPUT_TOKENS:,} トークン／回と仮定。"
        f"トークン数は未測定）: {cost_text}。無料枠なら請求は 0 だが、入力は製品の改善に使われる。\n"
    )
