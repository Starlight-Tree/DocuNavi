"""LLM 呼び出しの「遅い・止まる」への対処（ヘッジ付きの再試行）。

Gemini は 1〜3 秒で返す回と、20 秒以上かかる回・無応答のまま 60 秒でサーバーに打ち切られる回が
混在することがある。同じ要求をもう一度投げ直すと速く返ることが多いので、遅い呼び出しを待たずに
追加の試行を投げ、最初に成功したものを採る（ヘッジ）。失敗した場合の再試行も同じ仕組みで行う。

- 最初の 1 本を投げ、`hedge_delay` 秒たっても結果が無ければ、次の試行を追加で投げる（最大 `max_attempts` 本）。
- 投げた試行が全て失敗していれば、`retry_delay` 秒後に次を投げる。結果待ちの試行が残っているときは、
  その結果か次のヘッジの時刻まで待つ（投げ直しても先の試行が成功すれば足りるため）。
- 成功したら残りの試行は取り消す。取り消した試行の料金・クォータは Gemini 側の扱い次第。
  取り消した試行には LangChain のコールバック（トレース）に終了が通知されない。
- 一時的な失敗（無応答・接続系・408/500/502/503/504）だけを再試行する。認証・不正なリクエストなどは
  即座に失敗させる。
- 429（レート制限）は、以降の追加を止めたうえで、間隔を空けて再試行する。叩き続けないため。
  全ての試行が失敗したときは、最後の失敗ではなく 429 を返す（呼び出し側が待ってから再送できるように）。

ストリーミング（hedged_stream）は、同じ仕組みで「最初のチャンクが届いた試行」を採る。採った後は、
その試行が最後まで流れる（途中で失敗しても、別の試行へは切り替えない。利用者へ送った本文が変わるため）。
"""

import asyncio
import enum
import importlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE_CODES = {408, 500, 502, 503, 504}
_RATE_LIMIT_CODE = 429
# 負けた試行の取り消しを待つ上限。取り消しを無視する試行があっても、呼び出しが戻らなくならないようにする
_CANCEL_GRACE_SECONDS = 1.0


class AttemptCancelled(Exception):
    """試行が（hedged_call の取り消しによらず）自分で取り消された。一時的な失敗として扱う。"""


class EmptyStream(Exception):
    """ストリームがチャンクを 1 つも返さずに終わった。一時的な失敗として扱う。"""


def _optional_classes(*specs: tuple[str, str]) -> tuple[type[BaseException], ...]:
    """(モジュール, クラス名) のうち import できた例外クラスだけを返す（ライブラリのバージョン差対策）。"""
    found: list[type[BaseException]] = []
    for module_name, class_name in specs:
        try:
            cls = getattr(importlib.import_module(module_name), class_name)
        except Exception:  # ImportError / AttributeError 等
            continue
        if isinstance(cls, type) and issubclass(cls, BaseException):
            found.append(cls)
    return tuple(found)


# 無応答（タイムアウト）や接続の失敗。リクエストの内容が悪いわけではないので、投げ直せば通りうる
_TRANSIENT_ERRORS: tuple[type[BaseException], ...] = (
    TimeoutError,
    ConnectionError,
    AttemptCancelled,
    EmptyStream,
) + _optional_classes(
    ("httpx", "TransportError"),  # TimeoutException / ConnectError / RemoteProtocolError などの親
    ("httpx2", "TransportError"),
    ("langchain_core.exceptions", "ModelTimeoutError"),
    ("langchain_core.exceptions", "ModelConnectionError"),
)
_RATE_LIMIT_ERRORS = _optional_classes(("langchain_core.exceptions", "ModelRateLimitError"))


class Failure(enum.Enum):
    RETRY = "retry"  # 一時的な失敗。すぐ投げ直してよい
    RATE_LIMITED = "rate_limited"  # 429。間隔を空け、追加は止める
    FATAL = "fatal"  # 投げ直しても同じ。即座に失敗させる


def exception_chain(exc: BaseException) -> list[BaseException]:
    """exc とその原因を辿った一覧（ラッパー例外の裏に本当の原因があるため）。"""
    chain: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in chain and len(chain) < 10:
        chain.append(current)
        current = current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    return chain


