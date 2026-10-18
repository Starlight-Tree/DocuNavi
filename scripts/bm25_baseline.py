"""評価用の基準線（ベースライン）: ローカルの PDF / .txt を読み、BM25 で検索する。アプリは使わない。

アプリの検索は、DB の `chunks` へのベクトル検索だけ（`app/search_vector.py`）。ここにあるのは、以前アプリが
`app/agents/tools.py` で使っていた BM25 の実装で、評価（`scripts/eval_retrieval.py`・`eval_vector.py`・`eval_search_compare.py`）が
「BM25 の経路」を測るために残してある。コーパスが増えて、ハイブリッド検索やリランカーを測り直すときの比較の基準にもなる
（`docs/search-design.md`）。アプリの挙動には関与しない。

検索する資料群は `SEARCH_SCOPE`（評価が使うときは、環境変数に左右されないよう全資料群を渡す）。
"""

import logging
import math
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from functools import lru_cache
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STORAGE_DIR = Path(__file__).resolve().parents[1] / "storage"
# リポジトリ直下の storage/chunk/（.txt の資料。資料群 TEXT_COLLECTION）
CHUNK_DIR = _STORAGE_DIR / "chunk"
# storage/pdf/<資料群の名前>/ に PDF を置く。ディレクトリ名がそのまま資料群の名前になる
PDF_DIR = _STORAGE_DIR / "pdf"
TEXT_COLLECTION = "zahard"
# 検索する資料群（名前をカンマ区切り。all か未設定なら全部）
SEARCH_SCOPE_ENV = "SEARCH_SCOPE"
TOP_K = 2

# 1 ファイルから採用する「一致した文」の上限。全文を返すと無関係な文まで LLM に渡るため絞る
MAX_PASSAGES_PER_FILE = 3
# 採用した文の前後何文までを一緒に返すか。「これ」「このため」などの指示語や、
# 同じ話題の隣の箇条書きが欠けて回答が不完全になるのを防ぐ
CONTEXT_WINDOW = 1
# 最上位の文のスコアに対する比率。これ未満の文は「ついでに一致しただけ」として落とす
RELATIVE_SCORE_CUTOFF = 0.4
# 全文の何割以上に現れる 2-gram を「主題語」として検索から外すか。「ザーハード」のような
# 資料全体の主題語は、長いほど 2-gram の数が多く、肝心の語（「大気」など）より点を稼いでしまう
GENERIC_TERM_RATIO = 0.25
# 文とクエリで共有すべき 2-gram の数。1 語だけの偶然の一致（「地球」「生命」など）を
# 該当なしにするため 2 以上を要求する。クエリ自体がそれより短ければクエリ全体の一致を要求する
MIN_SHARED_TERMS = 2

# BM25 のパラメータ（一般的な既定値）
_BM25_K1 = 1.2
_BM25_B = 0.75


@lru_cache(maxsize=1)
def load_chunks() -> dict[str, str]:
    """storage/chunk/ 内の .txt を全て読み込む（ファイル名 -> 本文）。"""
    if not CHUNK_DIR.is_dir():
        return {}
    return {
        path.name: path.read_text(encoding="utf-8")
        for path in sorted(CHUNK_DIR.glob("*.txt"))
    }


@lru_cache(maxsize=1)
def _load_pdf_collections() -> dict[str, dict[str, list[Any]]]:
    """storage/pdf/ の直下のディレクトリごとに、PDF を検索の単位にして読む（資料群の名前 -> ファイル名 -> 単位の列）。

    取り込めない PDF は、その PDF だけを外して WARNING に残す（残りの資料で検索は動く）。
    """
    from app.ingestion.corpus import load_pdf_corpus  # PDF を使わないとき（pypdf）を読み込まない

    if not PDF_DIR.is_dir():
        return {}
    collections: dict[str, dict[str, list[Any]]] = {}
    for directory in sorted(p for p in PDF_DIR.iterdir() if p.is_dir()):
        corpus, _, failures = load_pdf_corpus(directory)
        for failure in failures:
            logger.warning("PDF を検索の資料に入れられません: %s/%s", directory.name, failure)
        if corpus:
            collections[directory.name] = corpus
    return collections


def load_collections() -> dict[str, dict[str, Any]]:
    """検索できる資料群（名前 -> (ファイル名 -> 本文))。.txt の資料群 TEXT_COLLECTION が先、PDF は名前順。

    資料群ごとに、頻出語の判定や BM25 の統計を別々に持つ。混ぜて 1 つにすると、別の資料群が増えただけで
    「ザーハード」のような主題語の出現率が下がり、除外されなくなる（GENERIC_TERM_RATIO）。
    PDF のディレクトリ名が TEXT_COLLECTION と同じなら、その資料群に PDF も入る。
    """
    collections: dict[str, dict[str, Any]] = {}
    texts = load_chunks()
    if texts:
        collections[TEXT_COLLECTION] = dict(texts)
    for name, corpus in _load_pdf_collections().items():
        collections.setdefault(name, {}).update(corpus)
    return collections


