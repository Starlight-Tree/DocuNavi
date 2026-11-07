"""回答の生成だけを Cloudflare Workers AI の Qwen3 30B A3B FP8 に切り替えるための、試験用のチャットモデル。

`CHAT_ANSWER_BACKEND=workers_ai` のときだけ `graph.build_graph` が使う（既定は Gemini）。検索・履歴・ガードは変えない。
追加学習はしない（モデルはそのまま）。モデル名は固定で、環境変数で別のモデルに替えられない（有料のモデルを選ばないため）。

Free プランの無料枠（1 日 10,000 Neurons。UTC の 0 時にリセット）の中でだけ使う。
- 呼び出しのたびに、応答の usage から Neurons を数え、1 日の予算（`WORKERS_AI_DAILY_NEURON_BUDGET`。既定 8,000。
  10,000 を超えては設定できない）に達したら、Cloudflare へ送らずに止める（`WorkersAIQuotaExceeded`）。
- Cloudflare が枠切れ（エラー 3036）を返したら、その日の終わりまで止める。
- 数えるのはこのプロセスの分だけ。再起動で 0 に戻り、同じアカウントの他の利用も見えない（目安の安全弁で、請求の上限ではない）。
  アカウントが Workers Paid だと、枠を超えた分は課金される。Free プランであることは、使う前に利用者が確かめる。
- Gemini へは自動で切り替えない。失敗はそのまま固定メッセージの 429/502 などになる。再試行・ヘッジもしない。
"""

import json
import logging
import math
import os
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from typing import Any

import httpx
from langchain_core.callbacks import AsyncCallbackManagerForLLMRun, CallbackManagerForLLMRun
from langchain_core.exceptions import (
    ModelAuthenticationError,
    ModelNotFoundError,
    ModelPermissionDeniedError,
    ModelRateLimitError,
    ModelTimeoutError,
)
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    AIMessageChunk,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.messages.tool import tool_call_chunk
from langchain_core.outputs import ChatGeneration, ChatGenerationChunk, ChatResult
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field, SecretStr

logger = logging.getLogger(__name__)

MODEL = "@cf/qwen/qwen3-30b-a3b-fp8"
ENDPOINT = "https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions"

# 公式の料金表（https://developers.cloudflare.com/workers-ai/platform/pricing/ 、2026-11-05 に確認）
FREE_DAILY_NEURONS = 10_000
NEURONS_PER_M_INPUT_TOKENS = 4625
NEURONS_PER_M_OUTPUT_TOKENS = 30475
DEFAULT_DAILY_BUDGET = 8_000
DEFAULT_MAX_TOKENS = 1500
# 枠切れ（Account limited）。HTTP 429 で返る。3040（一時的な容量不足）は別物で、その日の停止にはしない
ERROR_DAILY_ALLOCATION_EXCEEDED = 3036

THINKING_DEFAULT = "default"  # モデルの既定のまま（何も送らない）
THINKING_OFF = "off"  # chat_template_kwargs.enable_thinking=false を送る（このモデルが受け付けるかは、実 API で確かめるまで未確認）
_THINKING_MODES = {THINKING_DEFAULT, THINKING_OFF}


