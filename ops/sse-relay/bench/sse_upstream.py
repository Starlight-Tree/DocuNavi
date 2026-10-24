"""測定用の上流（FastAPI）。LLM も DB も使わず、SSE を一定間隔で送るだけ。

    uvicorn sse_upstream:app --port 8110            # HTTP/1.1
    hypercorn sse_upstream:app --bind 127.0.0.1:8110  # h1 と h2c の両方

GET /stream?sid=<id>&hold=<秒>&interval=<秒>: 最初のイベントをすぐ送り、hold 秒の間 interval 秒ごとに送る。
  各イベントは `data: {"seq": n, "t": <送った時刻（epoch 秒）>}`。クライアントが受け取った時刻との差を遅延として測る。
GET /stats: 接続中の数、取り消された接続（sid と時刻）、最後まで送った数。`?reset=1` で数え直す。
本物のアプリと同じ Starlette の StreamingResponse を使うので、切断の検知と取り消しは本物と同じ仕組み。
"""

import asyncio
import time

from fastapi import FastAPI
from fastapi.responses import StreamingResponse

app = FastAPI()
_state = {"active": 0, "finished": 0, "cancelled": {}}


@app.get("/stream")
async def stream(sid: str = "x", hold: float = 20.0, interval: float = 1.0) -> StreamingResponse:
    async def gen():
        _state["active"] += 1
        try:
            end = time.monotonic() + hold
            seq = 0
            while True:
                yield f'data: {{"seq": {seq}, "t": {time.time()!r}}}\n\n'
                seq += 1
                if time.monotonic() >= end:
                    break
                await asyncio.sleep(interval)
            _state["finished"] += 1
        except asyncio.CancelledError:
            _state["cancelled"][sid] = time.time()
            raise
        finally:
            _state["active"] -= 1

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"cache-control": "no-cache, no-transform"})


@app.get("/stats")
async def stats(reset: int = 0) -> dict:
    out = {"active": _state["active"], "finished": _state["finished"], "cancelled": dict(_state["cancelled"])}
    if reset:
        _state["finished"] = 0
        _state["cancelled"].clear()
    return out
