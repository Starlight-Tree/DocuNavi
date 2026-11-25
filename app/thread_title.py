"""会話の題名（最初のユーザー発言の先頭）の作り方。

一覧（`app/api/threads.py`）と、保存（`app/d1_saver.py` の `aput`）の両方が使う。保存時に題名を `checkpoints.metadata`（jsonb）の `title` へ入れておくと、
一覧は、会話ごとの最新のチェックポイント本体（msgpack。検索結果の本文を含み、1 件 十数 KB）を取らず、復号もしないで済む（2026-11-25）。
"""

import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import HumanMessage

MAX_TITLE_LENGTH = 40
UNTITLED = "（題名なし）"


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            part if isinstance(part, str) else str(part.get("text", ""))
            for part in content
            if isinstance(part, str) or isinstance(part, dict)
        )
    return ""


def stored_title(messages: Sequence[Any]) -> str | None:
    """最初のユーザー発言を、空白をまとめて MAX_TITLE_LENGTH 文字までにする。本文のあるユーザー発言が無ければ None。"""
    for message in messages:
        if isinstance(message, HumanMessage):
            text = re.sub(r"\s+", " ", _text_of(message.content)).strip()
            if text:
                return text if len(text) <= MAX_TITLE_LENGTH else text[: MAX_TITLE_LENGTH - 1] + "…"
    return None


def make_title(messages: Sequence[Any]) -> str:
    """`stored_title`。無ければ「（題名なし）」。"""
    return stored_title(messages) or UNTITLED
