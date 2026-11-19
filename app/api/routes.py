import asyncio
import importlib
import logging
import math
import os
import re
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import aclosing
from datetime import UTC, datetime
from functools import lru_cache
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from langchain_core.messages import HumanMessage
from pydantic import BaseModel, Field, StringConstraints, field_validator, model_validator
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse

from app.access import (
    DETAIL_FORBIDDEN_TARGET,
    DETAIL_UNAVAILABLE,
    Access,
    AccessUnavailable,
    request_access,
    target_is_visible,
)
from app.agents.graph import STREAM_TOKENS_KEY, build_graph, is_mock_mode
from app.agents.smalltalk import is_smalltalk
from app.agents.tools import DOCUMENT_IDS_KEY
from app.api.ui_stream import DONE, KEEPALIVE, UI_STREAM_HEADERS, UIMessageStream, encode
from app.auth import conversation_key, current_user
from app.history import get_checkpointer, history_backend
from app.metrics import ChatMetrics, detect_lang, new_request_id, trace_id_from, user_hash
from app.scope import ACCESS_SCOPE_KEY
from app.search.types import VectorSearchError

logger = logging.getLogger(__name__)


def _log_value(value: str) -> str:
    """ログに出す値から改行を除く（ログの行を偽装されないため）。"""
    return value.replace("\r", "_").replace("\n", "_")


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
_DETAIL_SEARCH = "資料の検索に失敗しました。しばらく待ってから再試行してください。"
# 検索対象が会話の途中で変えられたとき。グループ名は載せない（見えないグループの名前を、固定文から漏らさないため）
_DETAIL_SCOPE_CHANGED = (
    "この会話の検索対象と違う検索対象が送られました。会話の途中では変えられません。"
    "検索対象を変えるときは、新しい会話を始めてください。"
)
# 検索対象（資料のグループ）の識別名。documents.collection と同じ形（db/migrations/0001_schema.sql）
COLLECTION_PATTERN = r"^[A-Za-z0-9_-]{1,40}$"
# 質問の対象（documents.target）の識別名。document_targets.name と同じ形（db/migrations/0001_schema.sql）
TARGET_PATTERN = r"^[a-z0-9][a-z0-9-]{0,39}$"
_DETAIL_BOTH_SCOPES = "target と collection は同時に指定できません。"
_DETAIL_DOCUMENTS_AND_SCOPE = "document_ids は target・collection と同時に指定できません。"
# 一度に選べる資料の数。資料一覧の上限（app/api/documents.py の _MAX_DOCUMENTS）と同じ
MAX_DOCUMENT_IDS = 500
_MAX_DOCUMENT_ID = 9_223_372_036_854_775_807  # bigint の上限（DB に渡す前に 422 にする）


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
# 会話履歴の保存先（DB）の失敗。本番は pg（Postgres）で、app/pg.py も DB の失敗を D1Error で送出する。
# d1（データ用 Worker 経由）は退役済みで、テストの偽 DB（tests/d1_fakes.py）のために残る
_HISTORY_STORE_ERRORS = _optional_classes(("app.d1", "D1Error"))


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

    collection: str | None = Field(
        default=None,
        pattern=COLLECTION_PATTERN,
        description=(
            "検索対象の資料のグループ（documents.collection の識別名）。省略は、新しい会話なら「すべて」、"
            "続きの会話ならその会話の検索対象のまま。会話ごとに固定で、続きの会話で違う値を送ると 409。"
            "旧方式（互換のために残す）。画面は target を使う。target と同時には指定できない（422）"
        ),
    )
    target: str | None = Field(
        default=None,
        pattern=TARGET_PATTERN,
        description=(
            "質問の対象（documents.target の識別名）。省略は、新しい会話なら「対象を絞らない」、"
            "続きの会話ならその会話の対象のまま。会話ごとに固定で、続きの会話で違う値（保存が対象なし・旧 collection の会話を含む）を送ると 409。"
            "新しい会話で、見えない・存在しない・資料のない対象を送ると 403。collection と同時には指定できない（422）"
        ),
    )

    document_ids: list[Annotated[int, Field(ge=1, le=_MAX_DOCUMENT_ID)]] | None = Field(
        default=None,
        min_length=1,
        max_length=MAX_DOCUMENT_IDS,
        description=(
            "このターンの回答に使う資料（documents.id）。画面でチェックを付けた資料で、ターンごとに送る（会話には固定されない）。"
            "送ると、会話の対象（target）・旧グループ（collection）に代わって、見える資料の中のこの資料だけを探す。"
            "見えない・存在しない資料は黙って除く（全部そうなら検索は 0 件）。省略は資料で絞らない。空の並びは 422"
            "（「選ばない」を「全部」と読み替えない）。target / collection と同時には指定できない（422）"
        ),
    )

    @model_validator(mode="after")
    def _one_scope(self) -> "ChatRequest":
        # 対象と旧グループは、同時に指定できない（新しい会話でも続きの会話でも）。黙って片方を捨てると、範囲が広がるため
        if self.target is not None and self.collection is not None:
            raise ValueError(_DETAIL_BOTH_SCOPES)
        if self.document_ids is not None and (self.target is not None or self.collection is not None):
            raise ValueError(_DETAIL_DOCUMENTS_AND_SCOPE)
        return self

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
    collection: str | None = Field(
        default=None, description="この会話の検索対象の資料のグループ（旧方式）。null は絞らない"
    )
    target: str | None = Field(default=None, description="この会話の質問の対象（documents.target）。null は絞らない")
    hit_count: int | None = Field(
        default=None, description="今ターンの検索で拾ったチャンクの数。検索していなければ null（0 件とは別）"
    )


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

    if any(isinstance(e, VectorSearchError) for e in chain):
        # 資料の検索（質問の埋め込み・DB）の失敗。LLM の失敗として返すと、原因を取り違える。別の検索経路には戻らない
        return HTTPException(status_code=503, detail=_DETAIL_SEARCH)

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


