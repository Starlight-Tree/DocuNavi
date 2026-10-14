"""hedged_stream: 最初のチャンクが届いた試行を採る（app/agents/resilience.py）。

実際の API は呼ばない。待ち時間は数十ミリ秒にして、採る試行・取り消し・ストリームを閉じること・
失敗の扱いを確かめる。
"""

import asyncio
import time
from collections.abc import AsyncIterator
from typing import Any

import pytest

from app.agents import resilience
from app.agents.resilience import EmptyStream, HedgePolicy, hedged_stream

FAST = HedgePolicy(hedge_delay=0.05, max_attempts=3, retry_delay=0.01, rate_limit_delay=0.02)


class Rejected(Exception):
    """リクエストの内容が悪い失敗（再試行しても同じ）。"""


class Unavailable(Exception):
    """503 のように、投げ直せば通りうる失敗。"""

    code = 503


class Scripted:
    """n 本目の試行の振る舞いを順番に決める偽のストリーム生成関数。

    steps は ("sleep", 秒) / ("yield", 値) / ("raise", 例外) の並び。呼び出しごとに 1 本のストリームを返し、
    開始・取り消し・閉じられた（finally まで到達した）試行の番号を記録する。
    """

    def __init__(self, *attempts: list[tuple[str, Any]]) -> None:
        self.attempts = attempts
        self.started: list[int] = []
        self.cancelled: list[int] = []
        self.closed: list[int] = []

    def __call__(self) -> AsyncIterator[Any]:
        index = len(self.started)
        self.started.append(index)
        steps = self.attempts[min(index, len(self.attempts) - 1)]
        return self._stream(index, steps)

    async def _stream(self, index: int, steps: list[tuple[str, Any]]) -> AsyncIterator[Any]:
        try:
            for kind, value in steps:
                if kind == "sleep":
                    await asyncio.sleep(value)
                elif kind == "yield":
                    yield value
                else:
                    raise value
        except asyncio.CancelledError:
            self.cancelled.append(index)
            raise
        finally:
            self.closed.append(index)


async def collect(script: Scripted, policy: HedgePolicy = FAST) -> list[Any]:
    return [item async for item in hedged_stream(script, policy)]


def run(script: Scripted, policy: HedgePolicy = FAST) -> list[Any]:
    return asyncio.run(collect(script, policy))


def test_single_fast_attempt_streams_all_chunks() -> None:
    script = Scripted([("yield", "a"), ("yield", "b"), ("yield", "c")])
    assert run(script) == ["a", "b", "c"]
    assert script.started == [0]  # ヘッジは投げない


def test_slow_first_chunk_is_hedged_and_the_fast_attempt_wins() -> None:
    script = Scripted(
        [("sleep", 5.0), ("yield", "slow")],
        [("yield", "x"), ("yield", "y")],
    )
    started = time.monotonic()
    assert run(script) == ["x", "y"]
    assert time.monotonic() - started < 2.0
    assert script.started == [0, 1]
    # 遅い試行は取り消され、ストリームは閉じられている
    assert script.cancelled == [0]
    assert 0 in script.closed


def test_committed_attempt_is_never_replaced_even_if_it_stalls_afterwards() -> None:
    # 最初のチャンクの後は、遅くても別の試行を投げない（送った本文と食い違うため）
    script = Scripted(
        [("yield", "a"), ("sleep", 0.3), ("yield", "b")],
        [("yield", "OTHER")],
    )
    assert run(script) == ["a", "b"]
    assert script.started == [0]


def test_failure_after_first_chunk_propagates_without_switching() -> None:
    script = Scripted(
        [("yield", "a"), ("raise", Unavailable())],
        [("yield", "OTHER")],
    )

    async def scenario() -> list[Any]:
        seen = []
        with pytest.raises(Unavailable):
            async for item in hedged_stream(script, FAST):
                seen.append(item)
        return seen

    assert asyncio.run(scenario()) == ["a"]
    assert script.started == [0]


def test_failure_before_first_chunk_is_retried() -> None:
    script = Scripted(
        [("raise", Unavailable())],
        [("yield", "ok")],
    )
    assert run(script) == ["ok"]
    assert script.started == [0, 1]


def test_fatal_failure_is_raised_immediately() -> None:
    script = Scripted([("raise", Rejected())], [("yield", "never")])

    async def scenario() -> None:
        with pytest.raises(Rejected):
            await collect(script)

    asyncio.run(scenario())
    assert script.started == [0]


def test_empty_stream_is_retried_as_transient() -> None:
    script = Scripted([], [("yield", "ok")])
    assert run(script) == ["ok"]
    assert script.started == [0, 1]


def test_all_attempts_empty_raises_empty_stream() -> None:
    script = Scripted([])

    async def scenario() -> None:
        with pytest.raises(EmptyStream):
            await collect(script)

    asyncio.run(scenario())
    assert len(script.started) == FAST.max_attempts


def test_losers_that_also_produce_a_first_chunk_are_closed() -> None:
    # 2 本が同じ回に最初のチャンクを返しても、採らなかった側のストリームを開いたまま残さない
    script = Scripted(
        [("sleep", 0.06), ("yield", "first"), ("sleep", 5.0)],
        [("sleep", 0.01), ("yield", "second"), ("sleep", 5.0)],
    )

    async def scenario() -> list[Any]:
        stream = hedged_stream(script, HedgePolicy(hedge_delay=0.05, max_attempts=2))
        first = await anext(stream)
        await stream.aclose()
        return [first]

    asyncio.run(scenario())
    assert sorted(script.closed) == [0, 1]


def test_consumer_stopping_early_closes_the_stream() -> None:
    script = Scripted([("yield", "a"), ("yield", "b"), ("sleep", 5.0), ("yield", "c")])

    async def scenario() -> None:
        stream = hedged_stream(script, FAST)
        assert await anext(stream) == "a"
        await stream.aclose()

    asyncio.run(scenario())
    assert script.closed == [0]


def test_cancelling_the_consumer_cancels_all_attempts() -> None:
    script = Scripted([("sleep", 30.0), ("yield", "never")])

    async def scenario() -> None:
        task = asyncio.ensure_future(collect(script))
        # 追加の試行が 2 本目まで投げられるのを待ってから取り消す
        deadline = time.monotonic() + 5
        while len(script.started) < 2:
            assert time.monotonic() < deadline
            await asyncio.sleep(0.005)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())
    assert sorted(script.cancelled) == script.started
    assert sorted(script.closed) == script.started


def test_a_loser_that_succeeded_while_being_cancelled_is_discarded() -> None:
    # 勝者を決めた直後に、別の試行が最初のチャンクを返し終えていた場合（取り消しと入れ違い）も、
    # そのストリームを閉じる。resilience._cancel_and_drain の後始末で確かめる
    async def scenario() -> list[str]:
        loop = asyncio.get_running_loop()
        succeeded = loop.create_future()
        succeeded.set_result(("first", "stream-of-the-loser"))
        failed = loop.create_future()
        failed.set_exception(RuntimeError("x"))
        pending = loop.create_future()  # まだ終わっていない（取り消される）
        discarded: list[str] = []

        async def discard(result: tuple[str, str]) -> None:
            discarded.append(result[1])

        await resilience._cancel_and_drain({succeeded, failed, pending}, discard)
        assert pending.cancelled()
        return discarded

    assert asyncio.run(scenario()) == ["stream-of-the-loser"]
