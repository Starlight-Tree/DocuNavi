---
paths:
  - "app/search/**"
---

# 検索の実装を触るとき

検索の実装は `app/search/`（`pipeline` `vector` `keyword` `rerank` `graph` `types`）。詳細は [app/CLAUDE.md](../../app/CLAUDE.md)。

- 3 つのフラグ（`SEARCH_HYBRID` `SEARCH_RERANK` `SEARCH_GRAPH`）がすべて off なら、`search_vector.search` をそのまま呼ぶ。この経路を変えない
- `scope` は必須。`Scope.ALL` は呼び出し側が明示する。範囲は SQL と、返った行の両方で確かめる
- 失敗したとき、別の経路（ベクトルだけ、メモリー、BM25 のみ）に黙って戻さない。`VectorSearchError`（API は 503 の固定文）
- 質問の本文をログに出さない（ハッシュと字数だけ）。経路（`origin`）と点は 1 回の検索に 1 行で残す
- `RRF_K`、リランカーの割合、識別子の重み、`MIN_SIMILARITY` は評価に使った値。変えるなら、先に評価の計画を示して承認を得る
- 関数名・モジュール名を変えるときは、`tests/conftest.py` `tests/test_search_pipeline*.py` `scripts/eval_pipeline.py` の差し替え（monkeypatch）の対象も同じ変更で直す
- 触れたら `ruff check .` と `pytest tests/test_search_pipeline.py` を実行する。実 API は呼ばない（呼ぶ測定は `.claude/skills/search-eval/SKILL.md`）