class WorkersAIError(Exception):
    """Workers AI の呼び出しの失敗。本文・応答は載せない（HTTP ステータスと Cloudflare のエラー番号だけ）。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.code = code


class WorkersAIQuotaExceeded(WorkersAIError, ModelRateLimitError):
    """無料枠（または自分で決めた 1 日の予算）が尽きた。API は 429 の固定メッセージにする。次の UTC 0 時まで再試行しない。"""

    def __init__(self, message: str = "Workers AI の 1 日の無料枠に達したため、呼び出しを止めました"):
        WorkersAIError.__init__(self, message, 429)


class WorkersAIRateLimited(WorkersAIError, ModelRateLimitError):
    """429（一時的な容量不足など）。その日の停止にはしない。"""

    def __init__(self, message: str = "Workers AI が 429 を返しました"):
        WorkersAIError.__init__(self, message, 429)


class WorkersAIAuthError(WorkersAIError, ModelAuthenticationError):
    pass


class WorkersAIPermissionError(WorkersAIError, ModelPermissionDeniedError):
    pass


class WorkersAINotFound(WorkersAIError, ModelNotFoundError):
    pass


class WorkersAITimeout(WorkersAIError, ModelTimeoutError):
    pass


def neurons_for(prompt_tokens: int, completion_tokens: int) -> float:
    """入出力のトークン数から、消費する Neurons（公式の料金表の換算）。"""
    return prompt_tokens / 1e6 * NEURONS_PER_M_INPUT_TOKENS + completion_tokens / 1e6 * NEURONS_PER_M_OUTPUT_TOKENS


class NeuronBudget:
    """UTC の日ごとに、使った Neurons を数え、予算に達したら呼び出しを止める（プロセス内だけ）。"""

    def __init__(self, limit: float = DEFAULT_DAILY_BUDGET, clock: Callable[[], float] = time.time):
        self.limit = min(limit, FREE_DAILY_NEURONS)
        self._clock = clock
        self._lock = threading.Lock()
        self._day = self._today()
        self._used = 0.0
        self._exhausted = False

    def _today(self) -> int:
        return int(self._clock() // 86400)

    def _roll(self) -> None:
        today = self._today()
        if today != self._day:
            self._day, self._used, self._exhausted = today, 0.0, False

    @property
    def used(self) -> float:
        with self._lock:
            self._roll()
            return self._used

    def check(self) -> None:
        """予算に達している（または Cloudflare が枠切れを返した）なら、送る前に WorkersAIQuotaExceeded。"""
        with self._lock:
            self._roll()
            if self._exhausted or self._used >= self.limit:
                raise WorkersAIQuotaExceeded()

    def add(self, neurons: float) -> None:
        with self._lock:
            self._roll()
            self._used += max(neurons, 0.0)

    def mark_exhausted(self) -> None:
        with self._lock:
            self._roll()
            self._exhausted = True


def daily_budget() -> float:
    """WORKERS_AI_DAILY_NEURON_BUDGET（既定 8,000）。無料枠の 10,000 を超える値は、10,000 にする。不正値・0 以下は既定に戻す。"""
    try:
        value = float(os.environ.get("WORKERS_AI_DAILY_NEURON_BUDGET", ""))
    except ValueError:
        return DEFAULT_DAILY_BUDGET
    return min(value, FREE_DAILY_NEURONS) if math.isfinite(value) and value > 0 else DEFAULT_DAILY_BUDGET


def max_tokens() -> int:
    """WORKERS_AI_MAX_TOKENS（既定 1500）。1 回の回答の最大トークン数。不正値・1 未満は既定に戻す。"""
    try:
        value = int(os.environ.get("WORKERS_AI_MAX_TOKENS", "").strip())
    except ValueError:
        return DEFAULT_MAX_TOKENS
    return value if value >= 1 else DEFAULT_MAX_TOKENS


def thinking_mode() -> str:
    """WORKERS_AI_THINKING（`default` / `off`。既定 default）。それ以外の値は既定に戻す。"""
    value = os.environ.get("WORKERS_AI_THINKING", "").strip().lower()
    return value if value in _THINKING_MODES else THINKING_DEFAULT


def _partial_suffix(text: str, token: str) -> int:
    """text の末尾が token の先頭と重なっている最大の長さ（token が次のチャンクへまたがるときの保留に使う）。"""
    for size in range(min(len(token) - 1, len(text)), 0, -1):
        if text.endswith(token[:size]):
            return size
    return 0


class ThinkStripper:
    """本文に混ざる `<think>…</think>`（推論の過程）を取り除く。チャンクをまたぐ目印も扱う。

    推論は利用者に見せず、履歴にも残さない。`</think>` だけが来る形（開きの目印がテンプレート側にある場合）は、
    まだ何も出していないときに限り、それまでを推論として捨てる。
    """

    OPEN = "<think>"
    CLOSE = "</think>"

    def __init__(self) -> None:
        self._buffer = ""
        self._inside = False
        self._emitted = False
        self._trim_lead = False

    def feed(self, text: str) -> str:
        self._buffer += text
        out: list[str] = []
        while True:
            if self._inside:
                end = self._buffer.find(self.CLOSE)
                if end < 0:
                    keep = _partial_suffix(self._buffer, self.CLOSE)
                    self._buffer = self._buffer[len(self._buffer) - keep :]
                    break
                self._buffer = self._buffer[end + len(self.CLOSE) :]
                self._inside = False
                self._trim_lead = True
                continue
            start = self._buffer.find(self.OPEN)
            end = self._buffer.find(self.CLOSE)
            if end >= 0 and (start < 0 or end < start) and not self._emitted and not "".join(out).strip():
                self._buffer = self._buffer[end + len(self.CLOSE) :]
                self._trim_lead = True
                continue
            if start < 0:
                keep = max(_partial_suffix(self._buffer, self.OPEN), _partial_suffix(self._buffer, self.CLOSE))
                out.append(self._buffer[: len(self._buffer) - keep])
                self._buffer = self._buffer[len(self._buffer) - keep :]
                break
            out.append(self._buffer[:start])
            self._buffer = self._buffer[start + len(self.OPEN) :]
            self._inside = True
        return self._emit("".join(out))

    def flush(self) -> str:
        """入力の終わり。推論の途中なら残りは捨て、そうでなければ保留していた末尾を返す。"""
        rest = "" if self._inside else self._buffer
        self._buffer = ""
        return self._emit(rest)

    def _emit(self, text: str) -> str:
        if self._trim_lead and text:
            text = text.lstrip()
        if text:
            self._trim_lead = False
            self._emitted = True
        return text


def strip_think(text: str) -> str:
    stripper = ThinkStripper()
    return stripper.feed(text) + stripper.flush()


def content_text(value: Any) -> str:
    """応答の content を文字列にする。Workers AI は、数字だけの断片（"1" や "10"）を、文字列ではなく数値で返すことがある（実 API で確認）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return ""


