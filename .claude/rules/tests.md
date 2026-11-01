---
paths:
  - "tests/**"
---

# tests/ を触るとき

詳細は [tests/CLAUDE.md](../../tests/CLAUDE.md)（領域ごとのファイル名の表、fixture の置き場所と命名）。

- 実 API・本物の DB・Supabase を呼ばない。`conftest.py` の冒頭（`GEMINI_API_KEY` `DATABASE_URL` を空にしてから import）を変えない
- サブディレクトリーを作らない・既存のテストを改名/移動しない（`tests.` からの import と `monkeypatch` の対象が崩れる）。新しいファイルは、領域ごとのファイル名の先頭に従う
- Postgres が要るテストは `TEST_DATABASE_URL` が無ければスキップ。DB だけのファイルは `test_<対象>_postgres.py`
- 補助モジュールは `test_` で始めない。`autouse` の fixture を `conftest.py` に足さない（全テストに効く）
- 検索の差し替え（`tools.search_chunks_async`、`search_vector.search`）の名前を、移動・改名と同じ変更で直す。本物の検索を確かめるテストは `@pytest.mark.real_search`
- 実 API を呼ぶ検証（`tests/real_api_*`）は、確認なしに実行しない
- 実行は `.venv/bin/python -m pytest`。触れた範囲を実行してから報告する
