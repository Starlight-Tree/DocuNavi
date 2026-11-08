"""停止の合図（SIGTERM）の後に、進行中の応答を打ち切るための合図（ロードマップ ⑥）。

hypercorn は、SIGTERM の後、新しい接続を閉じたところで「すべての接続が閉じる」のを待つ（Python 3.12 以降の
`Server.wait_closed()`）。待ちには上限が無く、`--graceful-timeout` も効かない。生成中の `/chat` `/chat/stream` が
つながったままだと、いつまでも終わらない（Cloud Run は約 10 秒で SIGKILL する）。
そこで `app/serve.py` が、SIGTERM から少し待ったあとにこの合図を出し、進行中の応答が自分で終わる（LLM 呼び出しを取り消し、接続を閉じる）ようにする。
合図の置き場は `app.state.drain`。無いとき（テスト・`uvicorn` 起動）は、何も変わらない。
"""

import asyncio


class Drain:
    """一度だけ立つ合図。イベントループの中で作る。"""

    def __init__(self) -> None:
        self._event = asyncio.Event()

    def set(self) -> None:
        self._event.set()

    def is_set(self) -> bool:
        return self._event.is_set()

    async def wait(self) -> None:
        await self._event.wait()