async def _check_scope(
    graph: Any, config: dict, requested: str | None, access: Access, requested_target: str | None = None
) -> None:
    """続きの会話の検索対象を確かめる。

    - 会話に保存された検索対象（collection）が、いま見えないグループなら 403（固定文。グループ名は載せない）。
    - 送られた collection / target が会話の検索対象と違えば 409（固定文。名前は載せない）。保存が「対象なし」でも、違う指定は 409。
    - 新しい会話で target が送られたら、その対象に見える資料があるかを確かめ、無ければ 403（固定文。見えない・存在しない・資料のない対象は同じ）。
      続きの会話の対象は、確かめ直さない（資料が移って空になっても、会話は続けられる。検索は 0 件になり、画面が知らせる）。

    検索対象は会話ごとに固定で、最初の発言でグラフの状態（チェックポイント）に残る。省略（None）は何も主張しないので、
    アクセス制御で範囲を絞っていないときは、読まずに通す（検索対象は tools ノードが状態から復元する）。
    この項目が無い古い会話は「すべて」として扱う。履歴の保存先の失敗は、`classify_llm_error` と同じ固定文の HTTPException にする。
    """
    if requested is None and requested_target is None and not access.restricts and access.scope.shadow is None:
        return
    try:
        snapshot = await graph.aget_state(config)
    except Exception as exc:  # noqa: BLE001 - 履歴の保存先の失敗。例外の文字列は応答に含めない
        failure = classify_llm_error(exc)
        logger.error("会話の検索対象を読めませんでした (status=%s, %s)", failure.status_code, type(exc).__name__)
        raise failure from None
    values = snapshot.values or {}
    if not values.get("messages"):
        # 新しい会話（まだ何も保存されていない）。対象を指定したなら、見える資料のある対象かを確かめる
        if requested_target is not None:
            await _require_visible_target(access, requested_target)
        return
    current = values.get("collection")
    access.check_collection(current, "会話の検索対象")
    if requested is not None and current != requested:
        raise HTTPException(status_code=409, detail=_DETAIL_SCOPE_CHANGED)
    if requested_target is not None and values.get("target") != requested_target:
        raise HTTPException(status_code=409, detail=_DETAIL_SCOPE_CHANGED)


async def _require_visible_target(access: Access, target: str) -> None:
    try:
        visible = await target_is_visible(access.scope, target)
    except AccessUnavailable:
        raise HTTPException(status_code=503, detail=DETAIL_UNAVAILABLE) from None
    if not visible:
        raise HTTPException(status_code=403, detail=DETAIL_FORBIDDEN_TARGET)


def _add_document_ids(config: dict, document_ids: list[int] | None) -> None:
    """今ターンで選ばれた資料を、検索ツールへ渡す config に入れる（会話の状態＝チェックポイントには残さない）。"""
    if document_ids is not None:
        config["configurable"][DOCUMENT_IDS_KEY] = tuple(sorted(set(document_ids)))


