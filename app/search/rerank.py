"""リランカー: 1 次検索（ベクトル・キーワード）の候補を、質問との語の一致で並べ直す。DB にも API にも依存しない純粋な計算。

いまの実装は語彙の一致だけ（`lexical`）で、外部のモデルも API も使わない。質問の語のうち候補に含まれるものの重み（IDF）の割合と、
質問の型番・版番号・コードが候補の本文にそのまま出るかを見る。クロスエンコーダーや Gemini による並べ替えは、費用・依存・遅延が
増えるので入れていない。差し替えるときは `lexical_scores` と同じ形（候補の位置の列 → 0〜1 の点）の関数にする。
"""

from collections.abc import Sequence

from app.search.keyword import KeywordIndex, identifiers, normalize, tokenize

# 型番などが質問にあるとき、一致の割合に置く重み（残りは語の一致の割合）
IDENTIFIER_WEIGHT = 0.5


def lexical_scores(query: str, positions: Sequence[int], index: KeywordIndex, texts: Sequence[str]) -> list[float]:
    """候補ごとの 0〜1 の点。`positions` は `index` の中の位置、`texts` はその本文（同じ並び）。"""
    terms = set(tokenize(query))
    wanted = identifiers(query)
    total = sum(index.idf(t) for t in terms)
    scores: list[float] = []
    for position, text in zip(positions, texts, strict=True):
        coverage = (sum(index.idf(t) for t in terms if index.has(position, t)) / total) if total > 0 else 0.0
        if wanted:
            body = normalize(text)
            matched = sum(1 for w in wanted if w in body) / len(wanted)
            scores.append((1 - IDENTIFIER_WEIGHT) * coverage + IDENTIFIER_WEIGHT * matched)
        else:
            scores.append(coverage)
    return scores
