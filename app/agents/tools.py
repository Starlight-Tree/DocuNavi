import math
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool

# リポジトリ直下の storage/chunk/
CHUNK_DIR = Path(__file__).resolve().parents[2] / "storage" / "chunk"
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


def bigrams(text: str) -> set[str]:
    # 日本語は空白区切りでないため、空白を除いた文字 2-gram で照合する
    compact = "".join(text.split())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[i : i + 2] for i in range(len(compact) - 1)}


# ひらがなを区切りとみなして、漢字・カタカナ・英数字の連なり（語の核）を取り出す
_WORD_RUN = re.compile(r"[^\W_\u3041-\u309f]+")


def _terms(text: str) -> list[str]:
    """検索用の 2-gram 列（重複あり）。

    bigrams() と違い、ひらがな・句読点・空白をまたがない。ひらがなは助詞や活用語尾が大半で、
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


def search_chunks(
    query: str, top_k: int = TOP_K, corpus: dict[str, str] | None = None
) -> list[tuple[str, str]]:
    """クエリと関連度の高い箇所を (ファイル名, 本文) で最大 top_k ファイル分返す。

    ファイルを文単位に分けて BM25 で採点し、一致した文とその前後を元の順序で本文として返す。
    資料全体に頻出する語は採点から外し、クエリと共有する 2-gram が少ない文は関連なしとして
    落とすので、無関係な質問では空になる。

    corpus は (ファイル名 -> 本文) の資料。None なら storage/chunk/ を使う（アプリはこちら）。
    評価（scripts/eval_retrieval.py）が、別の資料で同じ検索を試すための差し替え口。
    """
    chunks = load_chunks() if corpus is None else corpus
    all_query_terms = set(_terms(query))
    if not chunks or not all_query_terms or top_k <= 0:
        return []

    sentences_by_file = {name: _split_passages(text) for name, text in chunks.items()}
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
        picked = sorted(
            (item for item in scored if item[1] == name and item[0] >= cutoff),
            key=lambda item: -item[0],
        )[:MAX_PASSAGES_PER_FILE]
        if picked:
            # 採用した文とその前後を、資料中の出現順に並べて 1 つの本文にする
            sentences = sentences_by_file[name]
            positions = {
                pos
                for _, _, position, _ in picked
                for pos in range(position - CONTEXT_WINDOW, position + CONTEXT_WINDOW + 1)
                if 0 <= pos < len(sentences)
            }
            results.append((name, "\n".join(sentences[pos] for pos in sorted(positions))))
    return results


@tool
def search_zahard_lore(query: str) -> str:
    """架空の惑星「ザーハード」の設定資料を検索する。

    惑星の環境・大気・探査団・歴史など、ザーハードに関する質問に答える前に必ず使う。
    query には質問の要点となるキーワードや短い文を渡す。
    """
    hits = search_chunks(query)
    if not hits:
        return "該当する設定資料は見つかりませんでした。別のキーワードで再検索してください。"
    return "\n\n".join(f"[{name}]\n{text}" for name, text in hits)


TOOLS = [search_zahard_lore]
