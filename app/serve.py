"""本番の起動（`python -m app.serve`。Dockerfile の CMD）。hypercorn を同じプロセスで動かす。

`hypercorn app.main:app` との違いは、SIGTERM（Cloud Run のスケールダウン・入れ替え）の扱いだけ:
SIGTERM を受けたら、新しい接続を受けるのをやめ、進行中の応答に `DRAIN_GRACE_SECONDS` だけ猶予を与え、
その後に `app/drain.py` の合図を出して打ち切らせる。Cloud Run が SIGKILL する約 10 秒より前に終わるため、猶予は 10 秒未満にする。
待ち受けは `0.0.0.0:$PORT`（既定 8000）。HTTP/1.1 と h2c（HTTP/2 の平文）の両方を受ける点は、hypercorn の既定のまま。
"""

import asyncio
import os
import signal

from hypercorn.asyncio import serve
from hypercorn.config import Config

from app.drain import Drain
from app.main import app

DRAIN_GRACE_SECONDS = 6.0


async def run(config: Config, grace: float = DRAIN_GRACE_SECONDS) -> None:
    loop = asyncio.get_running_loop()
    drain = Drain()
    app.state.drain = drain
    stop = asyncio.Event()
    timers: list[asyncio.TimerHandle] = []

    def on_signal() -> None:
        if stop.is_set():
            return
        stop.set()
        timers.append(loop.call_later(grace, drain.set))

    for name in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(name, on_signal)
    try:
        await serve(app, config, shutdown_trigger=stop.wait)
    finally:
        for timer in timers:
            timer.cancel()
        app.state.drain = None


def main() -> None:
    config = Config()
    config.bind = [f"0.0.0.0:{os.environ.get('PORT', '8000')}"]
    asyncio.run(run(config))


if __name__ == "__main__":
    main()
