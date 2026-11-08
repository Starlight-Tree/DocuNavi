"""チャット 1 回ぶんの速度・コスト・挙動の指標（本番の構造化ログと、評価の golden set で同じものを測る）。

`ChatMetrics` は LangChain のコールバックで、グラフに渡す config の `callbacks` に入れる。応答 1 回に 1 行、JSON の INFO ログ
（ロガー `app.chat_metrics`。`app/main.py` の整形で、行全体が JSON になる）を `log()` で出す。Cloud Run では `jsonPayload` になり、
ログベース指標（`EXTRACT(jsonPayload.total_ms)` など）にできる。項目は `docs/configuration.md` の「チャットの指標」。

**ログに入れないもの:** 質問・回答・検索クエリ・検索結果の本文、埋め込み、API キー・トークン、ユーザー ID の生値
（利用者は `user_id_hash` = SHA-256 の先頭 16 桁。`session_id` はクライアントの `thread_id`）。数値・真偽・短い区分だけを出す。

集計の失敗でチャットを止めないよう、コールバックは例外を送出しない（`raise_error` は既定の False）。同期・インライン
（`run_inline = True`）で動かす。非同期のハンドラーだと、グラフの実行に `await` が増えて切断との先後が変わり、
`tests/test_chat_disconnect.py`（入力ガードの前の切断）が落ちる。
"""

import asyncio
import hashlib
import json
import logging
import re
import time
import unicodedata
import uuid
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler

logger = logging.getLogger("app.chat_metrics")

SEARCH_TOOL = "search_documents"
EVENT = "chat_metrics"

# 無関係な検索結果に触れる言い方（回答が検索結果の無関係さを言い出すのは、利用者に不要な情報）。規則ベースの近似
IRRELEVANT_PHRASES = ("関係ありません", "無関係", "とは関係", "関連はありません", "ご了承ください")

_CLOUD_TRACE = re.compile(r"^([0-9a-f]{32})(?:/|;|$)")
_CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿]")


def mentions_irrelevant(answer: str) -> bool:
    return any(phrase in answer for phrase in IRRELEVANT_PHRASES)


def new_request_id() -> str:
    return uuid.uuid4().hex


def trace_id_from(header: str | None) -> str:
    """Cloud Run の `X-Cloud-Trace-Context`（`TRACE_ID/SPAN_ID;o=1`）の TRACE_ID。無い・不正なら新しい ID。"""
    match = _CLOUD_TRACE.match((header or "").strip().lower())
    return match.group(1) if match else uuid.uuid4().hex


def user_hash(user_id: str | None) -> str:
    """ユーザー ID の SHA-256（先頭 16 桁）。匿名（AUTH_MODE=off）は `anon`。生の ID はログに出さない。"""
    if not user_id:
        return "anon"
    return hashlib.sha256(f"docunavi:{user_id}".encode()).hexdigest()[:16]


def detect_lang(text: str) -> str:
    """質問の主な言語の目安（`ja` / `en` / `other`）。本文はログに出さず、区分だけ残す。"""
    normalized = unicodedata.normalize("NFKC", text)
    if _CJK.search(normalized):
        return "ja"
    return "en" if re.search(r"[A-Za-z]", normalized) else "other"


def provider_and_model() -> tuple[str, str]:
    """回答に使う LLM の提供元とモデル名（設定から。リクエストごとの実際の切り替え〔フォールバック〕は反映しない）。"""
    from app.agents import graph

    if graph.is_mock_mode():
        return "mock", "mock"
    if graph.answer_backend() == graph.ANSWER_WORKERS_AI:
        return "workers_ai", "workers-ai"
    model = graph.os.environ.get("GEMINI_MODEL", "").strip() or graph.DEFAULT_MODEL
    return ("vertex" if graph.use_vertex_ai() else "gemini_api"), model