def _turn_inputs(message: str, collection: str | None, target: str | None = None) -> dict:
    """1 ターンの入力。context・sources・hit_count は直近のターンの検索結果だけを返したいので、毎ターン空に戻す。

    collection と target は、送られたときだけ入れる（省略なら入れない。続きの会話の検索対象は、状態に残っているものを使う）。
    """
    inputs: dict[str, Any] = {
        # 受け付けた時刻（UTC）を質問に持たせる。履歴（`GET /threads/{id}/messages`）の `sent_at` になる。LLM には渡らない
        "messages": [HumanMessage(content=message, additional_kwargs={"sent_at": datetime.now(UTC).isoformat()})],
        "context": "",
        "sources": [],
        "hit_count": None,
    }
    if collection is not None:
        inputs["collection"] = collection
    if target is not None:
        inputs["target"] = target
    return inputs


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


DRAIN_MESSAGE = "サーバーを入れ替えています。少し待ってから、もう一度お試しください。"
DRAIN_RETRY_AFTER_SECONDS = 5


def _drain_wait(request: Request) -> "asyncio.Future[None] | None":
    """停止の合図（app/drain.py）を待つタスク。合図の置き場（app.state.drain）が無ければ None（何も変わらない）。"""
    drain = getattr(request.app.state, "drain", None)
    return asyncio.ensure_future(drain.wait()) if drain is not None else None


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


def _new_metrics(
    endpoint: str, request: Request, req: "ChatRequest", user_id: str | None, thread_id: str, *, stream: bool
) -> ChatMetrics:
    """応答 1 回ぶんの指標（app/metrics.py）。質問の本文は、区分（挨拶か・言語）に直してから渡し、ログには残さない。"""
    return ChatMetrics(
        endpoint,
        stream=stream,
        request_id=new_request_id(),
        trace_id=trace_id_from(request.headers.get("x-cloud-trace-context")),
        session_id=thread_id,
        user_id_hash=user_hash(user_id),
        smalltalk=is_smalltalk(req.message),
        lang=detect_lang(req.message),
    )


@router.post("/chat", response_model=ChatResponse)
async def chat(
    req: ChatRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user)],
    access: Annotated[Access, Depends(request_access)],
) -> ChatResponse | Response:
    mode = current_mode()
    thread_id = req.thread_id or str(uuid.uuid4())
    access.require_any()
    access.check_collection(req.collection, "チャット")
    # 履歴のキーは、ログイン中なら利用者 ID つき。クライアントに返す thread_id は接頭辞なしのまま。
    # 検索できる範囲（ACCESS_SCOPE_KEY）は、会話の状態ではなく config で渡す（チェックポイントに残さない）
    metrics = _new_metrics("chat", request, req, user_id, thread_id, stream=False)
    config = {
        "configurable": {"thread_id": conversation_key(user_id, thread_id), ACCESS_SCOPE_KEY: access.scope},
        "callbacks": [metrics],
    }
    _add_document_ids(config, req.document_ids)
    started = time.monotonic()
    work: asyncio.Future | None = None
    gone: asyncio.Future | None = None
    drain: asyncio.Future | None = None
    result: dict | None = None
    status = "ok"
    error: BaseException | None = None
    try:
        graph = get_graph(mode == "mock")
        await _check_scope(graph, config, req.collection, access, req.target)
        work = asyncio.ensure_future(graph.ainvoke(_turn_inputs(req.message, req.collection, req.target), config))
        # 本文は読み終えているので、ここから切断を監視できる。切断が先に来たら、待っても受け取る相手が
        # いない LLM 呼び出し（ヘッジの追加試行を含む）を取り消す
        gone = asyncio.ensure_future(_until_disconnect(request))
        drain = _drain_wait(request)
        waiting = {work, gone} if drain is None else {work, gone, drain}
        done, _ = await asyncio.wait(waiting, timeout=llm_timeout_seconds(), return_when=asyncio.FIRST_COMPLETED)
        if work in done:  # 同時に終わったときは結果を優先する
            result = work.result()
        elif gone in done:
            # 切断はエラーではない。except Exception の外で返し、ログも INFO・固定文・thread_id だけにする
            logger.info(
                "クライアントが切断したため LLM 呼び出しを取り消しました (thread_id=%s, elapsed=%.1fs)",
                _log_value(thread_id),
                time.monotonic() - started,
            )
            status = "gone"
            return Response(status_code=499)  # 届く相手はいない（uvicorn は書き込まず、アクセスログにも出さない）
        elif drain is not None and drain in done:
            # サーバーの停止（SIGTERM）。待たずに打ち切り、画面から再試行できるようにする
            status = "shutdown"
            return JSONResponse(
                {"detail": DRAIN_MESSAGE}, status_code=503, headers={"Retry-After": str(DRAIN_RETRY_AFTER_SECONDS)}
            )
        else:
            raise TimeoutError
    except HTTPException as exc:
        status = str(exc.status_code)
        error = exc
        raise  # 検索対象の不一致（409）など、すでに固定文になっているもの
    except Exception as exc:  # Gemini 側のエラー・タイムアウト等
        failure = classify_llm_error(exc)
        status = str(failure.status_code)
        error = exc
        # 詳細（例外本体・スタックトレース）はログにだけ残す。thread_id はパターン検証済み
        logger.exception(
            "LLM 呼び出しに失敗しました (status=%s, thread_id=%s)", failure.status_code, _log_value(thread_id)
        )
        raise failure from exc
    finally:
        await _cancel_and_wait(work, gone, drain)
        metrics.log(status, result if isinstance(result, dict) else None, error)

    return ChatResponse(
        reply=str(result["messages"][-1].text),
        thread_id=thread_id,
        context=result.get("context", ""),
        mode=mode,
        collection=result.get("collection"),
        target=result.get("target"),
        hit_count=result.get("hit_count"),
    )


