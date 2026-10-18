"""抽出した関係（章・条・項・脚注と辺）を、画面に渡せるノードと辺にまとめ、変更の影響の及ぶ先を数える。

DB にも API にも依存しない純粋な計算。辺の向きは「src が dst に依存する」なので、dst を変えると src に影響が及ぶ。
影響をたどる辺は、依存を表す `references` / `exception_of` / `uses_term` だけ（`same_chapter` は所属で、依存ではない）。
"""

import io
from collections import deque
from dataclasses import dataclass

from app.ingestion.pdf_parser import extract_pages
from app.relations.articles import Regulation, parse_regulation
from app.relations.extract import Edge, extract_edges
from app.relations.terms import TSUBASA_DORM_TERMS

DEPENDENCY_TYPES = ("references", "exception_of", "uses_term")


@dataclass(frozen=True)
class Node:
    id: str
    type: str  # chapter / article / paragraph / footnote
    label: str
    text: str | None  # 項・脚注の本文（章と条は None）
    parent: str | None  # 項 -> 条、条 -> 章（脚注と章は None）
    page: int | None


@dataclass(frozen=True)
class Impacted:
    id: str
    hops: int  # 起点からの距離（起点に隣り合う依存元が 1）


@dataclass(frozen=True)
class Impact:
    start: tuple[str, ...]  # 起点にした項・脚注（条を指定したときは、その条の全ての項）
    impacted: tuple[Impacted, ...]
    articles: tuple[str, ...]  # 影響が及ぶ条（起点の条を除く）
    edges: tuple[Edge, ...]  # たどった辺


@dataclass(frozen=True)
class Analysis:
    title: str | None
    nodes: tuple[Node, ...]
    edges: tuple[Edge, ...]


def analyze_dorm_rules(pdf: bytes) -> Analysis:
    """入寮規程の PDF のバイト列から、ノードと辺を作る。読めない・形が違うときは例外（呼び出し側が固定メッセージにする）。"""
    reg = parse_regulation(extract_pages(io.BytesIO(pdf)))
    edges = tuple(extract_edges(reg, terms=TSUBASA_DORM_TERMS))
    return Analysis(title=reg.title, nodes=_nodes(reg), edges=edges)


def _nodes(reg: Regulation) -> tuple[Node, ...]:
    nodes: list[Node] = [Node(c.id, "chapter", c.heading, None, None, None) for c in reg.chapters]
    for article in reg.articles:
        nodes.append(Node(article.id, "article", article.heading, None, f"ch{article.chapter}", article.page))
        for para in article.paragraphs:
            label = f"第{article.number}条 第{para.number}項"
            nodes.append(Node(para.id, "paragraph", label, para.text, article.id, para.page))
    nodes.extend(Node(f.id, "footnote", f"脚注 {f.number}", f.text, None, f.page) for f in reg.footnotes)
    return tuple(nodes)


class UnknownNode(Exception):
    """この規程に無いノード。"""


class NotAnImpactSource(Exception):
    """章は、影響の起点にできない（章の所属は依存ではない）。"""


def impact_of(analysis: Analysis, node: str, hops: int) -> Impact:
    """`node`（項・脚注・条）を変えたとき、`hops` 回までの依存をたどって影響が及ぶ項・脚注と条を返す。"""
    known = {n.id: n for n in analysis.nodes}
    if node not in known:
        raise UnknownNode(node)
    kind = known[node].type
    if kind == "chapter":
        raise NotAnImpactSource(node)
    if kind == "article":
        start = tuple(n.id for n in analysis.nodes if n.type == "paragraph" and n.parent == node)
    else:
        start = (node,)

    dependents: dict[str, list[Edge]] = {}
    for edge in analysis.edges:
        if edge.type in DEPENDENCY_TYPES:
            dependents.setdefault(edge.dst, []).append(edge)

    distance = {s: 0 for s in start}
    queue = deque(start)
    walked: list[Edge] = []
    while queue:
        current = queue.popleft()
        if distance[current] >= hops:
            continue
        for edge in dependents.get(current, ()):
            walked.append(edge)
            if edge.src not in distance:
                distance[edge.src] = distance[current] + 1
                queue.append(edge.src)

    impacted = tuple(Impacted(n, d) for n, d in distance.items() if n not in start)
    own_article = node if kind == "article" else known[node].parent
    reached = {known[i.id].parent for i in impacted if known[i.id].type == "paragraph"}
    articles = sorted((a for a in reached if a is not None and a != own_article), key=lambda a: int(a[1:]))
    return Impact(start=start, impacted=impacted, articles=tuple(articles), edges=tuple(walked))