class ChatMetrics(BaseCallbackHandler):
    run_inline = True

    def __init__(
        self,
        endpoint: str,
        *,
        stream: bool = False,
        request_id: str | None = None,
        trace_id: str | None = None,
        session_id: str | None = None,
        user_id_hash: str = "anon",
        smalltalk: bool = False,
        lang: str = "other",
    ) -> None:
        self.endpoint = endpoint
        self.stream = stream
        self.request_id = request_id or new_request_id()
        self.trace_id = trace_id or new_request_id()
        self.session_id = session_id
        self.user_id_hash = user_id_hash
        self.smalltalk = smalltalk
        self.lang = lang
        self.started = time.monotonic()
        self.first_text_at: float | None = None
        self.llm_calls = 0
        self.tool_calls = 0
        self.search_calls = 0
        self.retries = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.llm_s = 0.0  # LLM 呼び出しの所要時間の合計（ヘッジで並行した試行は重なって数える）
        self.tool_s = 0.0
        self.retrieval_s = 0.0
        self.steps: list[str] = []  # llm / ツール名の順序（評価の軌跡で使う。ログには出さない）
        self._open: dict[Any, tuple[str, float]] = {}

    # --- LangChain のコールバック（同期・インライン）---

    def on_chat_model_start(self, serialized: Any, messages: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self.llm_calls += 1
        self.steps.append("llm")
        self._open[run_id] = ("llm", time.monotonic())

    def on_llm_end(self, response: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id)
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                self.tokens_in += int(usage.get("input_tokens", 0))
                self.tokens_out += int(usage.get("output_tokens", 0))

    def on_llm_error(self, error: BaseException, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id)
        if not isinstance(error, asyncio.CancelledError):  # 取り消したヘッジの試行は、失敗に数えない
            self.retries += 1

    def on_tool_start(self, serialized: Any, input_str: str, *, run_id: Any = None, **kwargs: Any) -> None:
        name = str(kwargs.get("name") or (serialized or {}).get("name") or "tool")
        self.tool_calls += 1
        if name == SEARCH_TOOL:
            self.search_calls += 1
        self.steps.append(name)
        self._open[run_id] = (name, time.monotonic())

    def on_tool_end(self, output: Any, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id)

    def on_tool_error(self, error: BaseException, *, run_id: Any = None, **kwargs: Any) -> None:
        self._close(run_id)

    def _close(self, run_id: Any) -> None:
        opened = self._open.pop(run_id, None)
        if opened is None:
            return
        kind, started = opened
        elapsed = time.monotonic() - started
        if kind == "llm":
            self.llm_s += elapsed
        else:
            self.tool_s += elapsed
            if kind == SEARCH_TOOL:
                self.retrieval_s += elapsed

    # --- 時間 ---

    def mark_first_text(self) -> None:
        """最初の本文（text-delta）を送った時刻を残す（2 回目以降は無視）。"""
        if self.first_text_at is None:
            self.first_text_at = time.monotonic()

    @property
    def total_s(self) -> float:
        return time.monotonic() - self.started

    @property
    def ttft_s(self) -> float | None:
        return None if self.first_text_at is None else self.first_text_at - self.started

    # --- ログ ---

    def record(
        self,
        status: str,
        state: dict[str, Any] | None = None,
        error: BaseException | None = None,
    ) -> dict[str, Any]:
        """ログに出す項目（本文を含まない）。state は、グラフの最終状態（無ければ None）。"""
        from app.agents.guardrails import OUTPUT_FALLBACK_REPLY
        from app.agents.tools import TOP_K

        state = state or {}
        messages = state.get("messages") or []
        answer = ""
        if messages and getattr(messages[-1], "type", "") == "ai":
            answer = str(getattr(messages[-1], "text", "") or "")
        sources = [s for s in (state.get("sources") or []) if isinstance(s, dict)]
        hit_count = state.get("hit_count")
        provider, model = provider_and_model()
        ttft = self.ttft_s
        error_type = type(error).__name__ if error is not None else None
        return {
            "severity": "INFO",  # 失敗は status と error_type で見る（ERROR にすると、失敗のログと二重に数えられる）
            "message": EVENT,
            "trace_id": self.trace_id,
            "request_id": self.request_id,
            "session_id": self.session_id,
            "user_id_hash": self.user_id_hash,
            "endpoint": self.endpoint,
            "provider": provider,
            "model": model,
            "status": status,
            "error_type": error_type,
            "guardrail_blocked": bool(state.get("guard_reason")) or (bool(answer) and answer == OUTPUT_FALLBACK_REPLY),
            "timeout": status == "504" or error_type in {"TimeoutError", "ReadTimeout"},
            "stream": self.stream,
            "smalltalk": self.smalltalk,
            "multi_turn": sum(1 for m in messages if getattr(m, "type", "") == "human") > 1,
            "lang": self.lang,
            "ttft_ms": None if ttft is None else round(ttft * 1000),
            "total_ms": round(self.total_s * 1000),
            "llm_ms": round(self.llm_s * 1000),
            "retrieval_ms": round(self.retrieval_s * 1000),
            "tool_ms": round(self.tool_s * 1000),
            "llm_calls": self.llm_calls,
            "tool_calls": self.tool_calls,
            "search_calls": self.search_calls,
            "retries": self.retries,
            "input_tokens": self.tokens_in,
            "output_tokens": self.tokens_out,
            "total_tokens": self.tokens_in + self.tokens_out,
            "retrieved_chunks": hit_count if isinstance(hit_count, int) else len(sources),
            "top_k": TOP_K,
            "answer_chars": len(answer),
            "citation_count": len({str(s.get("name")) for s in sources}),
            "unrelated_source_mentioned": mentions_irrelevant(answer),
        }

    def log(self, status: str, state: dict[str, Any] | None = None, error: BaseException | None = None) -> None:
        fields = self.record(status, state, error)
        # exc_info は付けない（スタックトレースは、失敗のログ側にある。ここは数値と区分だけ）
        logger.info(json.dumps(fields, ensure_ascii=False, separators=(",", ":")))