def _tool_call_args(arguments: Any) -> dict[str, Any]:
    if isinstance(arguments, dict):
        return arguments
    try:
        parsed = json.loads(arguments) if arguments else {}
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def to_openai_messages(messages: Sequence[BaseMessage]) -> list[dict[str, Any]]:
    """LangChain のメッセージを、OpenAI 互換のチャットの形に直す。履歴の「検索の呼び出しと結果」もそのまま渡す。"""
    converted: list[dict[str, Any]] = []
    for message in messages:
        if isinstance(message, SystemMessage):
            converted.append({"role": "system", "content": message.text})
        elif isinstance(message, HumanMessage):
            converted.append({"role": "user", "content": message.text})
        elif isinstance(message, ToolMessage):
            converted.append({"role": "tool", "tool_call_id": message.tool_call_id, "content": message.text})
        elif isinstance(message, AIMessage):
            item: dict[str, Any] = {"role": "assistant", "content": message.text}
            if message.tool_calls:
                item["tool_calls"] = [
                    {
                        "id": call["id"] or f"call-{uuid.uuid4().hex[:12]}",
                        "type": "function",
                        "function": {"name": call["name"], "arguments": json.dumps(call["args"], ensure_ascii=False)},
                    }
                    for call in message.tool_calls
                ]
            converted.append(item)
        else:
            raise ValueError(f"対応していないメッセージの種類です: {type(message).__name__}")
    return converted


def _error_code(response: httpx.Response) -> int | None:
    """Cloudflare のエラー本文から、エラー番号だけを取る（本文そのものは使わない）。"""
    try:
        body = response.json()
    except ValueError:
        return None
    errors = body.get("errors") if isinstance(body, dict) else None
    if isinstance(errors, list) and errors and isinstance(errors[0], dict) and isinstance(errors[0].get("code"), int):
        return errors[0]["code"]
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict) and isinstance(error.get("code"), int):
        return error["code"]
    return None


