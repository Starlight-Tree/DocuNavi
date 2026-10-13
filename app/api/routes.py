import asyncio
import importlib
import logging
import math
import os
import re
import time
import uuid
from functools import lru_cache
from typing import Annotated

from fastapi import APIRouter, HTTPException, Request, Response
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, StringConstraints, field_validator

from app.agents.graph import build_graph, is_mock_mode

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_MESSAGE_LENGTH = 2000
MAX_THREAD_ID_LENGTH = 128
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0
# 取り消した LLM 呼び出し（と切断の監視）の終了を待つ上限。hedged_call 自身の後始末の上限
# （resilience._CANCEL_GRACE_SECONDS = 1 秒）より長くして、先に待ちを諦めないようにする
CANCEL_WAIT_SECONDS = 2.0

# LLM 失敗時にクライアントへ返す固定メッセージ。例外の文字列は絶対に含めない
_DETAIL_RATE_LIMIT = "リクエストが集中しています。しばらく待ってから再試行してください。"
_DETAIL_TIMEOUT = "LLM の応答がタイムアウトしました。しばらく待ってから再試行してください。"
_DETAIL_CONFIG = "サーバー側の LLM 設定に問題があります。管理者に連絡してください。"
_DETAIL_UPSTREAM = "LLM 呼び出しに失敗しました。しばらく待ってから再試行してください。"


def _optional_classes(*specs: tuple[str, str]) -> tuple[type[BaseException], ...]:
    """(モジュール, クラス名) のうち import できた例外クラスだけを返す。

    langchain / google-genai のバージョン差で属性が無くても、アプリ全体を落とさないため。
    """
    found: list[type[BaseException]] = []
    for module_name, class_name in specs:
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except Exception:  # ImportError / AttributeError 等
            continue
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)


# langchain-google-genai は google.genai の ClientError/ServerError を、HTTP ステータスに応じた
# langchain_core.exceptions.Model*Error（GoogleRateLimitError 等）へ包み直して送出する
_TIMEOUT_ERRORS = (TimeoutError,) + _optional_classes(
    ("langchain_core.exceptions", "ModelTimeoutError"),
    ("httpx", "TimeoutException"),
    ("httpx2", "TimeoutException"),
)
_RATE_LIMIT_ERRORS = _optional_classes(("langchain_core.exceptions", "ModelRateLimitError"))
_CONFIG_ERRORS = _optional_classes(
    ("langchain_core.exceptions", "ModelAuthenticationError"),
    ("langchain_core.exceptions", "ModelPermissionDeniedError"),
    ("langchain_core.exceptions", "ModelNotFoundError"),
)
# google.genai.errors.APIError は HTTP ステータスを code、本文を details に持つ
_API_ERRORS = _optional_classes(("google.genai.errors", "APIError"))


class ChatRequest(BaseModel):
    # 前後の空白を除いた上で長さを検証する（空白のみは min_length で弾かれる）
    message: Annotated[
        str,
        StringConstraints(strip_whitespace=True, min_length=1, max_length=MAX_MESSAGE_LENGTH),
        Field(description="ユーザーの発言"),
    ]
    thread_id: str | None = Field(
        default=None,
        max_length=MAX_THREAD_ID_LENGTH,
        pattern=r"^[A-Za-z0-9_-]+$",
        description="会話ID。同じ値を送ると会話が継続する",
    )

    @field_validator("thread_id", mode="before")
    @classmethod
    def _empty_thread_id_is_none(cls, value: object) -> object:
        # 従来は空文字でも新規会話として扱えたので、その挙動を保つ
        return None if value == "" else value


class ChatResponse(BaseModel):
    reply: str
    thread_id: str
    context: str = Field(description="回答の根拠として検索された設定資料（未検索なら空）")
    mode: str = Field(description="実行モード。mock = ダミー応答 / gemini = 本物の LLM")