def classify_failure(exc: BaseException) -> Failure:
    chain = exception_chain(exc)
    codes = [c for e in chain if isinstance(c := getattr(e, "code", None), int) and not isinstance(c, bool)]
    if any(isinstance(e, _RATE_LIMIT_ERRORS) for e in chain) or _RATE_LIMIT_CODE in codes:
        return Failure.RATE_LIMITED
    if any(isinstance(e, _TRANSIENT_ERRORS) for e in chain) or any(c in _RETRYABLE_CODES for c in codes):
        return Failure.RETRY
    return Failure.FATAL


@dataclass(frozen=True)
class HedgePolicy:
    hedge_delay: float  # 直前の投入からこの秒数たっても結果が無ければ、次の試行を追加で投げる
    max_attempts: int  # 投げる試行の最大数（最初の 1 本を含む）
    retry_delay: float = 0.5  # 全ての試行が失敗したとき、次を投げるまでの待ち
    rate_limit_delay: float = 2.0  # 429 のとき、次を投げるまでの待ち（429 の回数に比例して延ばす）


def _consume(task: "asyncio.Future[object]") -> None:
    """終わった試行の例外を取り出す（取り出さないと「Task exception was never retrieved」になる）。"""
    if not task.cancelled():
        task.exception()


async def _cancel_and_drain(
    tasks: set["asyncio.Future[T]"], discard: Callable[[T], Awaitable[None]] | None = None
) -> None:
    """残っている試行を取り消し、終わるのを待つ（タスクを取り残さない）。

    取り消しを無視する試行があっても戻れるよう、待ちには上限を設ける。後始末の最中にもう一度取り消された
    （外側の wait_for の期限切れなど）場合も、試行の終了は上限まで待ってから、その取り消しを伝える。
    discard があれば、取り消しと入れ違いに成功していた試行の結果を渡す（閉じる必要のある資源を残さない）。
    """
    if not tasks:
        return
    loop = asyncio.get_running_loop()
    deadline = loop.time() + _CANCEL_GRACE_SECONDS
    for task in tasks:
        task.add_done_callback(_consume)
        task.cancel()
    interrupted = False
    while (remaining := deadline - loop.time()) > 0:
        try:
            await asyncio.wait(tasks, timeout=remaining)
            break
        except asyncio.CancelledError:
            interrupted = True
    if discard is not None:
        for task in tasks:
            if task.done() and not task.cancelled() and task.exception() is None:
                try:
                    await discard(task.result())
                except Exception:
                    logger.debug("採らなかった試行の後始末に失敗しました", exc_info=True)
    if interrupted:
        raise asyncio.CancelledError


