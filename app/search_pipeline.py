"""資料の検索の入口。ベクトル検索（`app/search_vector.py`）に、ハイブリッド・リランカー・GraphRAG を、環境変数で重ねる。

| 環境変数 | 既定 | 中身 |
|---|---|---|
| `SEARCH_HYBRID` | on | キーワード（BM25。`app/search/keyword.py`）の順位をベクトルの順位と RRF で合わせる |
| `SEARCH_RERANK` | on | 候補を語の一致で並べ直す（`app/search/rerank.py`。外部のモデルも API も使わない） |
| `SEARCH_GRAPH` | off | 上位のチャンクが「第N条」と参照する先のチャンクを、追加の文脈として加える（`app/search/graph.py`）。2026-11-01 の実測では辺が 0 本で効果を測れなかったため、既定は off |
| `SEARCH_CANDIDATES` | 20 | 1 次検索の候補の数（ベクトル・キーワードそれぞれ。`top_k` より小さくはならない） |
| `SEARCH_GRAPH_MAX_EXTRA` | 1 | `graph` で加えるチャンクの上限（0 で加えない） |
| `SEARCH_CORPUS_TTL_SECONDS` | 60 | キーワード側が読む資料の写し（範囲ごと）を持つ秒数 |

値は on / off（1 / 0、true / false、yes / no も可）。**3 つとも off なら、`search_vector.search` をそのまま呼ぶ**（これまでと同じ経路・同じ結果）。
不正な値は `VectorSearchError`（API は 503 の固定文）にする。黙って既定に戻さない。

- **ベクトル検索は常に走る。** キーワードで拾うのは「ベクトル側の候補の並べ替え」と、型番・版番号・コード（数字を含む英数字の連なり。
  `search_keyword.identifiers`）が質問にあって本文にそのまま出るチャンクだけ。それ以外の語だけの一致は、ベクトルの下限（`MIN_SIMILARITY`）を
  越えていないので返さない（「資料にない質問」を LLM が断る前提を崩さないため）。
- **失敗しても別の経路には戻らない。** キーワード側の DB の読み取りが失敗したら、ベクトルだけの結果にはせず、`VectorSearchError` を投げる。
  原因を絞るときは、フラグを off にする。
- 資料の範囲（`Scope`）は、ベクトル側と同じ SQL の条件で絞り、返ってきた行ももう一度確かめる。
- **ログ（INFO、1 回の検索に 1 行）:** 質問の本文は載せず、先頭 8 文字のハッシュと字数だけ。フラグ、候補の数、返したチャンクごとに
  資料・ページ・経路（`vector` / `keyword` / `vector+keyword` / `graph`）・ベクトルの類似度・BM25・RRF・語の一致の点・最終点を残す。
"""

import hashlib
import logging
import os
import time
from dataclasses import dataclass, field

from app import search_vector
from app.scope import ALLOWED_FILTER_SQL, Scope, require_scope
from app.search.graph import build_edges
from app.search.keyword import KeywordIndex, identifiers, normalize
from app.search.rerank import lexical_scores
from app.search_vector import Hit, VectorSearchError

logger = logging.getLogger(__name__)

RRF_K = 60
# 最終点 = RRF（最大を 1 にそろえる）と語の一致の点の平均
RERANK_RRF_SHARE = 0.5

_TRUE = {"on", "1", "true", "yes"}
_FALSE = {"off", "0", "false", "no"}

_CORPUS_SQL = (
    "SELECT d.source AS source, d.collection AS collection, c.heading AS heading, c.content AS content, "
    "c.page AS page, c.page_end AS page_end "
    "FROM chunks c JOIN documents d ON d.id = c.document_id "
    "WHERE d.status = 'ready' AND c.embedding IS NOT NULL "
    "AND (%(collection)s::text IS NULL OR d.collection = %(collection)s::text) "
    f"AND {ALLOWED_FILTER_SQL} "
    "ORDER BY d.source, c.chunk_index"
)


# ---- フラグ ----


def _flag(name: str, default: bool) -> bool:
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise VectorSearchError(f"{name} は on か off で指定してください")


