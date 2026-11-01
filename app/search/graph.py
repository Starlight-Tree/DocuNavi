"""チャンクの参照関係（GraphRAG の最小版）。DB にも API にも依存しない純粋な計算。

「第N条」と書いたチャンクから、同じ資料の中で「第N条」の見出しで始まる行を持つチャンクへ、辺を張る（向きは参照する側 → 参照される側）。
条の参照だけを使い、LLM も PDF の再解析も使わない（`app/relations/` は入寮規程の PDF 専用で、PDF の原本から都度計算するため、
検索の経路では使わない）。ヒットしたチャンクが他の条を参照していたら、その条のチャンクを追加の文脈にする（`app/search_pipeline.py`）。
「前条」「前項」のような相対の参照と、語の定義への辺（`app/relations/terms.py`）は張らない。
"""

import re
from collections import defaultdict
from collections.abc import Sequence

from app.search.keyword import normalize

_DEFINES = re.compile(r"^\s*第(\d+)条", re.MULTILINE)
_REFERS = re.compile(r"第(\d+)条")


def build_edges(sources: Sequence[str], texts: Sequence[str]) -> dict[int, list[int]]:
    """位置 → 参照先の位置（昇順）。同じ `sources`（資料）の中だけで結ぶ。自分自身には張らない。"""
    defined: dict[tuple[str, int], list[int]] = defaultdict(list)
    normalized = [normalize(text) for text in texts]
    for position, (source, text) in enumerate(zip(sources, normalized, strict=True)):
        for m in _DEFINES.finditer(text):
            defined[(source, int(m.group(1)))].append(position)
    edges: dict[int, list[int]] = {}
    for position, (source, text) in enumerate(zip(sources, normalized, strict=True)):
        own = {int(m.group(1)) for m in _DEFINES.finditer(text)}
        targets: set[int] = set()
        for m in _REFERS.finditer(text):
            number = int(m.group(1))
            if number in own:
                continue
            targets.update(p for p in defined.get((source, number), ()) if p != position)
        if targets:
            edges[position] = sorted(targets)
    return edges
