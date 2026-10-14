import asyncio
import importlib
import logging
import math
import os
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncIterator
from contextlib import aclosing
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, StringConstraints, field_validator
from starlette.background import BackgroundTask

from app.agents.graph import STREAM_TOKENS_KEY, build_graph, is_mock_mode
from app.api.ui_stream import DONE, KEEPALIVE, UI_STREAM_HEADERS, UIMessageStream, encode, is_text_event
from app.history import get_checkpointer, history_backend

logger = logging.getLogger(__name__)

router = APIRouter()

MAX_MESSAGE_LENGTH = 2000
MAX_THREAD_ID_LENGTH = 128
DEFAULT_LLM_TIMEOUT_SECONDS = 60.0
# 取り消した LLM 呼び出し（と切断の監視）の終了を待つ上限。hedged_call 自身の後始末の上限
# （resilience._CANCEL_GRACE_SECONDS = 1 秒）より長くして、先に待ちを諦めないようにする
CANCEL_WAIT_SECONDS = 2.0
# /chat/stream で、イベントが無い間にこの秒数ごとに SSE のコメント行（ping）を送る。
# 無通信でプロキシに切られないことと、切断した相手への書き込みで中断に気づくことが目的
STREAM_KEEPALIVE_SECONDS = 10.0

# LLM 失敗時にクライアントへ返す固定メッセージ。例外の文字列は絶対に含めない
_DETAIL_RATE_LIMIT = "リクエストが集中しています。しばらく待ってから再試行してください。"
_DETAIL_TIMEOUT = "LLM の応答がタイムアウトしました。しばらく待ってから再試行してください。"
_DETAIL_CONFIG = "サーバー側の LLM 設定に問題があります。管理者に連絡してください。"
_DETAIL_UPSTREAM = "LLM 呼び出しに失敗しました。しばらく待ってから再試行してください。"
_DETAIL_HISTORY_STORE = "会話履歴の保存先に接続できませんでした。しばらく待ってから再試行してください。"


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
# 会話履歴の保存先（Postgres）の失敗。psycopg_pool の PoolTimeout なども psycopg.Error の子クラス
_HISTORY_STORE_ERRORS = _optional_classes(("psycopg", "Error"))


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


def get_graph(mock: bool):
    return _graph_for(mock, get_checkpointer())


@lru_cache(maxsize=4)
def _graph_for(mock: bool, checkpointer):
    # mock をキーにキャッシュするので、キー設定後は自動で Gemini 側のグラフに切り替わる。
    # checkpointer もキーに含める: 履歴の保存先は起動時（lifespan）に決まり、終了時に閉じるので、
    # 閉じた後のグラフを使い回さないため。None は従来どおりのメモリ（グラフごとの MemorySaver）
    return build_graph(checkpointer=checkpointer)


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
    """/chat の実行（LLM 呼び出しと履歴の保存）の例外を、内部情報を含まない HTTPException へ変換する。"""
    chain = _exception_chain(exc)
    codes = [_api_error_code(e) for e in chain]

    if _HISTORY_STORE_ERRORS and any(isinstance(e, _HISTORY_STORE_ERRORS) for e in chain):
        # LLM ではなく履歴の保存先の失敗。LLM の失敗として返すと、原因を取り違える
        return HTTPException(status_code=503, detail=_DETAIL_HISTORY_STORE)

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
    return {"status": "ok", "mode": current_mode(), "history": history_backend()}


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