@lru_cache(maxsize=2)
def get_graph(mock: bool):
    # mock をキーにキャッシュするので、キー設定後は自動で Gemini 側のグラフに切り替わる
    return build_graph()


def current_mode() -> str:
    return "mock" if is_mock_mode() else "gemini"


def llm_timeout_seconds() -> float:
    """LLM_TIMEOUT_SECONDS（既定 60）。不正値・0 以下は既定値に戻す。"""
    try:
        value = float(os.environ.get("LLM_TIMEOUT_SECONDS", ""))
    except ValueError:
        return DEFAULT_LLM_TIMEOUT_SECONDS
    return value if math.isfinite(value) and value > 0 else DEFAULT_LLM_TIMEOUT_SECONDS


def _exception_chain(exc: BaseException) -> list[BaseException]:
    """exc とその原因を辿った一覧（ラッパー例外の裏に本当の原因があるため）。"""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return chain


def _api_error_code(exc: BaseException) -> int | None:
    if _API_ERRORS and isinstance(exc, _API_ERRORS):
        code = getattr(exc, "code", None)
        return code if isinstance(code, int) else None
    return None


def _is_invalid_api_key(exc: BaseException) -> bool:
    # Gemini はキー無効を 401/403 ではなく 400 (reason=API_KEY_INVALID) で返すため、本文で判定する
    if _api_error_code(exc) != 400:
        return False
    return "API_KEY_INVALID" in str(getattr(exc, "details", ""))


def _retry_after_seconds(chain: list[BaseException]) -> int | None:
    """429 の待ち時間（秒）。Retry-After ヘッダ、無ければ RetryInfo.retryDelay から取る。"""
    for exc in chain:
        headers = getattr(getattr(exc, "response", None), "headers", None)
        try:
            raw = str(headers.get("retry-after", "")).strip() if headers is not None else ""
        except Exception:
            raw = ""
        if raw.isdigit() and int(raw) > 0:
            return int(raw)

        details = getattr(exc, "details", None)
        error = details.get("error", details) if isinstance(details, dict) else None
        items = error.get("details") if isinstance(error, dict) else None
        for item in items if isinstance(items, list) else []:
            if not (isinstance(item, dict) and str(item.get("@type", "")).endswith("RetryInfo")):
                continue
            match = re.fullmatch(r"(\d+(?:\.\d+)?)s", str(item.get("retryDelay", "")))
            if match and float(match.group(1)) > 0:
                return math.ceil(float(match.group(1)))
    return None


def classify_llm_error(exc: BaseException) -> HTTPException:
    """LLM 呼び出しの例外を、内部情報を含まない HTTPException へ変換する。"""
    chain = _exception_chain(exc)
    codes = [_api_error_code(e) for e in chain]

    if any(isinstance(e, _TIMEOUT_ERRORS) for e in chain) or any(c in (408, 504) for c in codes):
        return HTTPException(status_code=504, detail=_DETAIL_TIMEOUT)

    if any(isinstance(e, _RATE_LIMIT_ERRORS) for e in chain) or 429 in codes:
        retry_after = _retry_after_seconds(chain)
        headers = {"Retry-After": str(retry_after)} if retry_after else None
        return HTTPException(status_code=429, detail=_DETAIL_RATE_LIMIT, headers=headers)

    if (
        any(isinstance(e, _CONFIG_ERRORS) for e in chain)
        or any(c in (401, 403) for c in codes)
        or any(_is_invalid_api_key(e) for e in chain)
    ):
        # キーやモデル名の誤りはクライアントでは直せないので 500 とし、内容は伏せる
        return HTTPException(status_code=500, detail=_DETAIL_CONFIG)

    return HTTPException(status_code=502, detail=_DETAIL_UPSTREAM)


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "mode": current_mode()}


