"""キーワード検索（BM25）の部品。DB にも API にも依存しない純粋な計算で、ハイブリッド検索（`app/search/pipeline.py`）が使う。

日本語は分かち書きの辞書を使わず、連続する文字を 2 文字ずつ切る（2-gram）。英数字は、型番・版番号・コードを 1 語として残すため、
`AP-340S` のような連なりを丸ごとと、`-` `.` `/` `_` で分けた部品の両方を語にする。正規化は NFKC と小文字化。
評価用の基準線（`scripts/bm25_baseline.py`）とは別の実装で、アプリはあちらを import しない。
"""

import math
import re
import unicodedata
from collections import Counter
from collections.abc import Sequence

K1 = 1.5
B = 0.75

_WORD = re.compile(r"[a-z0-9]+(?:[-_./][a-z0-9]+)*|[^\x00-\x7f\W_]+")
_SEPARATORS = re.compile(r"[-_./]")


def normalize(text: str) -> str:
    return unicodedata.normalize("NFKC", text).lower()


def tokenize(text: str) -> list[str]:
    """語の列（重複あり）。日本語の連続は 2 文字ずつ（1 文字だけの連続はそのまま）、英数字の連なりは丸ごとと部品。"""
    tokens: list[str] = []
    for run in _WORD.findall(normalize(text)):
        if run.isascii():
            tokens.append(run)
            if _SEPARATORS.search(run):
                tokens.extend(part for part in _SEPARATORS.split(run) if part)
        elif len(run) == 1:
            tokens.append(run)
        else:
            tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


def identifiers(text: str) -> list[str]:
    """型番・版番号・コードのように見える連なり（正規化後）。数字を含み、かつ英字か区切りを含み、3 文字以上。

    数字だけの語（「10」「2026」）は含めない。どの文にも出るので、「この語が出る箇所」を決め手にできないため。
    """
    found: list[str] = []
    for run in _WORD.findall(normalize(text)):
        if not run.isascii() or len(run) < 3 or not any(ch.isdigit() for ch in run):
            continue
        if any(ch.isalpha() for ch in run) or _SEPARATORS.search(run):
            if run not in found:
                found.append(run)
    return found


class KeywordIndex:
    """チャンクの本文（見出しを含む）の BM25。チャンクは位置（0 始まり）で指す。"""

    def __init__(self, texts: Sequence[str]) -> None:
        self._counts = [Counter(tokenize(text)) for text in texts]
        self._lengths = [sum(c.values()) for c in self._counts]
        self._avg = (sum(self._lengths) / len(self._lengths)) if self._lengths else 0.0
        self._df: Counter[str] = Counter()
        for counts in self._counts:
            self._df.update(counts.keys())

    def __len__(self) -> int:
        return len(self._counts)

    def idf(self, token: str) -> float:
        n, df = len(self._counts), self._df.get(token, 0)
        return math.log(1 + (n - df + 0.5) / (df + 0.5))

    def has(self, position: int, token: str) -> bool:
        return token in self._counts[position]

    def score(self, query: str) -> dict[int, float]:
        """質問の語に 1 つでも当たったチャンクの BM25 スコア（0 より大きいものだけ）。"""
        terms = set(tokenize(query))
        scores: dict[int, float] = {}
        for position, counts in enumerate(self._counts):
            length = self._lengths[position]
            total = 0.0
            for term in terms:
                tf = counts.get(term)
                if not tf:
                    continue
                norm = tf + K1 * (1 - B + B * length / self._avg) if self._avg else tf + K1
                total += self.idf(term) * tf * (K1 + 1) / norm
            if total > 0:
                scores[position] = total
        return scores

    def ranked(self, query: str, limit: int) -> list[tuple[int, float]]:
        """スコアの高い順（同点は位置の小さい順）に上位 limit 件の (位置, スコア)。"""
        scores = self.score(query)
        return sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:limit]
