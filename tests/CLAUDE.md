# tests/（バックエンドのテスト）

全体の方針・禁止事項は、ルートの [CLAUDE.md](../CLAUDE.md)。実行の手順と CI は [docs/development.md](../docs/development.md)。`app/` のテストの注意は [app/CLAUDE.md](../app/CLAUDE.md) の「テスト」。

`pytest`（`pyproject.toml`: `testpaths = ["tests"]`、`pythonpath = ["."]`）。**実 API・本物の DB・Supabase を呼ばない。** 画面側のテストは `frontend/`（Vitest・Playwright）。Django の試作のテストは `ops/django-saas/saas/tests/`（別の venv。製品の `pytest` には含まれない）。

## 置き場所（フラット。移動しない）

ファイルが多い（約 80）が、領域別のサブディレクトリーは作らない。`from tests.fakes import …` のように `tests.` から import するテストがあり、移すと import・`monkeypatch` の対象・CI が崩れる。**ファイル名の先頭で領域を分ける。**

| 領域 | ファイル名 | 例 |
|---|---|---|
| API（HTTP・SSE・切断・エラー） | `test_chat_*` `test_api_errors` `test_health` `test_http_smoke` `test_threads*` `test_documents*` `test_document_*` `test_graph_api` `test_collections*` `test_collection_scope` | `test_chat_stream.py` |
| LLM・グラフ・ガード | `test_llm_*` `test_real_api_faults`（`real_api_faults.py` の検査） `test_hedged_stream` `test_key_switch` `test_guardrails` `test_streaming_guard` `test_dangling_tool_calls` `test_checkpointer_injection` | `test_llm_fallback.py` |
| 検索 | `test_search_*` `test_embedding` `test_fake_search` | `test_search_pipeline.py` |
| 認証・アクセス制御 | `test_auth` `test_access*` | `test_access.py` |
| 履歴 | `test_history_*` | `test_history_store.py` |
| 取り込み・R2・PDF | `test_ingestion*` `test_r2_*` `test_zahard_pdf` | `test_ingestion_sync.py` |
| 運用（ops/） | `test_budget_guard*` `test_mcp_search` `test_cloudbuild*` | `test_budget_guard.py` |
| 評価の道具（scripts/・evals/） | `test_eval_*` `test_bm25_*` `test_relations` | `test_eval_graph.py` |
| 開発環境のガード・フック | `test_guard_*` `test_hook_event_log` `test_session_rules_hook` `test_check_sensitive_terms` `test_preview_script` | `test_guard_cloud_cli.py` |

- **Postgres が要るテスト**は、`TEST_DATABASE_URL` が無いとスキップされる。DB だけを使うファイルは `test_<対象>_postgres.py` と名づける。ほかにも、同じ環境変数で DB の部分だけをスキップするファイルがある。DB 込みの全体は `make test-db`（ポート 55432。他のセッションが使っていたら失敗して止まる〔止めない〕）。
- 同じ内容をメモリー版と Postgres 版で確かめるときは、ファイルを分け、冒頭の docstring で互いを指す（`test_threads.py` ↔ `test_threads_postgres.py`）。

## fixture と補助モジュール

| 種類 | 置き場所 | 命名 |
|---|---|---|
| 全テストに効く pytest fixture（`autouse`） | `conftest.py` | `fake_search`（検索を偽物に）、`vector_only_search`（検索の重ねを off に）。**勝手に増やさない** |
| 共通の pytest fixture（`client` `searching_llm`） | `conftest.py` | 名詞。`monkeypatch` で戻る形にする |
| 偽物・テストデータの部品（pytest の外から import するもの） | `tests/<話題>.py`。既存: `fakes.py`（偽の検索・LLM）、`pdf_fixtures.py` `bm25_fixtures.py`（PDF・コーパス）、`zahard_corpus.py` `rag_eval_cases.py`（評価用の資料と質問）、`real_api_faults.py` | `test_` で始めない（pytest が拾うため）。新しい部品は `<話題>_fixtures.py` か `fakes.py` に足す |
| 手動・E2E 用のサーバー | `tests/*_server.py`（`e2e_server.py` は Playwright、`real_api_server.py` は実 API の検証用） | `test_` で始めない。起動はテストの外 |
| 1 ファイルだけで使う fixture | そのテストファイルの中 | 他のファイルが要るようになったら `conftest.py` か補助モジュールへ |

既存のファイルの改名・移動はしない（上の理由）。**新しいファイルから**この表に従う。

## 守ること

- **検索を差し替える名前を変えない。** `conftest.py` は `tools.search_chunks_async` を、`test_search_pipeline*.py` は `search_vector.search` などを `monkeypatch` する。モジュール・関数を移したら、差し替えも同じ変更で直す（空振りして本物を呼ぶ）。
- **本物の検索を確かめるテストは `@pytest.mark.real_search`**（差し替えを外す）。DB と埋め込みは、そのテスト自身が偽物にする。
- **モックモードに固定する。** `conftest.py` は `GEMINI_API_KEY` `DATABASE_URL` を空にして import する。`.env` に実キーがあっても動く。この冒頭を変えない。
- **API の応答にエラー本文・サーバーのパスが出ないことを、テストで確かめる**（固定メッセージ）。
- 実 API を呼ぶ検証（`tests/real_api_*`、`npm run test:e2e:real-api`）は課金。確認なしに実行しない。
- 既知の不安定なテスト: `test_access.py::test_the_default_role_behaves_exactly_like_access_mode_off`（再実行で通ることがある）。

## 実行

```bash
.venv/bin/python -m pytest -q                 # .venv/bin/pytest のシバンが古いパスを指すことがあるので、python -m を使う
.venv/bin/python -m pytest tests/test_search_pipeline.py
make test-db PYTEST=".venv/bin/python -m pytest"   # 一時 Postgres 17 込み
```