def search_scope() -> list[str]:
    """環境変数 SEARCH_SCOPE で選ばれた資料群の名前。未設定か all なら全部。知らない名前は ValueError。"""
    available = list(load_collections())
    raw = os.environ.get(SEARCH_SCOPE_ENV, "").strip()
    if raw.lower() in ("", "all"):
        return available
    wanted = [name.strip() for name in raw.split(",") if name.strip()]
    unknown = [name for name in wanted if name not in available]
    if unknown or not wanted:
        raise ValueError(
            f"{SEARCH_SCOPE_ENV} の資料群が見つかりません: {', '.join(unknown) or raw}"
            f"（選べるのは all と {', '.join(available) or '（資料なし）'}）"
        )
    return wanted


# ひらがなを区切りとみなして、漢字・カタカナ・英数字の連なり（語の核）を取り出す
_WORD_RUN = re.compile(r"[^\W_\u3041-\u309f]+")


def _terms(text: str) -> list[str]:
    """検索用の 2-gram 列（重複あり）。

    ひらがな・句読点・空白をまたがない。ひらがなは助詞や活用語尾が大半で、
    どの文にも現れるため、無関係な質問でも偶然一致して「ヒットした」ように見せてしまう。
    活用語尾が違う（「暴走する」と「暴走し」）だけで一致しなくなるのも防げる。
    """
    terms: list[str] = []
    for run in _WORD_RUN.findall(unicodedata.normalize("NFKC", text).lower()):
        terms.extend(run[i : i + 2] for i in range(len(run) - 1))
    return terms


def _split_passages(text: str) -> list[str]:
    # 資料は文・箇条書きを「\」（文字としての円記号）か改行で区切っている
    return [part.strip() for part in re.split(r"[\\\n]+", text) if part.strip()]


def _units_of(value: str | Sequence[str | Sequence[str]]) -> tuple[list[str], list[tuple[int, int]]]:
    """資料 1 件を、採点する単位（文）の列と、各文が属する「まとまり」の範囲（先頭, 末尾の位置）にする。

    文字列なら、「\\」と改行で文に分ける（どの文も、自分 1 つのまとまり）。
    列なら、文字列の要素は 1 つの文。文字列の列の要素（例: 表の行）は、1 つのまとまり: 行ごとに採点するが、
    どれかが採用されたら、まとまり全体を返す（表を行に割って返さないため）。
    """
    if isinstance(value, str):
        passages = _split_passages(value)
        return passages, [(i, i) for i in range(len(passages))]
    passages: list[str] = []
    spans: list[tuple[int, int]] = []
    for item in value:
        members = [item] if isinstance(item, str) else list(item)
        members = [m.strip() for m in members if m and m.strip()]
        if not members:
            continue
        start = len(passages)
        passages.extend(members)
        spans.extend([(start, start + len(members) - 1)] * len(members))
    return passages, spans


def search_chunks(
    query: str, top_k: int = TOP_K, corpus: Mapping[str, str | Sequence[str | Sequence[str]]] | None = None
) -> list[tuple[str, str]]:
    """クエリと関連度の高い箇所を (ファイル名, 本文) で最大 top_k ファイル分返す。

    corpus が None なら、storage/ の資料群（SEARCH_SCOPE で選んだもの）を、資料群ごとに別々に採点して合わせる
    （search_collections）。corpus が (ファイル名 -> 本文) なら、その 1 つの資料群だけを採点する。評価
    （scripts/eval_retrieval.py）が、別の資料で同じ検索を試すための差し替え口。
    """
    if corpus is not None:
        return _search_collection(query, top_k, corpus)
    collections = load_collections()
    return search_collections(query, top_k, {name: collections[name] for name in search_scope()})


def search_collections(
    query: str, top_k: int, collections: Mapping[str, Mapping[str, Any]]
) -> list[tuple[str, str]]:
    """資料群ごとに採点し、順位で交互に取って、最大 top_k ファイル分を返す。

    BM25 のスコアは資料群の統計（頻出語・平均の長さ）に依存し、資料群をまたいで比べられない。そのため
    スコアでは並べず、各資料群の 1 位、2 位…を、資料群の並び順で交互に採る。1 つの資料群しか当たらない
    質問では、その資料群を単独で検索した結果と同じになる。複数の資料群が当たる質問は、どの資料群からも採る。
    """
    return _interleave([_search_collection(query, top_k, corpus) for corpus in collections.values()], top_k)


def _interleave(per_collection: Sequence[Sequence[tuple[str, str]]], top_k: int) -> list[tuple[str, str]]:
    """各資料群の結果の 1 位、2 位…を、資料群の並び順で交互に採って、最大 top_k 件にする。"""
    merged: list[tuple[str, str]] = []
    for rank in range(max(top_k, 0)):
        for hits in per_collection:
            if rank < len(hits):
                merged.append(hits[rank])
                if len(merged) >= top_k:
                    return merged
    return merged


