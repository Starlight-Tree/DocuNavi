"""実 API 検証（tests/real_api_server.py）用の、Gemini の 503 の差し込み。

主モデルへの HTTP 要求だけを、本物の 503 の応答に差し替える。差し替える位置は httpx の送信（`httpx.AsyncClient.send`）で、
その先は本物と同じ経路を通る: google-genai の `raise_for_async_response` が `errors.ServerError(503, …)` を作り、
langchain-google-genai が `GoogleAPIError` にし、`app/agents/resilience.py` の `classify_failure` が一時的な失敗と判定する。
フォールバックのモデル（と、差し替えを止めているときの主モデル）への要求は、そのまま本物の Gemini に届く。

本物の 503 との差: ネットワークを通らない（待ち時間がほぼ 0 で、Retry-After などのヘッダーが無い）。応答の本文は、
2026-10-20 に開発環境のログで見た本物の 503 と同じ形（code・message・status）。
"""

import hashlib
import re
import time
from typing import Any

import httpx

GEMINI_HOST = "generativelanguage.googleapis.com"
_MODEL_IN_PATH = re.compile(r"/models/([^/:]+):")

# 開発環境のログに出た、本物の 503 の本文の形
SERVER_503_BODY: dict[str, Any] = {
    "error": {
        "code": 503,
        "message": "This model is currently experiencing high demand. Spikes in demand are usually temporary. "
        "Please try again later.",
        "status": "UNAVAILABLE",
    }
}


# 失敗した応答のヘッダーのうち、レート制限か容量不足かの切り分けに使うものだけを記録する（それ以外は残さない）
DIAGNOSTIC_HEADERS = (
    "retry-after",
    "x-ratelimit-limit-requests",
    "x-ratelimit-limit-tokens",
    "x-ratelimit-remaining-requests",
    "x-ratelimit-remaining-tokens",
    "x-ratelimit-reset-requests",
    "x-ratelimit-reset-tokens",
)


def model_of(url: httpx.URL) -> str | None:
    """Gemini API の要求なら、URL に含まれるモデル名。それ以外は None。"""
    if url.host != GEMINI_HOST:
        return None
    match = _MODEL_IN_PATH.search(url.path)
    return match.group(1) if match else None


class FaultInjector:
    """主モデルへの要求を 503 にする切り替えと、Gemini への要求の記録（モデル名・差し替えたか・ステータス・秒数）。

    記録に残すのはモデル名・パスの種類・ステータス・秒数と、失敗（4xx/5xx）のときだけ、エラーのコードと状態（本文の
    `error.code` / `error.status`。メッセージ本文は残さない）と、DIAGNOSTIC_HEADERS に挙げたヘッダー。
    URL の全体や、他のヘッダー（API キー・Authorization・Cookie を含む）は残さない。
    """

    def __init__(self, primary_model: str) -> None:
        self.primary_model = primary_model
        self.primary_fail = False
        self.calls: list[dict[str, Any]] = []
        self._original_send: Any = None

    def install(self) -> None:
        if self._original_send is not None:
            return
        original = httpx.AsyncClient.send
        injector = self

        async def send(client: httpx.AsyncClient, request: httpx.Request, *args: Any, **kwargs: Any) -> httpx.Response:
            model = model_of(request.url)
            if model is None:
                return await original(client, request, *args, **kwargs)
            started = time.monotonic()
            if injector.primary_fail and model == injector.primary_model:
                fake = httpx.Response(503, json=SERVER_503_BODY, request=request)
                injector._record(model, request, faked=True, status=503, started=started, response=fake)
                return fake
            try:
                response = await original(client, request, *args, **kwargs)
                if response.status_code >= 400:
                    await response.aread()  # エラーの本文は小さく、SDK もこのあと読む（読み済みでも読み直せる）
            except BaseException as exc:
                injector._record(model, request, faked=False, status=None, started=started, error=type(exc).__name__)
                raise
            injector._record(
                model, request, faked=False, status=response.status_code, started=started, response=response
            )
            return response

        self._original_send = original
        httpx.AsyncClient.send = send  # type: ignore[method-assign]

    def uninstall(self) -> None:
        if self._original_send is not None:
            httpx.AsyncClient.send = self._original_send  # type: ignore[method-assign]
            self._original_send = None

    def reset(self) -> None:
        self.calls.clear()
        self.primary_fail = False

    def _record(
        self,
        model: str,
        request: httpx.Request,
        *,
        faked: bool,
        status: int | None,
        started: float,
        error: str | None = None,
        response: httpx.Response | None = None,
    ) -> None:
        entry: dict[str, Any] = {
            "seq": len(self.calls) + 1,
            "model": model,
            "endpoint": request.url.path.rsplit(":", 1)[-1],  # streamGenerateContent など
            "faked": faked,
            "status": status,
            # 本文を読み終えるまでではなく、応答の先頭（ヘッダー）が届くまでの秒数
            "seconds_to_response": round(time.monotonic() - started, 3),
        }
        if error:
            entry["error"] = error
        if response is not None and response.status_code >= 400:
            entry.update(_failure_details(response))
        self.calls.append(entry)


QUERY_HEAD_CHARS = 24


class RecordingSearch:
    """偽の検索を包み、モデルが検索ツールに渡した検索語と、そのヒット数を記録する（`app/` は変えない）。

    出典が空になった回が「ツール未呼び出し」「検索 0 件」「最後の検索が 0 件」のどれかを切り分けるための記録。
    検索語は全文を残さず、先頭 QUERY_HEAD_CHARS 文字と、全文の SHA-256 の先頭 8 桁、文字数だけにする。
    """

    def __init__(self, inner: Any) -> None:
        self._inner = inner
        self.searches: list[dict[str, Any]] = []

    async def __call__(self, query: str, top_k: int = 2, **kwargs: Any) -> list[Any]:
        hits = await self._inner(query, top_k, **kwargs)
        self.searches.append(
            {
                "seq": len(self.searches) + 1,
                "query_head": query[:QUERY_HEAD_CHARS],
                "query_chars": len(query),
                "query_sha256": hashlib.sha256(query.encode()).hexdigest()[:8],
                "hits": len(hits),
            }
        )
        return hits

    def reset(self) -> None:
        self.searches.clear()


def _failure_details(response: httpx.Response) -> dict[str, Any]:
    """失敗した応答から、診断に要る最小限（エラーのコード・状態と、DIAGNOSTIC_HEADERS のヘッダー）だけを取り出す。"""
    details: dict[str, Any] = {}
    try:
        error = response.json().get("error", {})
    except Exception:  # 本文が JSON でない・読めない
        error = {}
    if isinstance(error, dict):
        if isinstance(error.get("code"), int):
            details["error_code"] = error["code"]
        if isinstance(error.get("status"), str):
            details["error_status"] = error["status"]
    headers = {name: response.headers[name] for name in DIAGNOSTIC_HEADERS if name in response.headers}
    if headers:
        details["headers"] = headers
    return details
