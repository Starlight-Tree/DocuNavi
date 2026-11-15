"""追質問で、直前の質問の検索結果（元資料の抜粋）を、今回の根拠セットへ足すための部品。

追質問（「夕方はどう？」など）の検索が外れても、直前の質問で取れた資料から答えられるようにする。
引用の番号は、今回の根拠セットの並び（今回の検索結果 → 足した前の結果）で付け直す。前の質問の表示番号は使わない。

- 材料は、会話履歴の ToolMessage（元資料の検索結果）だけ。AI の回答は使わない。
- 呼び出し側が、アクセス制御で伏せた後の履歴（`mask_hidden_turns`）を渡す。伏せた検索結果は `artifact` が空で、ここでは拾わない。
- 足すのは、直前の 1 質問の最後の検索の、上位の数件だけ（`MAX_CARRIED`）。`weak`（類似度の差が大きい 2 件目以降など）は足さない。
"""

import re
from collections.abc import Sequence
from typing import Any

from langchain_core.messages import BaseMessage, HumanMessage, ToolMessage

MAX_CARRIED = 3  # 足す前の結果の上限（件）。トークンを増やしすぎず、話題が離れた古い根拠を溜めないため

_HEADER = re.compile(r"^\[(\d+)\] ")


def split_numbered(content: str, count: int) -> list[str] | None:
    """`[n] 名前\\n本文` を `[n+1]`… と続ける文面を、`count` 件の本文（見出しの行を除く）に分ける。数が合わなければ None。

    先頭の番号 n から、n, n+1, … の順に見出しを探す（本文の中に `[1]` のような文字列があっても、順番の見出しだけを区切りにする）。
    """
    first = _HEADER.match(content)
    if first is None:
        return None
    start = int(first.group(1))
    marks: list[tuple[int, int]] = []  # (見出しの開始位置, 見出しの長さ)
    position = 0
    for number in range(start, start + count):
        needle = f"[{number}] "
        index = content.find(needle, position) if number == start else content.find("\n\n" + needle, position)
        if index < 0:
            return None
        begin = index if number == start else index + 2
        marks.append((begin, len(needle)))
        position = begin + len(needle)
    pieces: list[str] = []
    for i, (begin, header_length) in enumerate(marks):
        end = marks[i + 1][0] - 2 if i + 1 < len(marks) else len(content)
        body = content[begin + header_length : end]
        newline = body.find("\n")
        pieces.append(body[newline + 1 :] if newline >= 0 else "")
    return pieces


def previous_evidence(history: Sequence[BaseMessage], limit: int = MAX_CARRIED) -> list[tuple[dict[str, Any], str]]:
    """最後のユーザー発言より前の、直前の 1 質問の最後の検索の上位 `limit` 件を、(出典の項目, 本文) で返す（無ければ空）。"""
    last_human = max((i for i, m in enumerate(history) if isinstance(m, HumanMessage)), default=-1)
    if last_human <= 0:
        return []
    before = history[:last_human]
    previous_human = max((i for i, m in enumerate(before) if isinstance(m, HumanMessage)), default=-1)
    turn = before[previous_human + 1 :] if previous_human >= 0 else before
    for message in reversed(turn):
        if not isinstance(message, ToolMessage):
            continue
        artifact = message.artifact
        content = message.content
        if not artifact or not isinstance(artifact, list) or not isinstance(content, str):
            continue
        if not all(isinstance(item, dict) for item in artifact):
            continue
        texts = split_numbered(content, len(artifact))
        if texts is None:
            return []  # 形が想定と違うときは、足さない（誤った本文を根拠にしない）
        pairs = [(item, text) for item, text in zip(artifact, texts, strict=True) if not item.get("weak")]
        return pairs[:limit]
    return []


def merge_carried(
    content: str, current: Sequence[dict[str, Any]], carried: Sequence[tuple[dict[str, Any], str]]
) -> tuple[str, list[dict[str, Any]]]:
    """今回の検索結果の文面 `content`（出典が `current`）に、足す前の結果を、続きの番号で付ける。

    返すのは、(足した後の文面, 足した出典の項目)。今回の検索結果と同じ資料・本文のものは足さない（`current` の項目は、本文 `_text` を持つ検索直後のもの）。
    """
    seen = {(item.get("name"), item.get("_text")) for item in current}
    fresh = [(item, text) for item, text in carried if (item.get("name"), text) not in seen]
    if not fresh:
        return content, []
    start = len(current) + 1
    extra = "\n\n".join(f"[{number}] {item['name']}\n{text}" for number, (item, text) in enumerate(fresh, start=start))
    return f"{content}\n\n{extra}", [item for item, _ in fresh]