def _search_collection(
    query: str, top_k: int, chunks: Mapping[str, str | Sequence[str | Sequence[str]]]
) -> list[tuple[str, str]]:
    """1 つの資料群（ファイル名 -> 本文）を採点して、最大 top_k ファイル分を (ファイル名, 本文) で返す。

    ファイルを文単位に分けて BM25 で採点し、一致した文とその前後を元の順序で本文として返す。
    資料全体に頻出する語は採点から外し、クエリと共有する 2-gram が少ない文は関連なしとして
    落とすので、無関係な質問では空になる。

    本文は、文字列（「\\」と改行で文に分ける）か、文の列。列の中の「文字列の列」（例: 表の行）は 1 つの
    まとまりで、行ごとに採点し、採用されたらまとまり全体を返す（表のチャンクを、行に割らずに返すため）。
    """
    all_query_terms = set(_terms(query))
    if not chunks or not all_query_terms or top_k <= 0:
        return []

    units = {name: _units_of(text) for name, text in chunks.items()}
    sentences_by_file = {name: passages for name, (passages, _) in units.items()}
    # (ファイル名, 文の位置, 文, 文の 2-gram 列)
    passages: list[tuple[str, int, str, list[str]]] = [
        (name, position, sentence, _terms(sentence))
        for name, sentences in sentences_by_file.items()
        for position, sentence in enumerate(sentences)
    ]
    if not passages:
        return []

    total = len(passages)
    doc_freq: dict[str, int] = {}
    for *_, terms in passages:
        for term in set(terms):
            doc_freq[term] = doc_freq.get(term, 0) + 1
    avg_len = sum(len(terms) for *_, terms in passages) / total or 1.0

    # 採点に使う語。資料に 1 度も出ない語は何にも一致しないので除き、主題語も外す。
    # 全部が主題語（「ザーハードとは」「エネルギー源の正体は」など）なら、外さずそのまま使う
    known_terms = {term for term in all_query_terms if doc_freq.get(term, 0)}
    if not known_terms:
        return []
    query_terms = {
        term for term in known_terms if doc_freq[term] / total < GENERIC_TERM_RATIO
    } or known_terms

    def bm25(terms: list[str]) -> float:
        score = 0.0
        for term in query_terms:
            freq = terms.count(term)
            if not freq:
                continue
            df = doc_freq[term]
            idf = math.log(1 + (total - df + 0.5) / (df + 0.5))
            norm = freq + _BM25_K1 * (1 - _BM25_B + _BM25_B * len(terms) / avg_len)
            score += idf * freq * (_BM25_K1 + 1) / norm
        return score

    # 関連ありの判定は、主題語を含む全ての語で数える。採点では主題語を外すが、判定まで外すと
    # 「エネルギー源の正体は」のように主題語（エネルギー源）＋資料にない語（正体）の質問が
    # 該当なしになってしまう。資料にない語は分母に残るので、1 語だけの偶然の一致は落とせる
    required = min(MIN_SHARED_TERMS, len(all_query_terms))
    scored = [
        (bm25(terms), name, position, sentence)
        for name, position, sentence, terms in passages
        if len(all_query_terms & set(terms)) >= required
    ]
    if not scored:
        return []

    # ファイルの順位は、そのファイルで最も高い文のスコアで決める
    best_by_file: dict[str, float] = {}
    for score, name, _, _ in scored:
        best_by_file[name] = max(best_by_file.get(name, 0.0), score)
    top_files = sorted(best_by_file, key=lambda n: (-best_by_file[n], n))[:top_k]

    cutoff = max(best_by_file.values()) * RELATIVE_SCORE_CUTOFF
    results: list[tuple[str, str]] = []
    for name in top_files:
        spans = units[name][1]
        picked = []
        seen: set[tuple[int, int]] = set()
        for item in sorted(
            (item for item in scored if item[1] == name and item[0] >= cutoff),
            key=lambda item: -item[0],
        ):
            if spans[item[2]] in seen:
                continue  # 同じまとまり（表）の別の行。まとまりは 1 つと数える
            seen.add(spans[item[2]])
            picked.append(item)
            if len(picked) >= MAX_PASSAGES_PER_FILE:
                break
        if picked:
            # 採用した文（まとまりなら全体）とその前後を、資料中の出現順に並べて 1 つの本文にする
            sentences = sentences_by_file[name]
            positions = {
                pos
                for _, _, position, _ in picked
                for pos in range(spans[position][0] - CONTEXT_WINDOW, spans[position][1] + CONTEXT_WINDOW + 1)
                if 0 <= pos < len(sentences)
            }
            results.append((name, "\n".join(sentences[pos] for pos in sorted(positions))))
    return results