class _GraphRun:
    """グラフの実行（astream）を別タスクで進め、イベントをキュー経由で受け渡す。

    LLM 呼び出しを取り消す手段（タスクの cancel）を、/chat と同じく自分で持つためと、レスポンスを
    始める前に最初のイベントを待てるようにするため。全体の上限（LLM_TIMEOUT_SECONDS）は、この中で
    実行時間に課す（超えると TimeoutError が error イベントになり、504 として扱われる）。

    next() の戻り値: ("event", (stream_mode, chunk)) / ("done", None) / ("error", 例外) /
    ("gone", None)（クライアントの切断）/ ("drain", None)（サーバーの停止。app/drain.py）/ ("idle", None)（idle 秒の間、何も無かった）。
    """

    def __init__(self, graph: Any, inputs: dict, config: dict, request: Request, timeout: float) -> None:
        self._queue: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()
        self.producer: asyncio.Future = asyncio.ensure_future(self._produce(graph, inputs, config, timeout))
        # 本文は読み終えているので、ここから切断を監視できる（/chat と同じ）
        self.gone: asyncio.Future = asyncio.ensure_future(_until_disconnect(request))
        self.drain: asyncio.Future | None = _drain_wait(request)  # サーバーの停止の合図（無ければ None）

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
        waiting = {getter, self.gone} if self.drain is None else {getter, self.gone, self.drain}
        try:
            done, _ = await asyncio.wait(waiting, timeout=idle, return_when=asyncio.FIRST_COMPLETED)
        except BaseException:
            await _cancel_and_wait(getter)
            raise
        if self.gone in done:
            await _cancel_and_wait(getter)
            return ("gone", None)
        if getter in done:
            return getter.result()
        if self.drain is not None and self.drain in done:
            await _cancel_and_wait(getter)
            return ("drain", None)
        await _cancel_and_wait(getter)
        return ("idle", None)

    async def close(self) -> None:
        """実行と切断の監視を取り消して、終わるのを待つ（何度呼んでもよい）。"""
        await _cancel_and_wait(self.producer, self.gone, self.drain)


def _log_stream_failure(exc: BaseException, failure: HTTPException, thread_id: str) -> None:
    # 詳細（例外本体・スタックトレース）はログにだけ残す。thread_id はパターン検証済み
    logger.error(
        "LLM 呼び出しに失敗しました (status=%s, thread_id=%s)",
        failure.status_code,
        _log_value(thread_id),
        exc_info=(type(exc), exc, exc.__traceback__),
    )


