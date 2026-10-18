"""POST /chat/stream の応答（AI SDK の UI message stream）の組み立て。

形式: `text/event-stream`。1 チャンクは `data: <JSON>` の行と空行で、最後に `data: [DONE]`。
ヘッダー `x-vercel-ai-ui-message-stream: v1` を付けると、AI SDK の useChat（@ai-sdk/react）が
そのまま読める。ここは純粋な変換だけで、LLM もリクエストも知らない。

1 回の応答のチャンクの並び（正常時）:

    start（messageId、messageMetadata = {thread_id, mode}）
    text-start → text-delta … → text-end      （LLM の応答ごと。ツール呼び出しの前置きがあれば複数）
    [最終回答が直前の部分と違うときだけ] text-start → text-delta → text-end
    message-metadata（messageMetadata = {context, sources}。sources は context の出典と同じ順の {name, page, page_end}）
    finish
    [DONE]

「メッセージの本文は、最後のテキスト部分」と決めてある。入力ガードの拒否や、出力ガードによる
定型文への置き換えのとき、サーバーは最後に正しい本文を新しいテキスト部分として送る。
先に届いた部分（ガードが止めるまでの前半など）は、クライアントが無視してよい。
"""

import json
from typing import Any

from app.agents.graph import EVENT_TEXT, EVENT_TURN_END

UI_STREAM_HEADERS = {
    "x-vercel-ai-ui-message-stream": "v1",
    "cache-control": "no-cache, no-transform",
    # nginx 系のプロキシに、応答を溜めずにそのまま流させる
    "x-accel-buffering": "no",
}
DONE = b"data: [DONE]\n\n"
# SSE のコメント行。クライアントは無視する。送ることで、無通信の間もプロキシに切られず、切断も検知できる
KEEPALIVE = b": ping\n\n"


def encode(chunk: dict[str, Any]) -> bytes:
    """チャンクを SSE の 1 イベントにする。JSON は改行を含まないので、1 行の data: になる。"""
    payload = json.dumps(chunk, ensure_ascii=False, separators=(",", ":"))
    return f"data: {payload}\n\n".encode()


def is_text_event(item: tuple[str, Any]) -> bool:
    """graph.astream(stream_mode=["custom", "values"]) の 1 件が、利用者へ送る本文の断片か。"""
    mode, data = item
    return mode == "custom" and isinstance(data, dict) and data.get("type") == EVENT_TEXT


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
                },
            }
        )
        chunks.append({"type": "finish"})
        return chunks

    def error(self, text: str) -> list[dict[str, Any]]:
        """本文を送り始めた後の失敗。HTTP ステータスは変えられないので、error チャンクで伝える。"""
        return [*self._close(), {"type": "error", "errorText": text}]
