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

- 主モデルの試行が一時的な失敗で終わり、フォールバックがあるときは（`early_fallback`）、主モデルへの再試行ではなく
  フォールバックをすぐ投げる。落ちている主モデルへ同じ要求を投げ直して、失敗の待ちを試行の数だけ重ねない。
- 最初の結果を待つ総時間には上限を置ける（`deadline`）。超えたら投げた試行を全て取り消し、TimeoutError にする。

ストリーミング（hedged_stream）は、同じ仕組みで「最初のチャンクが届いた試行」を採る。採った後は、
その試行が最後まで流れる（途中で失敗しても、別の試行へは切り替えない。利用者へ送った本文が変わるため）。

フォールバックのモデルがあるときは、最初のヘッジの時刻（hedge_delay 秒たっても結果が無い）に、主モデルの
追加の試行の代わりに、フォールバックを 1 本だけ並行して投げる。先に結果（最初のチャンク）が届いた方を採る。
主モデルが落ちて無応答のまま止まっていても、フォールバックの分だけで済む（試行の上限まで待たない）。
"""

import asyncio
import enum
import importlib
import logging
import math
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import aclosing
from dataclasses import dataclass
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE_CODES = {408, 500, 502, 503, 504}
_RATE_LIMIT_CODE = 429
DEFAULT_KEY_SWITCH_HOLD_SECONDS = 60.0
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
    # 主モデルの試行が一時的な失敗（5xx・接続）で終わり、fallback があるときは、主モデルの再試行ではなく fallback を
    # すぐ投げる（同じ落ち方の主モデルへ投げ直して、失敗を max_attempts 回待たない）。429 のときは使わない
    early_fallback: bool = False
    # 最初の結果（ストリームなら最初のチャンク）を待つ総時間の上限（秒。None は上限なし）。超えたら TimeoutError
    deadline: float | None = None


@dataclass
class FallbackTrace:
    """1 回の LLM 呼び出しの中で、フォールバックを試したか・採ったか（呼び出しごとに新しく作る）。"""

    tried: bool = False  # すでにフォールバックを投げた（同じ呼び出しの中では、もう投げない）
    won: bool = False  # フォールバックの結果を採った（主モデルより先に届いた）
    # 主モデルの試行が、実際に一時的な失敗（5xx・接続・タイムアウト・空の応答）で終わっていた。
    # 遅いだけ（まだ結果もエラーも無い）なら立たない。ブレーカーを開けるかの判断に使う
    primary_failed: bool = False


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
    fallback: Callable[[], Awaitable[T]] | None = None,
    fallback_name: str = "フォールバック",
    trace: FallbackTrace | None = None,
) -> T:
    """make_attempt() を policy に従って投げ、最初に成功した結果を返す。

    全ての試行が失敗したときは、429 があればその失敗を、無ければ最後の失敗を送出する。
    呼び出し元がキャンセルされたときは、投げた試行を全て取り消してから戻る（タスクを取り残さない）。
    discard は、採らなかった成功結果（同じ回に複数が成功したときの残り）を後始末するための関数。
    閉じる必要のある資源（開いたままのストリームなど）を返す試行で使う。

    fallback があるとき（trace.tried が立っていないとき）は、最初の時間経過によるヘッジで、make_attempt の
    追加の代わりに fallback() を 1 本だけ並行して投げる（試行数 max_attempts に数える）。先に成功した方を採り、
    fallback の結果を採ったら trace.won を立てる。fallback の失敗（429・設定の失敗を含む）は、主モデルの
    試行の扱いに影響させず、ログだけ残す（全ての試行が失敗したときに送出するのは、主モデルの失敗）。
    429 を受けて追加を止めているとき、max_attempts が 1 のときは、並行して投げない。
    """
    if trace is None:
        trace = FallbackTrace()
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
    fallback_tasks: set[asyncio.Future[T]] = set()  # 並行して投げたフォールバックの試行
    primary_retryable_failure = False  # 直前の回に、主モデルの試行が一時的な失敗で終わった（early_fallback の判断用）

    def _expired() -> bool:
        return policy.deadline is not None and loop.time() >= started + policy.deadline

    def _bounded(seconds: float) -> float:
        """待ち時間を、総時間の上限（policy.deadline）の残りに収める。"""
        if policy.deadline is None:
            return seconds
        return max(0.0, min(seconds, started + policy.deadline - loop.time()))

    def launch(reason: str, *, use_fallback: bool = False) -> None:
        nonlocal launched, next_launch_at
        factory = fallback if use_fallback and fallback is not None else make_attempt
        try:
            task: asyncio.Future[T] = asyncio.ensure_future(factory())
        except Exception as exc:  # 同期的に失敗した（awaitable を返さなかった場合を含む）。試行の失敗として扱う
            task = loop.create_future()
            task.set_exception(exc)
        launch_order[task] = launched
        pending.add(task)
        launched += 1
        if use_fallback:
            fallback_tasks.add(task)
            trace.tried = True
        if launched > 1:
            logger.warning(
                "%s: %s（%d/%d 本目、開始から %.1f 秒）",
                name,
                reason,
                launched,
                policy.max_attempts,
                loop.time() - started,
            )
        next_launch_at = loop.time() + policy.hedge_delay

    try:
        launch("")
        while True:
            if not pending:
                # 投げた試行が全て失敗している。待つ相手がいないので、待たずに（429 は間隔を空けて）次を投げる
                if launched >= policy.max_attempts:
                    break
                if (
                    policy.early_fallback
                    and fallback is not None
                    and not trace.tried
                    and not rate_limited
                    and primary_retryable_failure
                ):
                    launch(
                        f"主モデルが一時的な失敗で終わったため、再試行の代わりに {fallback_name} を投げます",
                        use_fallback=True,
                    )
                else:
                    await asyncio.sleep(
                        _bounded(policy.rate_limit_delay * rate_limited if rate_limited else policy.retry_delay)
                    )
                    launch("失敗したため再試行します")
            primary_retryable_failure = False

            can_launch = launched < policy.max_attempts
            wait_for = max(0.0, next_launch_at - loop.time()) if (can_launch and hedging) else None
            if policy.deadline is not None:
                wait_for = _bounded(wait_for if wait_for is not None else policy.deadline)
            done, _ = await asyncio.wait(pending, timeout=wait_for, return_when=asyncio.FIRST_COMPLETED)
            if not done and _expired():
                raise TimeoutError(f"{name}: 最初の応答が {policy.deadline:g} 秒以内に届きませんでした")

            failed: list[BaseException] = []
            for task in sorted(done, key=launch_order.__getitem__):
                pending.discard(task)
                is_fallback = task in fallback_tasks
                if task.cancelled():
                    # 呼び出し元の取り消しなら、この await 自体が CancelledError になる。ここに来るのは試行の自己都合
                    if not is_fallback:
                        trace.primary_failed = True
                        failed.append(AttemptCancelled(f"{name}: 試行が取り消されました"))
                    continue
                exc = task.exception()
                if exc is not None and is_fallback:
                    logger.warning(
                        "%s: 並行して投げた %s の試行が失敗しました（%s）", name, fallback_name, type(exc).__name__
                    )
                    continue  # 主モデルの試行の扱い（429 の待ち・即時失敗・最後の失敗）に影響させない
                if exc is not None and classify_failure(exc) is Failure.RETRY:
                    trace.primary_failed = True  # 同じ回にフォールバックが先に採られても、この失敗は残す
                    primary_retryable_failure = True
                if exc is None:
                    if is_fallback:
                        trace.won = True
                    if launched > 1:
                        logger.info(
                            "%s: %.1f 秒で成功（投げた試行 %d 本%s）",
                            name,
                            loop.time() - started,
                            launched,
                            f"。{fallback_name} が先に応答" if is_fallback else "",
                        )
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
                if fallback is not None and not trace.tried:
                    launch(
                        f"{policy.hedge_delay:g} 秒応答が無いため、追加の試行の代わりに {fallback_name} を並行して投げます",
                        use_fallback=True,
                    )
                else:
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
    make_stream: Callable[[], AsyncIterator[T]],
    policy: HedgePolicy,
    *,
    name: str = "LLM",
    fallback: Callable[[], AsyncIterator[T]] | None = None,
    fallback_name: str = "フォールバック",
    trace: FallbackTrace | None = None,
) -> AsyncIterator[T]:
    """make_stream() が返すストリームを policy に従って投げ、最初のチャンクが届いた試行のチャンクを流す。

    ヘッジ・再試行・429 の扱いは hedged_call と同じで、「結果」を「最初のチャンク」に読み替える。
    最初のチャンクの前に全ての試行が失敗したときは、その失敗を送出する。最初のチャンクの後の失敗は、
    そのまま送出する（別の試行に切り替えると、先に送った本文と食い違うため）。
    採らなかった試行のストリームは閉じる。呼び出し側が途中でやめたとき（aclose・取り消し）も閉じる。
    fallback・trace は hedged_call と同じ（最初のヘッジで fallback のストリームを並行して開き、先に最初の
    チャンクが届いた方を採る。fallback を採ったら trace.won が立つ）。
    """

    def first_chunk_of(factory: Callable[[], AsyncIterator[T]], label: str):
        async def attempt() -> tuple[T, AsyncIterator[T]]:
            stream = aiter(factory())
            try:
                first = await anext(stream)
            except StopAsyncIteration:
                await _aclose(stream)
                raise EmptyStream(f"{label}: 応答が空でした") from None
            except BaseException:
                await _aclose(stream)
                raise
            return first, stream

        return attempt

    attempt = first_chunk_of(make_stream, name)

    async def discard(result: tuple[T, AsyncIterator[T]]) -> None:
        await _aclose(result[1])

    first, stream = await hedged_call(
        attempt,
        policy,
        name=name,
        discard=discard,
        fallback=first_chunk_of(fallback, fallback_name) if fallback is not None else None,
        fallback_name=fallback_name,
        trace=trace,
    )
    try:
        yield first
        async for item in stream:
            yield item
    finally:
        await _aclose(stream)


class PrimaryBreaker:
    """主モデルを一時的に飛ばす、プロセス内の簡易ブレーカー（GEMINI_FALLBACK_HOLD_SECONDS）。

    落ちている主モデルを毎回待たない（ヘッジ・再試行を使い切るまで約 10 秒かかる）ための状態。
    - 閉（既定）: 主モデルから試す。
    - 開: 主モデルが一時的な失敗で尽き、フォールバックが成功したときから hold_seconds の間。
      主モデルを飛ばして、フォールバックから始める。
    - 半開: 開の満了後。最初の 1 件だけ主モデルを試し（その間の他の呼び出しはフォールバックから始める）、
      成功すれば閉、まだ失敗なら開に戻る。
    hold_seconds が 0 以下なら何もしない（常に閉）。状態はプロセスごとで、再起動・インスタンス間では共有しない。
    メソッドはどれも await を含まないので、asyncio のなかではロック無しで整合する。
    """

    def __init__(
        self,
        hold_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        name: str = "LLM",
    ) -> None:
        self._hold = hold_seconds
        self._clock = clock
        self._name = name
        self._open_until: float | None = None
        self._probing = False

    def route(self) -> str:
        """この呼び出しの始め方。"primary"（通常）/ "probe"（主モデルを試す 1 件）/ "fallback"（飛ばす）。"""
        if self._hold <= 0 or self._open_until is None:
            return "primary"
        if self._clock() < self._open_until or self._probing:
            return "fallback"
        self._probing = True
        logger.info("%s: 主モデルを 1 件だけ試して、戻ったかを確かめます", self._name)
        return "probe"

    def primary_ok(self) -> None:
        """主モデルが応答した。開（半開）なら閉じる。"""
        self._probing = False
        if self._open_until is not None:
            self._open_until = None
            logger.info("%s: 主モデルが戻りました。通常の経路に戻します", self._name)

    def opened(self) -> None:
        """主モデルが一時的な失敗で尽き、フォールバックが成功した。主モデルを hold_seconds 飛ばす。"""
        self._probing = False
        if self._hold <= 0:
            return
        again = self._open_until is not None
        self._open_until = self._clock() + self._hold
        logger.warning(
            "%s: 主モデルが%s落ちています。%g 秒間は主モデルを飛ばして、フォールバックから始めます",
            self._name,
            "まだ" if again else "",
            self._hold,
        )

    def fallback_failed(self) -> None:
        """フォールバックが失敗した。主モデルを飛ばし続けない（通常の経路に戻す）。"""
        self._probing = False
        if self._open_until is not None:
            self._open_until = None
            logger.warning("%s: フォールバックも失敗したため、通常の経路に戻します", self._name)

    def end_probe(self) -> None:
        """主モデルを試す 1 件が（取り消し・設定の失敗などで）結論なしに終わった。次の呼び出しが試す。"""
        self._probing = False


def _start_route(prefer_fallback: bool, breaker: PrimaryBreaker | None) -> str:
    if prefer_fallback:
        return "fallback"
    return breaker.route() if breaker is not None else "primary"


def _fallback_won_race(
    name: str,
    fallback_name: str,
    trace: FallbackTrace,
    breaker: PrimaryBreaker | None,
    on_fallback: Callable[[], None] | None,
) -> None:
    """並行して投げたフォールバックが先に応答したときの記録。

    主モデルの試行が実際に失敗していたときだけ、ブレーカーを開ける。遅いだけ（エラーが出ていない）なら、
    主モデルはまだ生きているかもしれないので、数十秒も高いモデルに固定しない（毎回、主モデルから試す）。
    同じ質問の中の以降の呼び出しがフォールバックから始まる記録（on_fallback）は、どちらでも残す。
    """
    if trace.primary_failed:
        if breaker is not None:
            breaker.opened()
    else:
        logger.info(
            "%s: %s が先に応答しました。主モデルにエラーは出ていない（遅いだけ）ので、主モデルは飛ばしません",
            name,
            fallback_name,
        )
    if on_fallback is not None:
        on_fallback()


async def call_with_fallback(
    primary: Callable[[], Awaitable[T]],
    fallback: Callable[[], Awaitable[T]],
    *,
    name: str = "LLM",
    fallback_name: str = "フォールバック",
    prefer_fallback: bool = False,
    breaker: PrimaryBreaker | None = None,
    on_fallback: Callable[[], None] | None = None,
    trace: FallbackTrace | None = None,
) -> T:
    """primary() が一時的な失敗で終わったときだけ、fallback() を 1 回呼ぶ。

    primary は、ヘッジ・再試行を使い切った呼び出し（hedged_call など）を渡す。切り替えるのは、
    投げ直せば通りうる失敗（`Failure.RETRY`）だけ。429（レート制限）と、認証・不正なリクエスト・モデルが
    使えない（404）といった失敗は、別のモデルでも解決しないか、叩き続けないために、そのまま送出する。
    fallback も失敗したときは、fallback の失敗ではなく primary の失敗を送出する（呼び出し側のエラー変換が
    これまでと変わらないように。fallback の失敗の種類だけログに残す）。キャンセルは握りつぶさない。

    prefer_fallback（同じ質問の中で、先の呼び出しがすでにフォールバックに切り替わった）か、breaker が開いて
    いるときは、fallback() から始める。それが失敗したら、primary() を 1 回の通常の経路で試す（fallback は
    もう試さない）。fallback が回答を返したとき on_fallback() を呼ぶ（呼び出し側が「同じ質問の中では
    フォールバックを使う」と覚えるため）。

    trace は、primary の中（hedged_call）でフォールバックを並行して投げたかを受け取る。並行して投げた
    フォールバックが先に応答したとき（trace.won）は、fallback() を呼ばずにその結果を、フォールバックの
    回答として扱う。並行して投げて失敗していたとき（trace.tried）は、fallback() をもう一度は試さない。
    """
    route = _start_route(prefer_fallback, breaker)
    try:
        if route == "fallback":
            try:
                result = await fallback()
            except Exception as exc:
                logger.warning(
                    "%s: %s が失敗しました（%s）。主モデルを試します", name, fallback_name, type(exc).__name__
                )
                if breaker is not None:
                    breaker.fallback_failed()
                if trace is not None:
                    trace.tried = True  # 主モデルの中で、同じフォールバックをもう一度並行して投げない
                return await primary()
            if on_fallback is not None:
                on_fallback()
            return result

        try:
            result = await primary()
        except Exception as exc:
            if classify_failure(exc) is not Failure.RETRY:
                raise
            failure = exc
        else:
            if trace is not None and trace.won:
                # 並行して投げたフォールバックが、主モデルより先に応答した
                _fallback_won_race(name, fallback_name, trace, breaker, on_fallback)
            elif breaker is not None:
                breaker.primary_ok()
            return result
        if trace is not None and trace.tried:
            logger.warning(
                "%s: 並行して投げた %s も失敗していたため、もう一度は試さず元の失敗を返します", name, fallback_name
            )
            raise failure
        logger.warning(
            "%s: 一時的な失敗が続いたため、%s で 1 回だけ試します（失敗の種類: %s）",
            name,
            fallback_name,
            type(failure).__name__,
        )
        try:
            result = await fallback()
        except Exception as exc:
            logger.warning("%s: %s も失敗しました（%s）。元の失敗を返します", name, fallback_name, type(exc).__name__)
        else:
            logger.info("%s: %s で成功しました", name, fallback_name)
            if breaker is not None:
                breaker.opened()
            if on_fallback is not None:
                on_fallback()
            return result
        raise failure
    finally:
        if route == "probe" and breaker is not None:
            breaker.end_probe()


async def _stream_items(factory: Callable[[], AsyncIterator[T]], label: str) -> AsyncIterator[T]:
    """factory() のストリームを流す。チャンクを 1 つも返さずに終わったら EmptyStream（一時的な失敗）。"""
    got = False
    async with aclosing(factory()) as stream:
        async for item in stream:
            got = True
            yield item
    if not got:
        raise EmptyStream(f"{label}: 応答が空でした")


async def stream_with_fallback(
    primary: Callable[[], AsyncIterator[T]],
    fallback: Callable[[], AsyncIterator[T]],
    *,
    name: str = "LLM",
    fallback_name: str = "フォールバック",
    prefer_fallback: bool = False,
    breaker: PrimaryBreaker | None = None,
    on_fallback: Callable[[], None] | None = None,
    trace: FallbackTrace | None = None,
) -> AsyncIterator[T]:
    """primary() のストリームを流し、最初のチャンクの前に一時的な失敗で終わったときだけ fallback() に切り替える。

    切り替えの判定は call_with_fallback と同じ。最初のチャンクを送った後の失敗は、そのまま送出する
    （別のモデルに切り替えると、先に送った本文と食い違うため）。チャンクを 1 つも返さずに終わったストリームは、
    一時的な失敗（EmptyStream）として扱う。fallback が最初のチャンクの前に失敗したら primary の失敗を送出する。
    呼び出し側が途中でやめたとき（aclose・取り消し）は、流している側のストリームを閉じる。

    prefer_fallback・breaker・on_fallback の扱いも call_with_fallback と同じ（fallback から始めて、最初の
    チャンクの前に失敗したら primary へ）。主モデルの最初のチャンクが届いたら、その時点で breaker を閉じる
    （呼び出し側が途中でやめても、主モデルが戻ったことは分かるため）。trace の扱いも call_with_fallback と
    同じ（primary の中で並行して投げたフォールバックが先に最初のチャンクを返したら、フォールバックの回答
    として扱う。並行して投げて失敗していたなら、もう一度は試さない）。
    """
    route = _start_route(prefer_fallback, breaker)
    sent = False
    try:
        if route == "fallback":
            try:
                async with aclosing(_stream_items(fallback, fallback_name)) as items:
                    async for item in items:
                        if not sent and on_fallback is not None:
                            on_fallback()  # 最初のチャンクで記録する（呼び出し側が途中でやめても残る）
                        sent = True
                        yield item
            except Exception as exc:
                if sent:
                    raise  # 送り始めた後の失敗は、別の試行に置き換えられない
                logger.warning(
                    "%s: %s が失敗しました（%s）。主モデルを試します", name, fallback_name, type(exc).__name__
                )
                if breaker is not None:
                    breaker.fallback_failed()
                if trace is not None:
                    trace.tried = True  # 主モデルの中で、同じフォールバックをもう一度並行して投げない
            else:
                return
            async with aclosing(_stream_items(primary, name)) as items:
                async for item in items:
                    yield item
            return

        try:
            async with aclosing(_stream_items(primary, name)) as items:
                async for item in items:
                    if not sent:
                        if trace is not None and trace.won:
                            # 並行して投げたフォールバックが、主モデルより先に最初のチャンクを返した
                            _fallback_won_race(name, fallback_name, trace, breaker, on_fallback)
                        elif breaker is not None:
                            breaker.primary_ok()
                    sent = True
                    yield item
        except Exception as exc:
            if sent or classify_failure(exc) is not Failure.RETRY:
                raise
            failure = exc
        else:
            return
        if trace is not None and trace.tried:
            logger.warning(
                "%s: 並行して投げた %s も失敗していたため、もう一度は試さず元の失敗を返します", name, fallback_name
            )
            raise failure
        logger.warning(
            "%s: 一時的な失敗が続いたため、%s で 1 回だけ試します（失敗の種類: %s）",
            name,
            fallback_name,
            type(failure).__name__,
        )
        try:
            async with aclosing(_stream_items(fallback, fallback_name)) as items:
                async for item in items:
                    if not sent:
                        # fallback の最初のチャンクで記録する（呼び出し側が途中でやめても残る）
                        if breaker is not None:
                            breaker.opened()
                        if on_fallback is not None:
                            on_fallback()
                    sent = True
                    yield item
        except Exception as exc:
            if sent:
                raise
            logger.warning("%s: %s も失敗しました（%s）。元の失敗を返します", name, fallback_name, type(exc).__name__)
        else:
            return
        raise failure
    finally:
        if route == "probe" and breaker is not None:
            breaker.end_probe()


def paid_api_key() -> str:
    """GEMINI_API_KEY_PAID。空（既定）なら、有料キーへの切り替えは使わない。値は、ログにも例外にも入れない。"""
    key = os.environ.get("GEMINI_API_KEY_PAID", "").strip()
    return "" if key in {"", "your_api_key_here"} else key


def key_switch_hold_seconds() -> float:
    """GEMINI_KEY_SWITCH_HOLD_SECONDS（既定 60）。有料キーへ切り替えたあと、最初から有料キーを使う秒数。

    0 で無効（毎回、無料キーから試す）。不正値・負数は既定値に戻す。
    """
    try:
        value = float(os.environ.get("GEMINI_KEY_SWITCH_HOLD_SECONDS", ""))
    except ValueError:
        return DEFAULT_KEY_SWITCH_HOLD_SECONDS
    return value if math.isfinite(value) and value >= 0 else DEFAULT_KEY_SWITCH_HOLD_SECONDS


class KeySwitch:
    """無料キーが 429（レート制限）で尽きたとき、有料キーへ一時的に切り替える、プロセス内の状態（GEMINI_KEY_SWITCH_HOLD_SECONDS）。

    - 通常: 無料キーから試す。
    - 切り替え後: 有料キーが成功したときから hold_seconds の間は、最初から有料キーで投げる（毎回、無料キーで
      429 を待ってから有料へ回る遅れをなくすため）。満了後は、また無料キーから試す。
    hold_seconds が 0 以下なら保持しない（毎回、無料キーから試す）。状態はプロセスごとで、インスタンス間では共有しない。
    使った回数（無料・有料）を数える。キーの値は持たない。メソッドは await を含まないので、ロック無しで整合する。
    """

    def __init__(
        self,
        hold_seconds: float,
        *,
        clock: Callable[[], float] = time.monotonic,
        name: str = "LLM",
    ) -> None:
        self._hold = hold_seconds
        self._clock = clock
        self._name = name
        self._until: float | None = None
        self.free_used = 0
        self.paid_used = 0

    def use_paid(self) -> bool:
        return self._until is not None and self._clock() < self._until

    def free_ok(self) -> None:
        self.free_used += 1

    def paid_ok(self) -> None:
        """有料キーが成功した。保持を延ばす（連続して 429 のあいだは、有料に居続ける）。"""
        self.paid_used += 1
        again = self.use_paid()
        if self._hold > 0:
            self._until = self._clock() + self._hold
        logger.warning(
            "%s: 有料キーで応答しました（無料 %d 回・有料 %d 回）。%s",
            self._name,
            self.free_used,
            self.paid_used,
            f"{self._hold:g} 秒間は最初から有料キーを使います" if self._hold > 0 and not again else "",
        )

    def clear(self) -> None:
        """有料キーが失敗した。保持をやめて、無料キーから試す。"""
        self._until = None


async def call_with_key_switch(
    free: Callable[[], Awaitable[T]],
    paid: Callable[[], Awaitable[T]],
    *,
    switch: KeySwitch,
    name: str = "LLM",
) -> T:
    """free() が 429 で終わったときだけ、paid() を 1 回呼ぶ。

    free は、ヘッジ・再試行（429 の間隔を空けた再試行を含む）を使い切った呼び出しを渡す。切り替えるのは 429 だけで、
    認証・不正なリクエスト（400/401/403）・一時的な失敗（5xx・タイムアウト）は、そのまま送出する（別のキーでは
    解決しない、または別の仕組み＝モデルのフォールバックの担当）。paid も失敗したときは、paid の失敗ではなく
    free の 429 を送出する（呼び出し側のエラー変換がこれまでと変わらないように。paid の失敗の種類だけログに残す）。
    switch が保持中（直前に有料キーが成功した）なら paid() から始める。それが失敗したら保持をやめて free() を試す。
    """
    if switch.use_paid():
        try:
            return await paid()
        except Exception as exc:
            logger.warning("%s: 有料キーが失敗しました（%s）。無料キーを試します", name, type(exc).__name__)
            switch.clear()
    try:
        result = await free()
    except Exception as exc:
        if classify_failure(exc) is not Failure.RATE_LIMITED:
            raise
        failure = exc
    else:
        switch.free_ok()
        return result
    logger.warning("%s: 無料キーが 429（レート制限）で尽きたため、有料キーで 1 回だけ試します", name)
    try:
        result = await paid()
    except Exception as exc:
        logger.warning("%s: 有料キーも失敗しました（%s）。元の 429 を返します", name, type(exc).__name__)
        raise failure from None
    switch.paid_ok()
    return result


async def stream_with_key_switch(
    free: Callable[[], AsyncIterator[T]],
    paid: Callable[[], AsyncIterator[T]],
    *,
    switch: KeySwitch,
    name: str = "LLM",
) -> AsyncIterator[T]:
    """free() のストリームを流し、最初のチャンクの前に 429 で終わったときだけ paid() に切り替える。

    判定は call_with_key_switch と同じ。最初のチャンクを送った後の失敗は、そのまま送出する（別のキーで
    やり直すと、先に送った本文と食い違うため）。チャンクを 1 つも返さずに終わったストリームは、一時的な失敗
    （EmptyStream）として扱い、切り替えない。
    """
    sent = False
    if switch.use_paid():
        try:
            async with aclosing(_stream_items(paid, name)) as items:
                async for item in items:
                    if not sent:
                        switch.paid_ok()
                    sent = True
                    yield item
            return
        except Exception as exc:
            if sent:
                raise
            logger.warning("%s: 有料キーが失敗しました（%s）。無料キーを試します", name, type(exc).__name__)
            switch.clear()
    try:
        async with aclosing(_stream_items(free, name)) as items:
            async for item in items:
                if not sent:
                    switch.free_ok()
                sent = True
                yield item
        return
    except Exception as exc:
        if sent or classify_failure(exc) is not Failure.RATE_LIMITED:
            raise
        failure = exc
    logger.warning("%s: 無料キーが 429（レート制限）で尽きたため、有料キーで 1 回だけ試します", name)
    try:
        async with aclosing(_stream_items(paid, name)) as items:
            async for item in items:
                if not sent:
                    switch.paid_ok()
                sent = True
                yield item
    except Exception as exc:
        if sent:
            raise
        logger.warning("%s: 有料キーも失敗しました（%s）。元の 429 を返します", name, type(exc).__name__)
        raise failure from None
