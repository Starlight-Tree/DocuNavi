---
name: search-refactor
description: 検索の実装（app/search/）を、動作を変えずに移動・分割・整理するときの安全な手順。検索コードのリファクタを頼まれたときに使う。
---

# 検索リファクタの安全な手順

検索の実装は `app/search/`（`pipeline` `vector` `keyword` `rerank` `graph` `types`。2026-11-01 に `app/search_*.py` から移した）。
規約は `app/CLAUDE.md` と `.claude/rules/search.md`。

## 始める前

- 依頼に無いリファクタは、始める前に確認する。移動の範囲（どのファイルを、どこへ）を示して承認を得る。
- 「動作を変えない移動」と「実装の整理」は、別のコミット・別の承認にする。
- 実 API は呼ばない。評価の結果ファイル（`evals/results/`）を作り直さない。
- 作業ツリーが並行セッションと共有されていることがある。自分の変更だけをパス指定でステージする。

## 手順（1 ファイルずつ）

1. **基準を取る。** 変更前に `ruff check .` と `pytest` を実行して、通っていることを確かめる。
2. **参照元を洗い出す。** `grep -rn "<名前>" app tests scripts ops docs` で、import、`monkeypatch.setattr` の対象（文字列でモジュールを指すもの）、docstring・文書のパスを全部出す。
   参照元の例: `app/agents/tools.py`、`app/main.py`、`ops/mcp_search/main.py`、`scripts/eval_pipeline.py`、`tests/` の検索まわり。
   外からは `from app.search import vector as search_vector` の別名で読み、`monkeypatch` の対象名を変えない。ロガー名は `app.search_pipeline` `app.search_vector` のまま（`__name__` に戻さない）。
3. **1 ファイルだけ移す**（`git mv`）。中身は変えない。同じコミットで参照元を直す。
4. **差し替えを確かめる。** `monkeypatch` の対象が新しい名前を指していないと、テストが本物の検索を呼びに行く。`tests/conftest.py` の `fake_search` と `vector_only_search`、`scripts/eval_pipeline.py` の差し替え（`search_vector.search` と `search_pipeline._fetch_chunks`）を見る。
5. **検査する。** `ruff check .`、`ruff format --check .`、`pytest`。`SEARCH_*` がすべて off のとき `search_vector.search` をそのまま呼ぶ経路（`tests/test_search_pipeline.py`）が通ることを確かめる。
6. **コミットする。** 1 ファイル（または、DB も API も使わない葉のモジュールのまとまり）が 1 コミット（`refactor(search): …`）。本文に、動作を変えていないことと、実行した検査を書く。

## 触らないもの

- 点の計算（RRF、リランカー、識別子の重み、`MIN_SIMILARITY`）、フラグの既定、ログの形、`Hit` の項目。変える必要が出たら、そこで止めて相談する。
- `scripts/eval_pipeline.py run` は `evals/results/pipeline_compare.json` を上書きするので、リファクタの確認では実行しない（確認は `pytest`）。

## 終わりに

- docstring と文書（`docs/search-design.md` `docs/architecture.md` `CLAUDE.md` `evals/README.md`）の古いパスを探す。直す文書の差分は、先に示す。
- 移した結果を、ファイル一覧・参照元の一覧・検査の結果で報告する。確認していないこと（Docker のビルドなど）は確認済みと書かない。
