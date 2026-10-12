import math
from functools import lru_cache
from pathlib import Path

from langchain_core.tools import tool

# リポジトリ直下の storage/chunk/
CHUNK_DIR = Path(__file__).resolve().parents[2] / "storage" / "chunk"
TOP_K = 2


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


def search_chunks(query: str, top_k: int = TOP_K) -> list[tuple[str, str]]:
    """クエリと関連度の高いチャンクを (ファイル名, 本文) で最大 top_k 件返す。

    各チャンクに含まれる 2-gram のうちクエリと一致するものを IDF 重みで合計する。
    """
    chunks = load_chunks()
    query_grams = bigrams(query)
    if not chunks or not query_grams:
        return []

    chunk_grams = {name: bigrams(text) for name, text in chunks.items()}
    total = len(chunks)

    def idf(gram: str) -> float:
        df = sum(gram in grams for grams in chunk_grams.values())
        return math.log(1 + total / df) if df else 0.0

    scored = [
        (sum(idf(g) for g in query_grams & grams), name)
        for name, grams in chunk_grams.items()
    ]
    scored = [item for item in scored if item[0] > 0]
    scored.sort(reverse=True)
    return [(name, chunks[name]) for _, name in scored[:top_k]]


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