async def hedged_call(
    make_attempt: Callable[[], Awaitable[T]],
    policy: HedgePolicy,
    *,
    name: str = "LLM",
    discard: Callable[[T], Awaitable[None]] | None = None,
) -> T:
    """make_attempt() を policy に従って投げ、最初に成功した結果を返す。

    全ての試行が失敗したときは、429 があればその失敗を、無ければ最後の失敗を送出する。
    呼び出し元がキャンセルされたときは、投げた試行を全て取り消してから戻る（タスクを取り残さない）。
    discard は、採らなかった成功結果（同じ回に複数が成功したときの残り）を後始末するための関数。
    閉じる必要のある資源（開いたままのストリームなど）を返す試行で使う。
    """
    loop = asyncio.get_running_loop()
    started = loop.time()
    pending: set[asyncio.Future[T]] = set()
    launch_order: dict[asyncio.Future[T], int] = {}  # 同じ回に複数が終わったとき、投げた順に扱うため
    launched = 0
    rate_limited = 0
    hedging = True  # 429 を受けたら False にして、時間経過による追加を止める
    last_exc: BaseException | None = None
    rate_limit_exc: BaseException | None = None
    next_launch_at = started

    def launch(reason: str) -> None:
        nonlocal launched, next_launch_at
        try:
            task: asyncio.Future[T] = asyncio.ensure_future(make_attempt())
        except Exception as exc:  # 同期的に失敗した（awaitable を返さなかった場合を含む）。試行の失敗として扱う
            task = loop.create_future()
            task.set_exception(exc)
        launch_order[task] = launched
        pending.add(task)
        launched += 1
        if launched > 1:
            logger.warning(
                "%s: %s（%d/%d 本目、開始から %.1f 秒）", name, reason, launched, policy.max_attempts, loop.time() - started
            )
        next_launch_at = loop.time() + policy.hedge_delay

    try:
        launch("")
        while True:
            if not pending:
                # 投げた試行が全て失敗している。待つ相手がいないので、待たずに（429 は間隔を空けて）次を投げる
                if launched >= policy.max_attempts:
                    break
                await asyncio.sleep(policy.rate_limit_delay * rate_limited if rate_limited else policy.retry_delay)
                launch("失敗したため再試行します")

            can_launch = launched < policy.max_attempts
            wait_for = max(0.0, next_launch_at - loop.time()) if (can_launch and hedging) else None
            done, _ = await asyncio.wait(pending, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED)

            failed: list[BaseException] = []
            for task in sorted(done, key=launch_order.__getitem__):
                pending.discard(task)
                if task.cancelled():
                    # 呼び出し元の取り消しなら、この await 自体が CancelledError になる。ここに来るのは試行の自己都合
                    failed.append(AttemptCancelled(f"{name}: 試行が取り消されました"))
                    continue
                exc = task.exception()
                if exc is None:
                    if launched > 1:
                        logger.info("%s: %.1f 秒で成功（投げた試行 %d 本）", name, loop.time() - started, launched)
                    if discard is not None:
                        for other in done:
                            if other is not task and not other.cancelled() and other.exception() is None:
                                await discard(other.result())
                    return task.result()
                failed.append(exc)

            for exc in failed:
                last_exc = exc
                kind = classify_failure(exc)
                if kind is Failure.FATAL:
                    raise exc
                logger.warning("%s: 試行が失敗しました（%s / %s）", name, type(exc).__name__, kind.value)
                if kind is Failure.RATE_LIMITED:
                    rate_limited += 1
                    hedging = False
                    rate_limit_exc = rate_limit_exc or exc

            if not done and can_launch and hedging:
                launch(f"{policy.hedge_delay:g} 秒応答が無いため、追加の試行を投げます")

        # 全ての試行が失敗した場合だけここに来る
        raise rate_limit_exc or last_exc or RuntimeError(f"{name}: 試行が結果を返さずに終了しました")
    finally:
        await _cancel_and_drain(pending, discard)


async def _aclose(stream: object) -> None:
    """非同期イテレーターを閉じる（aclose を持たないものは何もしない）。閉じる失敗は呼び出し側を止めない。"""
    close = getattr(stream, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.debug("ストリームを閉じる際に例外が出ました", exc_info=True)


async def hedged_stream(
    make_stream: Callable[[], AsyncIterator[T]], policy: HedgePolicy, *, name: str = "LLM"
) -> AsyncIterator[T]:
    """make_stream() が返すストリームを policy に従って投げ、最初のチャンクが届いた試行のチャンクを流す。

    ヘッジ・再試行・429 の扱いは hedged_call と同じで、「結果」を「最初のチャンク」に読み替える。
    最初のチャンクの前に全ての試行が失敗したときは、その失敗を送出する。最初のチャンクの後の失敗は、
    そのまま送出する（別の試行に切り替えると、先に送った本文と食い違うため）。
    採らなかった試行のストリームは閉じる。呼び出し側が途中でやめたとき（aclose・取り消し）も閉じる。
    """

    async def attempt() -> tuple[T, AsyncIterator[T]]:
        stream = aiter(make_stream())
        try:
            first = await anext(stream)
        except StopAsyncIteration:
            await _aclose(stream)
            raise EmptyStream(f"{name}: 応答が空でした") from None
        except BaseException:
            await _aclose(stream)
            raise
        return first, stream

    async def discard(result: tuple[T, AsyncIterator[T]]) -> None:
        await _aclose(result[1])

    first, stream = await hedged_call(attempt, policy, name=name, discard=discard)
    try:
        yield first
        async for item in stream:
            yield item
    finally:
        await _aclose(stream)