def _number(name: str, default: float, low: float, high: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise VectorSearchError(f"{name} は数値で指定してください") from None
    if not low <= value <= high:
        raise VectorSearchError(f"{name} は {low:g} 以上 {high:g} 以下で指定してください")
    return value


@dataclass(frozen=True)
class Flags:
    hybrid: bool
    rerank: bool
    graph: bool
    candidates: int
    graph_extra: int
    corpus_ttl: float

    @classmethod
    def from_env(cls) -> "Flags":
        return cls(
            hybrid=_flag("SEARCH_HYBRID", True),
            rerank=_flag("SEARCH_RERANK", True),
            graph=_flag("SEARCH_GRAPH", False),
            candidates=int(_number("SEARCH_CANDIDATES", 20, 1, 100)),
            graph_extra=int(_number("SEARCH_GRAPH_MAX_EXTRA", 1, 0, 3)),
            corpus_ttl=_number("SEARCH_CORPUS_TTL_SECONDS", 60, 0, 3600),
        )

    @property
    def any(self) -> bool:
        return self.hybrid or self.rerank or self.graph

    def label(self) -> str:
        return f"hybrid={'on' if self.hybrid else 'off'},rerank={'on' if self.rerank else 'off'},graph={'on' if self.graph else 'off'}"


# ---- 資料の写し（キーワード・グラフ側） ----


@dataclass(frozen=True)
class Chunk:
    source: str
    text: str
    page: int | None
    page_end: int | None
    collection: str | None

    @property
    def key(self) -> tuple[str, str]:
        return (self.source, self.text)


class Corpus:
    """検索できる範囲のチャンクの写し（BM25 の索引と、参照の辺を持つ）。"""

    def __init__(self, chunks: list[Chunk]) -> None:
        self.chunks = chunks
        texts = [chunk.text for chunk in chunks]
        self.index = KeywordIndex(texts)
        self.normalized = [normalize(text) for text in texts]
        self.position = {chunk.key: i for i, chunk in enumerate(chunks)}
        self.edges = build_edges([chunk.source for chunk in chunks], texts)


_CACHE: dict[tuple[tuple[str, ...] | None, str | None], tuple[float, Corpus]] = {}
_CACHE_LIMIT = 32


async def _fetch_chunks(scope: Scope, collection: str | None) -> list[Chunk]:
    pool = search_vector.get_pool()
    if pool is None:
        raise VectorSearchError("DB の接続プールが開いていません")
    params = {"collection": collection, "allowed": scope.sql_names()}
    async with pool.connection() as conn:
        cursor = await conn.execute(_CORPUS_SQL, params)
        rows = await cursor.fetchall()
    return [
        Chunk(
            row["source"],
            search_vector._chunk_text(row.get("heading"), row["content"]),
            row.get("page"),
            row.get("page_end"),
            row.get("collection"),
        )
        for row in rows
        if scope.allows(row.get("collection"))  # SQL の条件を誤っても、範囲外のグループは持たない
    ]


async def _corpus(scope: Scope, collection: str | None, ttl: float) -> Corpus:
    names = scope.sql_names()
    key = (None if names is None else tuple(names), collection)
    now = time.monotonic()
    cached = _CACHE.get(key)
    if cached is not None and ttl > 0 and now - cached[0] < ttl:
        return cached[1]
    corpus = Corpus(await _fetch_chunks(scope, collection))
    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.pop(min(_CACHE, key=lambda k: _CACHE[k][0]))
    _CACHE[key] = (now, corpus)
    return corpus


def clear_cache() -> None:
    """資料の写しを捨てる（テストと、取り込み直しの直後に使う）。"""
    _CACHE.clear()


# ---- 検索 ----


@dataclass
class _Cand:
    chunk: Chunk
    position: int | None
    sim: float | None = None
    vec_rank: int | None = None
    bm25: float | None = None
    kw_rank: int | None = None
    rrf: float = 0.0
    lex: float | None = None
    score: float = 0.0
    graph_from: list[str] = field(default_factory=list)

    @property
    def origin(self) -> str:
        if self.vec_rank is not None and self.kw_rank is not None:
            return "vector+keyword"
        return "keyword" if self.kw_rank is not None else "vector"


def _to_hit(cand: _Cand, origin: str) -> Hit:
    chunk = cand.chunk
    return Hit(chunk.source, chunk.text, chunk.page, chunk.page_end, cand.sim, chunk.collection, origin)


def _fmt(value: float | None, digits: int = 3) -> str:
    return "-" if value is None else f"{value:.{digits}f}"


def _describe(cand: _Cand, origin: str) -> str:
    chunk = cand.chunk
    page = f"p{chunk.page}" if chunk.page else "p-"
    return (
        f"{chunk.source} {page} {origin} vec={_fmt(cand.sim)} bm25={_fmt(cand.bm25, 2)} "
        f"rrf={cand.rrf:.4f} lex={_fmt(cand.lex)} score={cand.score:.4f}"
    )


def _order_key(cand: _Cand) -> tuple[float, float, float]:
    return (-cand.score, float(cand.vec_rank if cand.vec_rank is not None else 1e9), float(cand.kw_rank or 1e9))


async def search(query: str, top_k: int, collection: str | None = None, *, scope: Scope) -> list[Hit]:
    """`search_vector.search` と同じ引数・同じ規則（scope は必須、範囲外は返さない）で、フラグに応じて検索を重ねる。"""
    flags = Flags.from_env()
    if not flags.any:
        hits = await search_vector.search(query, top_k, collection, scope=scope)
        _log_vector_only(query, top_k, hits)
        return hits
    scope = require_scope(scope)
    if top_k <= 0 or not query.strip():
        return []
    if scope.is_empty or (collection is not None and not scope.allows(collection)):
        return []
    started = time.perf_counter()
    pool_size = max(top_k, flags.candidates)

    vector_hits = await search_vector.search(query, pool_size, collection, scope=scope)
    corpus = await _corpus(scope, collection, flags.corpus_ttl)

    cands: dict[tuple[str, str], _Cand] = {}
    for rank, hit in enumerate(vector_hits, start=1):
        chunk = Chunk(hit.source, hit.text, hit.page, hit.page_end, hit.collection)
        cands[chunk.key] = _Cand(chunk, corpus.position.get(chunk.key), sim=hit.similarity, vec_rank=rank)
    vector_count = len(cands)

    keyword_count = rejected = 0
    if flags.hybrid:
        wanted = identifiers(query)
        for rank, (position, bm25) in enumerate(corpus.index.ranked(query, pool_size), start=1):
            chunk = corpus.chunks[position]
            cand = cands.get(chunk.key)
            if cand is None:
                # ベクトルの下限を越えていないチャンクは、質問の型番・版番号・コードが本文にそのまま出るときだけ拾う
                if not any(w in corpus.normalized[position] for w in wanted):
                    rejected += 1
                    continue
                cand = cands[chunk.key] = _Cand(chunk, position)
            cand.bm25, cand.kw_rank = bm25, rank
            keyword_count += 1

    ordered = list(cands.values())
    for cand in ordered:
        cand.rrf = sum(1 / (RRF_K + r) for r in (cand.vec_rank, cand.kw_rank) if r is not None)
    top_rrf = max((c.rrf for c in ordered), default=0.0)
    for cand in ordered:
        cand.score = cand.rrf
    if flags.rerank and ordered:
        with_position = [c for c in ordered if c.position is not None]
        lex = lexical_scores(
            query,
            [c.position for c in with_position if c.position is not None],
            corpus.index,
            [c.chunk.text for c in with_position],
        )
        for cand, value in zip(with_position, lex, strict=True):
            cand.lex = value
        for cand in ordered:
            cand.lex = cand.lex if cand.lex is not None else 0.0
            share = cand.rrf / top_rrf if top_rrf else 0.0
            cand.score = RERANK_RRF_SHARE * share + (1 - RERANK_RRF_SHARE) * cand.lex
    ordered.sort(key=_order_key)
    chosen = ordered[:top_k]

    extras: list[_Cand] = []
    if flags.graph and flags.graph_extra > 0:
        extras = _graph_extras(chosen, corpus, flags.graph_extra)

    hits = [_to_hit(c, c.origin) for c in chosen] + [_to_hit(c, "graph") for c in extras]
    elapsed_ms = (time.perf_counter() - started) * 1000
    logger.info(
        "検索 q=%s(%d字) top_k=%d flags=%s 候補=%d(vector=%d keyword=%d keyword除外=%d) 写し=%d件 返却=%d %.0fms hits=[%s]",
        hashlib.sha256(query.encode("utf-8")).hexdigest()[:8],
        len(query),
        top_k,
        flags.label(),
        len(ordered),
        vector_count,
        keyword_count,
        rejected,
        len(corpus.chunks),
        len(hits),
        elapsed_ms,
        "; ".join([_describe(c, c.origin) for c in chosen] + [_describe_graph(c) for c in extras]),
    )
    return hits


def _graph_extras(chosen: list[_Cand], corpus: Corpus, limit: int) -> list[_Cand]:
    """選んだチャンクが参照する先のチャンク（まだ選ばれていないもの）を、参照された数の多い順に limit 件。"""
    taken = {c.chunk.key for c in chosen}
    votes: dict[int, list[str]] = {}
    for cand in chosen:
        if cand.position is None:
            continue
        for target in corpus.edges.get(cand.position, ()):
            if corpus.chunks[target].key in taken:
                continue
            votes.setdefault(target, []).append(_label(cand.chunk))
    ranked = sorted(votes.items(), key=lambda item: (-len(item[1]), item[0]))[:limit]
    return [_Cand(corpus.chunks[position], position, graph_from=sources) for position, sources in ranked]


def _label(chunk: Chunk) -> str:
    return f"{chunk.source}#p{chunk.page}" if chunk.page else chunk.source


def _describe_graph(cand: _Cand) -> str:
    chunk = cand.chunk
    page = f"p{chunk.page}" if chunk.page else "p-"
    return f"{chunk.source} {page} graph from={'+'.join(cand.graph_from)}"


def _log_vector_only(query: str, top_k: int, hits: list[Hit]) -> None:
    described = "; ".join(
        f"{h.source} {'p' + str(h.page) if h.page else 'p-'} vector vec={_fmt(h.similarity)}" for h in hits
    )
    logger.info(
        "検索 q=%s(%d字) top_k=%d flags=hybrid=off,rerank=off,graph=off 返却=%d hits=[%s]",
        hashlib.sha256(query.encode("utf-8")).hexdigest()[:8],
        len(query),
        top_k,
        len(hits),
        described,
    )
