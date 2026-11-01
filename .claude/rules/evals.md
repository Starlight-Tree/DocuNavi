---
paths:
  - "evals/**"
---

# 評価（evals/）を触るとき

詳細は [evals/CLAUDE.md](../../evals/CLAUDE.md) と [evals/README.md](../../evals/README.md)。

- `corpora/` はアプリの検索対象（`storage/pdf/` に同じ PDF がある）。`fixtures/` は対象外で、`storage/pdf/` にも本番 DB にも入れない
- 質問の ID は評価セットをまたいで重ねない（`--app` が止まる）。新しい接頭辞は `evals/CLAUDE.md` の表に足す。`x` 始まりは使わない
- 承認済みの質問の期待値は変えない。PDF や抽出でずれたら、質問を直さず報告する
- `results/` の既存ファイルを上書きしない（`--out` で別名にする）。`cache/` はコミットしない
- 実 API（埋め込み・LLM）は、質問文・回数・概算費用を示して承認を得てから。キーは環境変数だけから読み、`.env` は開かない
