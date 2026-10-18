"""条・項の構造から、関係（辺）を規則だけで取り出す。LLM は使わない。

辺の種類（向きは「src が dst を前提にする」）:
    exception_of  「前項の定めにかかわらず」「第N条の規定にかかわらず」がある項 → 例外の対象の項
    references    「前項」「前条」「第N条（第M項）」「次のとおり」の参照、脚注の番号。src の項 → 参照先の項（または脚注）
    same_chapter  条 → それが属する章（条同士の全組み合わせは持たない。所属から導ける）

条の名前だけの参照（前条・第N条）は、参照先の第 1 項を指す。参照先が無い（前項が無い、存在しない条）ときは辺を作らない。
各辺に、出典のページ・根拠の本文・extracted_by・updated_at を付ける。
"""

import re
from dataclasses import dataclass
from datetime import UTC, datetime

from app.relations.articles import Article, Paragraph, Regulation

# 例外: 〜にかかわらず（かかわらず・拘らず）
_TARGET = r"(?P<prev>前項|前条)|第(?P<art>\d+)条(?:第(?P<par>\d+)項)?"
_EXCEPTION = re.compile(rf"(?:{_TARGET})の(?:定め|規定)?にかかわらず")
_REFERENCE = re.compile(_TARGET)
_FORWARD = re.compile(r"次のとおり|次の各号|次に掲げる")


@dataclass(frozen=True)
class Edge:
    type: str
    src: str
    dst: str
    evidence: str  # 根拠の本文（出典の項の本文に含まれる。same_chapter は章の見出し）
    page: int  # 出典（src）のページ
    extracted_by: str
    updated_at: str


def _resolve(reg: Regulation, para: Paragraph, m: re.Match[str]) -> str | None:
    """参照先の項の id。見つからなければ None。"""
    articles = {a.number: a for a in reg.articles}
    if m.group("prev") == "前項":
        return f"a{para.article}p{para.number - 1}" if para.number > 1 else None
    if m.group("prev") == "前条":
        target = articles.get(para.article - 1)
        return target.paragraphs[0].id if target else None
    target = articles.get(int(m.group("art")))
    if target is None or target.number == para.article and m.group("par") is None:
        return None
    number = int(m.group("par")) if m.group("par") else 1
    wanted = f"a{target.number}p{number}"
    return wanted if any(p.id == wanted for p in target.paragraphs) else None


def _paragraph_edges(reg: Regulation, article: Article, para: Paragraph, stamp: str) -> list[Edge]:
    def edge(type_: str, dst: str, evidence: str) -> Edge:
        return Edge(type_, para.id, dst, evidence, para.page, "rule", stamp)

    edges: list[Edge] = []
    taken: list[tuple[int, int]] = []
    for m in _EXCEPTION.finditer(para.text):
        dst = _resolve(reg, para, m)
        taken.append(m.span())
        if dst:
            edges.append(edge("exception_of", dst, m.group(0)))
    for m in _REFERENCE.finditer(para.text):
        if any(s <= m.start() < e for s, e in taken):
            continue
        dst = _resolve(reg, para, m)
        if dst:
            edges.append(edge("references", dst, m.group(0)))
    m = _FORWARD.search(para.text)
    if m:
        for later in article.paragraphs:
            if later.number > para.number:
                edges.append(edge("references", later.id, m.group(0)))
    if para.footnote is not None:
        edges.append(edge("references", f"fn{para.footnote}", para.raw[-20:]))
    return edges


def extract_edges(reg: Regulation, *, now: datetime | None = None) -> list[Edge]:
    stamp = (now or datetime.now(UTC)).isoformat(timespec="seconds")
    chapters = {c.number: c for c in reg.chapters}
    edges: list[Edge] = []
    for article in reg.articles:
        chapter = chapters.get(article.chapter)
        if chapter is not None:
            edges.append(Edge("same_chapter", article.id, chapter.id, chapter.heading, article.page, "rule", stamp))
        for para in article.paragraphs:
            edges.extend(_paragraph_edges(reg, article, para, stamp))
    seen: set[tuple[str, str, str]] = set()
    unique: list[Edge] = []
    for e in edges:
        key = (e.type, e.src, e.dst)
        if key not in seen:
            seen.add(key)
            unique.append(e)
    return unique