def error_for(status: int, cloudflare_code: int | None) -> WorkersAIError:
    """HTTP ステータスとエラー番号から、固定メッセージの例外を作る（API はこれを固定文の 4xx/5xx にする）。"""
    detail = f"Workers AI がエラーを返しました（HTTP {status}" + (
        f"、エラー {cloudflare_code}）" if cloudflare_code else "）"
    )
    if status == 429:
        return (
            WorkersAIQuotaExceeded(detail)
            if cloudflare_code == ERROR_DAILY_ALLOCATION_EXCEEDED
            else WorkersAIRateLimited(detail)
        )
    if status == 401:
        return WorkersAIAuthError(detail, status)
    if status == 403:
        return WorkersAIPermissionError(detail, status)
    if status == 404:
        return WorkersAINotFound(detail, status)
    if status in (408, 504):
        return WorkersAITimeout(detail, status)
    return WorkersAIError(detail, status)


class ChatWorkersAI(BaseChatModel):
    """Workers AI の OpenAI 互換エンドポイント（`/ai/v1/chat/completions`）を呼ぶ。ストリーミングとツール呼び出しに対応する。"""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    account_id: str
    api_token: SecretStr
    budget: NeuronBudget = Field(default_factory=NeuronBudget, exclude=True)
    max_tokens: int = DEFAULT_MAX_TOKENS
    timeout: float = 30.0
    thinking: str = THINKING_DEFAULT
    # テスト用。None なら httpx の既定の通信
    transport: Any = Field(default=None, exclude=True)

    @property
    def _llm_type(self) -> str:
        return "cloudflare-workers-ai"

    def bind_tools(self, tools: Sequence[Any], **kwargs: Any) -> Any:
        return self.bind(tools=[convert_to_openai_tool(t) for t in tools], **kwargs)

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: CallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        raise NotImplementedError("ChatWorkersAI は非同期（ainvoke / astream）だけに対応します")

    def _client(self) -> httpx.AsyncClient:
        # 接続は 5 秒、応答は「次のデータが届くまで」の上限（本文が少しずつ届く応答は切らない）
        timeout = httpx.Timeout(self.timeout, connect=min(5.0, self.timeout))
        return httpx.AsyncClient(timeout=timeout, transport=self.transport)

    def _request(self, messages: list[BaseMessage], stream: bool, tools: Any = None) -> tuple[str, dict, dict]:
        body: dict[str, Any] = {
            "model": MODEL,
            "messages": to_openai_messages(messages),
            "max_tokens": self.max_tokens,
            "stream": stream,
        }
        if tools:
            body["tools"] = tools
        if stream:
            body["stream_options"] = {"include_usage": True}
        if self.thinking == THINKING_OFF:
            body["chat_template_kwargs"] = {"enable_thinking": False}
        headers = {"Authorization": f"Bearer {self.api_token.get_secret_value()}", "Content-Type": "application/json"}
        return ENDPOINT.format(account_id=self.account_id), headers, body

    def _record(self, body: dict, usage: dict | None, answer_chars: int, started: float, first: float | None) -> None:
        """使った Neurons を数え、1 行のログ（質問・回答の本文は載せない）を残す。usage が無ければ、文字数を 1 トークンとみなして多めに見積もる。"""
        prompt_tokens = usage.get("prompt_tokens") if usage else None
        completion_tokens = usage.get("completion_tokens") if usage else None
        estimated = not (isinstance(prompt_tokens, int) and isinstance(completion_tokens, int))
        if estimated:
            prompt_tokens = len(json.dumps(body["messages"], ensure_ascii=False))
            completion_tokens = max(answer_chars, 1)
        # 応答の usage に neurons があれば、それを使う（2026-11-07 の実 API で、料金表からの計算と一致することを確認した）
        reported = usage.get("neurons") if usage else None
        neurons = (
            float(reported)
            if isinstance(reported, (int, float)) and not isinstance(reported, bool)
            else neurons_for(prompt_tokens, completion_tokens)
        )
        self.budget.add(neurons)
        logger.info(
            "Workers AI: 入力 %d・出力 %d トークン%s、%.1f Neurons（本日 %.0f / 予算 %.0f）、最初の応答 %s、全体 %.2f 秒",
            prompt_tokens,
            completion_tokens,
            "（推定）" if estimated else "",
            neurons,
            self.budget.used,
            self.budget.limit,
            f"{first - started:.2f} 秒" if first is not None else "なし",
            time.monotonic() - started,
        )

    async def _raise_for_status(self, response: httpx.Response) -> None:
        if response.is_success:
            return
        await response.aread()
        code = _error_code(response)
        error = error_for(response.status_code, code)
        if isinstance(error, WorkersAIQuotaExceeded):
            self.budget.mark_exhausted()
        logger.error("Workers AI がエラーを返しました (status=%s, code=%s)", response.status_code, code)
        raise error

    async def _agenerate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> ChatResult:
        self.budget.check()
        url, headers, body = self._request(messages, stream=False, tools=kwargs.get("tools"))
        started = time.monotonic()
        try:
            async with self._client() as client:
                response = await client.post(url, headers=headers, json=body)
        except httpx.TimeoutException as exc:
            raise WorkersAITimeout("Workers AI の応答がタイムアウトしました") from exc
        await self._raise_for_status(response)
        try:
            data = response.json()
            message = data["choices"][0]["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise WorkersAIError("Workers AI の応答の形式が想定と違います", 502) from exc
        text = strip_think(content_text(message.get("content")))
        calls = [
            {
                "name": call["function"]["name"],
                "args": _tool_call_args(call["function"].get("arguments")),
                "id": call.get("id") or f"call-{uuid.uuid4().hex[:12]}",
                "type": "tool_call",
            }
            for call in message.get("tool_calls") or []
        ]
        self._record(body, data.get("usage"), len(text), started, None)
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=text, tool_calls=calls))])

    async def _astream(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: AsyncCallbackManagerForLLMRun | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[ChatGenerationChunk]:
        self.budget.check()
        url, headers, body = self._request(messages, stream=True, tools=kwargs.get("tools"))
        started = time.monotonic()
        first: float | None = None
        usage: dict | None = None
        answer_chars = 0
        stripper = ThinkStripper()
        try:
            async with self._client() as client, client.stream("POST", url, headers=headers, json=body) as response:
                await self._raise_for_status(response)
                async for line in response.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        event = json.loads(payload)
                    except ValueError as exc:
                        raise WorkersAIError("Workers AI のストリームの形式が想定と違います", 502) from exc
                    if isinstance(event.get("usage"), dict):
                        usage = event["usage"]
                    for choice in event.get("choices") or []:
                        delta = choice.get("delta") or {}
                        text = stripper.feed(content_text(delta.get("content")))
                        calls = [
                            tool_call_chunk(
                                name=(call.get("function") or {}).get("name"),
                                args=(call.get("function") or {}).get("arguments"),
                                id=call.get("id"),
                                index=call.get("index", 0),
                            )
                            for call in delta.get("tool_calls") or []
                        ]
                        if not text and not calls:
                            continue
                        if first is None:
                            first = time.monotonic()
                        answer_chars += len(text)
                        yield ChatGenerationChunk(message=AIMessageChunk(content=text, tool_call_chunks=calls))
        except httpx.TimeoutException as exc:
            raise WorkersAITimeout("Workers AI の応答がタイムアウトしました") from exc
        rest = stripper.flush()
        if rest:
            answer_chars += len(rest)
            yield ChatGenerationChunk(message=AIMessageChunk(content=rest))
        self._record(body, usage, answer_chars, started, first)


def create_workers_ai_llm(timeout: float = 30.0) -> ChatWorkersAI:
    """環境変数（CLOUDFLARE_ACCOUNT_ID・CLOUDFLARE_API_TOKEN）から作る。無ければ、値を含まない固定のメッセージで失敗する。"""
    account_id = os.environ.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    token = os.environ.get("CLOUDFLARE_API_TOKEN", "").strip()
    if not account_id or not token:
        raise ValueError(
            "CHAT_ANSWER_BACKEND=workers_ai には、CLOUDFLARE_ACCOUNT_ID と CLOUDFLARE_API_TOKEN（Workers AI の権限）が要ります"
        )
    return ChatWorkersAI(
        account_id=account_id,
        api_token=SecretStr(token),
        budget=NeuronBudget(daily_budget()),
        max_tokens=max_tokens(),
        timeout=timeout,
        thinking=thinking_mode(),
    )
