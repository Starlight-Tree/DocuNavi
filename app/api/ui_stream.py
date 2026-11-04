"""POST /chat/stream の応答（AI SDK の UI message stream）の組み立て。

形式: `text/event-stream`。1 チャンクは `data: <JSON>` の行と空行で、最後に `data: [DONE]`。
ヘッダー `x-vercel-ai-ui-message-stream: v1` を付けると、AI SDK の useChat（@ai-sdk/react）が
そのまま読める。ここは純粋な変換だけで、LLM もリクエストも知らない。

1 回の応答のチャンクの並び（正常時）:

    start（messageId、messageMetadata = {thread_id, mode}）
    [本文の前と途中に] data-status（transient。data = {phase: analyzing | searching | generating}。履歴に入らない。通常の質問は searching → generating。analyzing は、追質問の検索語を補うときと、従来の agent の形の 1 回目）
    text-start → text-delta … → text-end      （LLM の応答ごと。ツール呼び出しの前置きがあれば複数）
    [最終回答が直前の部分と違うときだけ] text-start → text-delta → text-end
    message-metadata（messageMetadata = {context, sources, collection, target, hit_count}。sources は context の出典と同じ順の
        {name, page, page_end, weak, collection, target}。collection はこの会話の旧グループの範囲、target はこの会話の対象（どちらも null は絞らない）、hit_count は今ターンの
        検索で拾ったチャンクの数（検索していなければ null で、0 件とは別））
    finish
    [DONE]

「メッセージの本文は、最後のテキスト部分」と決めてある。入力ガードの拒否や、出力ガードによる
定型文への置き換えのとき、サーバーは最後に正しい本文を新しいテキスト部分として送る。
先に届いた部分（ガードが止めるまでの前半など）は、クライアントが無視してよい。

失敗のときは、`data-failure`（transient。data = {status, retryAfterSeconds?}）のあとに `error`（errorText）を送る。
`error` チャンクは errorText しか運べないので、HTTP で返していたときのステータスと Retry-After は、この `data-failure` で伝える。
"""

import json
from typing import Any

from app.agents.graph import (
    EVENT_STATUS,
    EVENT_TEXT,
    EVENT_TURN_END,
    PHASE_ANALYZING,
    PHASE_GENERATING,
    PHASE_SEARCHING,
)

UI_STREAM_HEADERS = {
    "x-vercel-ai-ui-message-stream": "v1",
    "cache-control": "no-cache, no-transform",
    # nginx 系のプロキシに、応答を溜めずにそのまま流させる
    "x-accel-buffering": "no",
}
DONE = b"data: [DONE]\n\n"
PHASES = frozenset({PHASE_ANALYZING, PHASE_SEARCHING, PHASE_GENERATING})
# SSE のコメント行。クライアントは無視する。送ることで、無通信の間もプロキシに切られず、切断も検知できる
KEEPALIVE = b": ping\n\n"


def encode(chunk: dict[str, Any]) -> bytes:
    """チャンクを SSE の 1 イベントにする。JSON は改行を含まないので、1 行の data: になる。"""
    payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n".encode()


class UIMessageStream:
    """グラフのカスタムストリームと状態の流れを、UI message stream のチャンクへ変換する。"""

    def __init__(self, message_id: str, thread_id: str, mode: str) -> None:
        self._message_id = message_id
        self._thread_id = thread_id
        self._mode = mode
        self._open_id: str | None = None  # 開いているテキスト部分の id
        self._parts = 0
        self._last_part_text = ""  # 直近のテキスト部分として送った本文
        self._state: dict[str, Any] = {}  # 直近の状態（stream_mode="values"）

    def start(self) -> list[dict[str, Any]]:
        return [
            {
                "type": "start",
                "messageId": self._message_id,
                "messageMetadata": {"thread_id": self._thread_id, "mode": self._mode},
            }
        ]

    def _open(self) -> list[dict[str, Any]]:
        self._open_id = f"text-{self._parts}"
        self._parts += 1
        self._last_part_text = ""
        return [{"type": "text-start", "id": self._open_id}]

    def _close(self) -> list[dict[str, Any]]:
        if self._open_id is None:
            return []
        chunk = {"type": "text-end", "id": self._open_id}
        self._open_id = None
        return [chunk]

    def feed(self, item: tuple[str, Any]) -> list[dict[str, Any]]:
        mode, data = item
        if mode == "values" and isinstance(data, dict):
            self._state = data
            return []
        if mode != "custom" or not isinstance(data, dict):
            return []
        if data.get("type") == EVENT_TURN_END:
            return self._close()
        if data.get("type") == EVENT_STATUS and data.get("phase") in PHASES:
            return [{"type": "data-status", "data": {"phase": data["phase"]}, "transient": True}]
        if data.get("type") == EVENT_TEXT and (text := data.get("text")):
            chunks = [] if self._open_id is not None else self._open()
            self._last_part_text += text
            chunks.append({"type": "text-delta", "id": self._open_id, "delta": text})
            return chunks
        return []

    def _final_text(self) -> str:
        messages = self._state.get("messages") or []
        return str(messages[-1].text) if messages else ""

    def finish(self) -> list[dict[str, Any]]:
        """正常終了。最終回答が、最後に送った本文と違うなら、それを新しいテキスト部分として足す。"""
        chunks = self._close()
        final = self._final_text()
        if final and final != self._last_part_text:
            chunks += self._open()
            chunks.append({"type": "text-delta", "id": self._open_id, "delta": final})
            chunks += self._close()
        chunks.append(
            {
                "type": "message-metadata",
                "messageMetadata": {
                    "context": str(self._state.get("context") or ""),
                    "sources": list(self._state.get("sources") or []),
                    "collection": self._state.get("collection"),
                    "target": self._state.get("target"),
                    "hit_count": self._state.get("hit_count"),
                },
            }
        )
        chunks.append({"type": "finish"})
        return chunks

    def error(self, text: str, status: int | None = None, retry_after: int | None = None) -> list[dict[str, Any]]:
        """レスポンスを始めた後の失敗。HTTP ステータスは変えられないので、error チャンクで伝える。

        `status`（/chat と同じ 429・504 など）と `retry_after`（秒）は、error チャンクに載せられないので、
        直前の transient なデータ部分（data-failure）で伝える。"""
        chunks = self._close()
        info: dict[str, Any] = {}
        if status is not None:
            info["status"] = status
        if retry_after is not None:
            info["retryAfterSeconds"] = retry_after
        if info:
            chunks.append({"type": "data-failure", "data": info, "transient": True})
        chunks.append({"type": "error", "errorText": text})
        return chunks
