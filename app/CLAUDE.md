# app/（バックエンド）

全体の方針・禁止事項は、ルートの [CLAUDE.md](../CLAUDE.md)。ここには `app/` を触るときの規約だけを置く。設計の詳細は `docs/search-design.md` `docs/api.md`。

## 検索

- 入口は `search/pipeline.py`（`search/vector.py` の `search` に、ハイブリッド・リランカー・GraphRAG を環境変数で重ねる）。ほかの経路から `vector.search` を直接呼ぶ追加はしない（MCP は運用者専用の例外）。
  フラグの表と既定（`SEARCH_HYBRID` on、`SEARCH_RERANK` on、`SEARCH_GRAPH` on。2026-11-01 から）は、`search/pipeline.py` の先頭の docstring が正本。
- **3 つのフラグがすべて off のときは、`search_vector.search` をそのまま呼ぶ**（結果も経路も以前と同じ）。この性質を壊さない。
- `search` は `scope`（`app/scope.py` の `Scope`）が必須で、渡し忘れは `TypeError`。`Scope.ALL` は呼び出し側が明示する。範囲は SQL で絞り、返った行も確かめる。
- 失敗しても別の経路には戻らない。DB・設定・キーの失敗は `VectorSearchError`（API は 503 の固定文）。不正なフラグ値も黙って既定に戻さない。
- 結果は `search_vector.Hit`。`origin`（`vector` / `keyword` / `vector+keyword` / `graph`）と各点をログ（INFO、1 回の検索に 1 行）に残す。質問の本文はログに載せない。
- 点の計算に使う定数（`RRF_K` `RERANK_RRF_SHARE`、識別子の重み、`MIN_SIMILARITY`）は、実 API の比較の前に固定した値。評価なしに変えない（`scripts/eval_pipeline.py` の `plan` も同じ値を前提にする）。
- 検索の実装は `app/search/`（`pipeline` `vector` `keyword` `rerank` `graph` `types`）。移動・分割は `.claude/skills/search-refactor/SKILL.md` に従う（承認が要る）。
- ロガー名は `app.search_pipeline` と `app.search_vector` のまま（文書とテストが使う。`__name__` に戻さない）。

## API（`app/api/`）

- 例外の文字列やサーバーのパスを応答に含めない。LLM・検索の失敗は固定メッセージにする。
- 画面を配信しない。`/{path}` のような全捕捉ルートを足さない。
- 切断は失敗ではない（499、INFO ログ）。詳細は `.claude/rules/app-api.md` の「切断は失敗ではない」。
- 履歴のキーと利用者 ID は、認証済みの `sub` からだけ作る（`app/auth.py`）。

## 型・データ

- `app/` に `models/` は無い。検索の結果は `Hit`（NamedTuple。`search/types.py`）、資料の範囲は `Scope`、LangGraph の状態は `app/agents/state.py` の `AgentState`。
- DB の表を変えるのは `workers/api/migrations/`（D1）と `db/migrations/`（Postgres。`db/migrate.py` で適用）だけ。アプリのコードから DDL を流さない。
- `DB_BACKEND`（既定 `d1`）で DB を切り替える。アプリの SQL は SQLite の書き方のままで、`app/pg.py` の `translate()` が Postgres に直す。新しい SQL を足したら、`tests/test_history_pg_backend.py` に実行を足す（`PG_TEST_URL` が要る。CI は pgvector 入りの Postgres を使う）。

## テスト

- `tests/conftest.py` がモックモードに固定し、検索も偽物に差し替える。テストで実 API・DB を呼ばない。
- 検索を差し替えるテストがある（`tests/conftest.py` は `tools.search_chunks_async` を差し替え、`tests/test_search_pipeline*.py` と `scripts/eval_pipeline.py` は `search_vector.search` などを差し替える）。名前を変えると、差し替えが空振りして本物を呼ぶ。