class _GraphRun:
    """グラフの実行（astream）を別タスクで進め、イベントをキュー経由で受け渡す。

    LLM 呼び出しを取り消す手段（タスクの cancel）を、/chat と同じく自分で持つためと、レスポンスを
    始める前に最初のイベントを待てるようにするため。全体の上限（LLM_TIMEOUT_SECONDS）は、この中で
    実行時間に課す（超えると TimeoutError が error イベントになり、504 として扱われる）。

    next() の戻り値: ("event", (stream_mode, chunk)) / ("done", None) / ("error", 例外) /
    ("gone", None)（クライアントの切断）/ ("idle", None)（idle 秒の間、何も無かった）。
    """

    def __init__(self, graph: Any, inputs: dict, config: dict, request: Request, timeout: float) -> None:
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.producer: asyncio.Future = asyncio.ensure_future(self._produce(graph, inputs, config, timeout))
        # 本文は読み終えているので、ここから切断を監視できる（/chat と同じ）
        self.gone: asyncio.Future = asyncio.ensure_future(_until_disconnect(request))

    async def _produce(self, graph: Any, inputs: dict, config: dict, timeout: float) -> None:
        try:
            async with asyncio.timeout(timeout):
                async with aclosing(graph.astream(inputs, config, stream_mode=["custom", "values"])) as stream:
                    async for item in stream:
                        self._queue.put_nowait(("event", item))
            self._queue.put_nowait(("done", None))
        except Exception as exc:  # Gemini 側のエラー・タイムアウト・履歴の保存先の失敗 等
            self._queue.put_nowait(("error", exc))

    async def next(self, idle: float | None = None) -> tuple[str, Any]:
        if self.gone.done():  # 切断した相手には届かないので、溜まったイベントより切断を優先する
            return ("gone", None)
        getter: asyncio.Future = asyncio.ensure_future(self._queue.get())
        try:
            done, _ = await asyncio.wait({getter, self.gone}, timeout=idle, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            await _cancel_and_wait(getter)
            raise
        if self.gone in done:
            await _cancel_and_wait(getter)
            return ("gone", None)
        if getter in done:
            return getter.result()
        await _cancel_and_wait(getter)
        return ("idle", None)

    async def close(self) -> None:
        """実行と切断の監視を取り消して、終わるのを待つ（何度呼んでもよい）。"""
        await _cancel_and_wait(self.producer, self.gone)


def _log_stream_failure(exc: BaseException, failure: HTTPException, thread_id: str) -> None:
    # 詳細（例外本体・スタックトレース）はログにだけ残す。thread_id はパターン検証済み
    logger.error(
        "LLM 呼び出しに失敗しました (status=%s, thread_id=%s)",
        failure.status_code,
        thread_id,
        exc_info=(type(exc), exc, exc.__traceback__),
    )


async def _stream_body(
    run: _GraphRun,
    ui: UIMessageStream,
    buffered: list[tuple[str, Any]],
    thread_id: str,
    started: float,
) -> AsyncIterator[bytes]:
    """レスポンスの本体（SSE）。最初のイベントまでは、呼び出し側が待って buffered に溜めてある。"""
    try:
        for chunk in ui.start():
            yield encode(chunk)
        pending = deque(buffered)
        while True:
            if pending:
                kind, payload = pending.popleft()
            else:
                kind, payload = await run.next(idle=STREAM_KEEPALIVE_SECONDS)

            if kind == "event":
                for chunk in ui.feed(payload):
                    yield encode(chunk)
            elif kind == "done":
                for chunk in ui.finish():
                    yield encode(chunk)
                yield DONE
                return
            elif kind == "idle":
                yield KEEPALIVE
            elif kind == "gone":
                logger.info(
                    "クライアントが切断したため LLM 呼び出しを取り消しました (thread_id=%s, elapsed=%.1fs)",
                    thread_id,
                    time.monotonic() - started,
                )
                return
            else:  # error: 本文を送り始めた後の失敗。固定メッセージだけをクライアントへ送る
                failure = classify_llm_error(payload)
                _log_stream_failure(payload, failure, thread_id)
                for chunk in ui.error(str(failure.detail)):
                    yield encode(chunk)
                yield DONE
                return
    except asyncio.CancelledError:
        # Starlette は、クライアントの切断を見つけると、本体の生成を取り消す。失敗ではない
        logger.info(
            "ストリームを中断し、LLM 呼び出しを取り消しました (thread_id=%s, elapsed=%.1fs)",
            thread_id,
            time.monotonic() - started,
        )
        raise
    finally:
        await run.close()


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    responses={200: {"description": "AI SDK の UI message stream（SSE）", "content": {"text/event-stream": {}}}},
)
async def chat_stream(req: ChatRequest, request: Request) -> Response:
    """/chat と同じ入力で、回答を SSE で少しずつ返す。

    最初の本文が届くまで（または最後まで）は、レスポンスを始めない。その間の失敗は /chat と同じ
    HTTP ステータス（固定メッセージ）で返せる。本文を送り始めた後の失敗は、error チャンクで伝える。
    """
    mode = current_mode()
    thread_id = req.thread_id or str(uuid.uuid4())
    config = {"configurable": {"thread_id": thread_id, STREAM_TOKENS_KEY: True}}
    # context は直近のターンの検索結果だけを返したいので、毎ターン空に戻す
    inputs = {"messages": [HumanMessage(content=req.message)], "context": ""}
    started = time.monotonic()
    run: _GraphRun | None = None
    handed_off = False
    try:
        try:
            run = _GraphRun(get_graph(mode == "mock"), inputs, config, request, llm_timeout_seconds())
        except Exception as exc:  # グラフの組み立て（LLM クライアントの設定）の失敗
            failure = classify_llm_error(exc)
            _log_stream_failure(exc, failure, thread_id)
            raise failure from exc

        buffered: list[tuple[str, Any]] = []
        while True:
            kind, payload = await run.next()
            if kind == "gone":
                # 切断はエラーではない。ログも INFO・固定文・thread_id だけにする
                logger.info(
                    "クライアントが切断したため LLM 呼び出しを取り消しました (thread_id=%s, elapsed=%.1fs)",
                    thread_id,
                    time.monotonic() - started,
                )
                return Response(status_code=499)  # 届く相手はいない
            if kind == "error":
                failure = classify_llm_error(payload)
                _log_stream_failure(payload, failure, thread_id)
                raise failure from payload
            buffered.append((kind, payload))
            # 本文の最初の断片、または（入力ガードの拒否など）本文が無いまま終わったら、レスポンスを始める
            if kind == "done" or is_text_event(payload):
                break

        handed_off = True
        return StreamingResponse(
            _stream_body(run, UIMessageStream(str(uuid.uuid4()), thread_id, mode), buffered, thread_id, started),
            media_type="text/event-stream",
            headers=UI_STREAM_HEADERS,
            # 本体の生成が始まる前に切断されても、実行を取り残さない（_stream_body の finally は、始まらないと動かない）
            background=BackgroundTask(run.close),
        )
    finally:
        if not handed_off and run is not None:
            await run.close()