async def _stream_body(
    run: _GraphRun, ui: UIMessageStream, thread_id: str, started: float, metrics: ChatMetrics | None = None
) -> AsyncIterator[bytes]:
    """レスポンスの本体（SSE）。始めてすぐ start を送り、グラフの進み具合（status）と本文を、届いた順に流す。"""
    status = "cancelled"  # 途中で取り消されたとき（下の CancelledError）。ほかは、各分岐で決める
    state: dict | None = None  # グラフの最終状態（最後の values）。指標のログに使う
    error: BaseException | None = None
    try:
        for chunk in ui.start():
            yield encode(chunk)
        while True:
            kind, payload = await run.next(idle=STREAM_KEEPALIVE_SECONDS)

            if kind == "event":
                if payload[0] == "values" and isinstance(payload[1], dict):
                    state = payload[1]
                for chunk in ui.feed(payload):
                    if metrics is not None and chunk.get("type") == "text-delta":
                        metrics.mark_first_text()  # TTFT = 最初の本文を送った時刻
                    yield encode(chunk)
            elif kind == "done":
                status = "ok"
                for chunk in ui.finish():
                    if metrics is not None and chunk.get("type") == "text-delta":
                        metrics.mark_first_text()
                    yield encode(chunk)
                yield DONE
                return
            elif kind == "idle":
                yield KEEPALIVE
            elif kind == "gone":
                status = "gone"
                logger.info(
                    "クライアントが切断したため LLM 呼び出しを取り消しました (thread_id=%s, elapsed=%.1fs)",
                    _log_value(thread_id),
                    time.monotonic() - started,
                )
                return
            elif kind == "drain":
                status = "shutdown"
                logger.info(
                    "サーバーの停止のため、進行中の応答を打ち切りました (thread_id=%s, elapsed=%.1fs)",
                    _log_value(thread_id),
                    time.monotonic() - started,
                )
                for chunk in ui.error(DRAIN_MESSAGE, status=503, retry_after=DRAIN_RETRY_AFTER_SECONDS):
                    yield encode(chunk)
                yield DONE
                return
            else:  # error: レスポンスを始めた後の失敗。固定メッセージだけをクライアントへ送る
                failure = classify_llm_error(payload)
                status = str(failure.status_code)
                error = payload
                _log_stream_failure(payload, failure, thread_id)
                retry_after = (failure.headers or {}).get("Retry-After", "")
                for chunk in ui.error(
                    str(failure.detail),
                    status=failure.status_code,
                    retry_after=int(retry_after) if retry_after.isdigit() else None,
                ):
                    yield encode(chunk)
                yield DONE
                return
    except asyncio.CancelledError:
        # Starlette は、クライアントの切断を見つけると、本体の生成を取り消す。失敗ではない
        logger.info(
            "ストリームを中断し、LLM 呼び出しを取り消しました (thread_id=%s, elapsed=%.1fs)",
            _log_value(thread_id),
            time.monotonic() - started,
        )
        raise
    finally:
        await run.close()
        if metrics is not None:
            metrics.log(status, state, error)


@router.post(
    "/chat/stream",
    response_class=StreamingResponse,
    responses={200: {"description": "AI SDK の UI message stream（SSE）", "content": {"text/event-stream": {}}}},
)
async def chat_stream(
    req: ChatRequest,
    request: Request,
    user_id: Annotated[str | None, Depends(current_user)],
    access: Annotated[Access, Depends(request_access)],
) -> Response:
    """/chat と同じ入力で、回答を SSE で少しずつ返す。

    入力検証・認証・グラフの組み立てが済んだら、すぐレスポンスを始める（最初の本文までに 20 秒以上かかることがあり、
    その間、画面に段階を見せるため）。その後の失敗は HTTP ステータスを変えられないので、error チャンクで伝える
    （ステータスと Retry-After は、直前の data-failure で伝える）。
    """
    mode = current_mode()
    thread_id = req.thread_id or str(uuid.uuid4())
    access.require_any()
    access.check_collection(req.collection, "チャット")
    metrics = _new_metrics("chat_stream", request, req, user_id, thread_id, stream=True)
    config = {
        "configurable": {
            "thread_id": conversation_key(user_id, thread_id),
            STREAM_TOKENS_KEY: True,
            ACCESS_SCOPE_KEY: access.scope,
        },
        "callbacks": [metrics],
    }
    _add_document_ids(config, req.document_ids)
    inputs = _turn_inputs(req.message, req.collection, req.target)
    started = time.monotonic()
    try:
        graph = get_graph(mode == "mock")
    except (
        Exception
    ) as exc:  # グラフの組み立て（LLM クライアントの設定）の失敗。まだレスポンスを始めていないので、HTTP で返せる
        failure = classify_llm_error(exc)
        _log_stream_failure(exc, failure, thread_id)
        raise failure from exc
    # 検索対象の不一致（409）と見えないグループ（403）は、レスポンスを始める前に HTTP で返す（422・401 と同じ）
    await _check_scope(graph, config, req.collection, access, req.target)
    run = _GraphRun(graph, inputs, config, request, llm_timeout_seconds())

    return StreamingResponse(
        _stream_body(run, UIMessageStream(str(uuid.uuid4()), thread_id, mode), thread_id, started, metrics),
        media_type="text/event-stream",
        headers=UI_STREAM_HEADERS,
        # 本体の生成が始まる前に切断されても、実行を取り残さない（_stream_body の finally は、始まらないと動かない）
        background=BackgroundTask(run.close),
    )