async def _until_disconnect(request: Request) -> None:
    """クライアントの切断（http.disconnect）が来るまで待つ。ほかのメッセージは読み捨てる。

    リクエスト本文は FastAPI が読み終えている。そのあとの receive() は、切断かレスポンスの完了まで待つ。
    """
    try:
        while True:
            message = await request.receive()
            if message["type"] == "http.disconnect":
                return
    except Exception:
        # 監視の失敗は LLM 呼び出しを止める理由にならない。切断を検知できないだけで、従来と同じ挙動になる
        logger.warning("クライアントの切断を監視できませんでした。LLM 呼び出しの完了を待ちます", exc_info=True)
        await asyncio.Event().wait()  # 切断とは扱わない。結果待ちの側が取り消す


def _retrieve_exception(task: "asyncio.Future[object]") -> None:
    """終わったタスクの例外を取り出す（取り出さないと「Task exception was never retrieved」になる）。"""
    if not task.cancelled():
        task.exception()


async def _cancel_and_wait(*tasks: "asyncio.Future[object] | None") -> None:
    """残っているタスクを取り消し、終わるのを待つ（タスクを取り残さない）。

    取り消しを無視するタスクがあっても戻れるよう、待ちには上限（CANCEL_WAIT_SECONDS）を設ける。
    待っている間にこのハンドラ自身が取り消された場合も、タスクの終了は上限まで待ってから、その取り消しを伝える。
    """
    live = [task for task in tasks if task is not None]
    for task in live:
        task.add_done_callback(_retrieve_exception)
        task.cancel()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + CANCEL_WAIT_SECONDS
    interrupted = False
    while live and (remaining := deadline - loop.time()) > 0:
        try:
            await asyncio.wait(live, timeout=remaining)
            break
        except asyncio.CancelledError:
            interrupted = True
            if all(task.done() for task in live):
                break
    if interrupted:
        raise asyncio.CancelledError


@router.post("/chat", response_model=ChatResponse)
async def chat(req: ChatRequest, request: Request) -> ChatResponse | Response:
    mode = current_mode()
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id}}
    started = time.monotonic()
    work: asyncio.Future | None = None
    gone: asyncio.Future | None = None
    try:
        # context は直近のターンの検索結果だけを返したいので、毎ターン空に戻す
        work = asyncio.ensure_future(
            get_graph(mode == "mock").ainvoke(
                {"messages": [HumanMessage(content=req.message)], "context": ""},
                config,
            )
        )
        # 本文は読み終えているので、ここから切断を監視できる。切断が先に来たら、待っても受け取る相手が
        # いない LLM 呼び出し（ヘッジの追加試行を含む）を取り消す
        gone = asyncio.ensure_future(_until_disconnect(request))
        done, _ = await asyncio.wait(
            {work, gone}, timeout=llm_timeout_seconds(), return_when=asyncio.FIRST_COMPLETED
        )
        if work in done:  # 同時に終わったときは結果を優先する
            result = work.result()
        elif gone in done:
            # 切断はエラーではない。except Exception の外で返し、ログも INFO・固定文・thread_id だけにする
            logger.info(
                "クライアントが切断したため LLM 呼び出しを取り消しました (thread_id=%s, elapsed=%.1fs)",
                thread_id,
                time.monotonic() - started,
            )
            return Response(status_code=499)  # 届く相手はいない（uvicorn は書き込まず、アクセスログにも出さない）
        else:
            raise TimeoutError
    except Exception as exc:  # Gemini 側のエラー・タイムアウト等
        failure = classify_llm_error(exc)
        # 詳細（例外本体・スタックトレース）はログにだけ残す。thread_id はパターン検証済み
        logger.exception(
            "LLM 呼び出しに失敗しました (status=%s, thread_id=%s)", failure.status_code, thread_id
        )
        raise failure from exc
    finally:
        await _cancel_and_wait(work, gone)

    return ChatResponse(
        reply=str(result["messages"][-1].text),
        thread_id=thread_id,
        context=result.get("context", ""),
        mode=mode,
    )
