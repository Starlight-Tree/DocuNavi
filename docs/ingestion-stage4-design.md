# 段階 4 の設計（取り込みの構成案。2026-10-14）

[roadmap.md](roadmap.md) の「段階 4 の設計」節を、2026-11-01 に、内容を変えずにこの文書へ移した（ロードマップは順序と状態、ここは設計と確認の記録）。
4b の後続の設計は [r2-ingest-design.md](r2-ingest-design.md)、検索の設計は [search-design.md](search-design.md)。

取り込みの構成案です。4a で手動のものを作り、4b で自動化します。

```text
R2 のイベント通知 → Cloudflare Queue → Cloud Run の取り込み（サービスまたはジョブ）
  → 抽出（必要なら PDF 変換）→ チャンク化 → 埋め込み → Postgres + pgvector に upsert
FastAPI + LangGraph → 検索 → 回答 + 参照元（source から署名付き URL）
```

- **取り込みはチャット用サービスと分ける。** 抽出や埋め込みは重く、同じサービスに入れると、応答の遅延と「容量の注意」（`--concurrency`）の計算を崩す。
- **Cloudflare 側は薄い中継にする。** Worker のメモリは 128 MB（isolate ごと）で、queue consumer の CPU 時間も限られる。PDF の抽出や埋め込みは Cloud Run で行う。
- **冪等にする。** イベントは重複して届きうる。`documents` に内容のハッシュと状態を持たせ、`chunks` は `UNIQUE (document_id, chunk_index)` で upsert する。
- **参照元は DB のデータとして持つ。** 回答時に `source` から署名付き URL を作る。LLM に URL を書かせない。

データモデルの出発点:

| テーブル | 列 |
|---|---|
| `documents` | `id`, `source`, `content_hash`, `status`, `created_at`, `updated_at` |
| `chunks` | `id`, `document_id`（`ON DELETE CASCADE`）, `chunk_index`, `page`, `content`, `embedding vector(N)`, `embedding_model`, `embedding_dimensions`, `created_at` |

- `vector(N)` は、索引を作るために次元を型で固定する。モデルを変えて次元が変わると、列かテーブルの作り直しになるので、`embedding_model` と `embedding_dimensions` を持たせる。
- `tsvector` 列は入れない（次の項）。

**日本語の全文検索（4c）。** Postgres 標準の全文検索（`tsvector`）は英数字の言語向けで、日本語のキーワード検索はほぼ効かない。

| 方法 | Supabase | Cloud SQL |
|---|---|---|
| PGroonga | 使える | 見つからなかった |
| pg_bigm（2-gram） | 未確認 | 使える（`cloudsql.enable_pg_bigm`） |
| アプリ側で分かち書き → `simple` に格納 | 使える | 使える（辞書の依存が増える。未検証） |

索引は `content` 列に後から作れるので、再取り込みは要らない。検索は 1 つのモジュール（または SQL 関数）に閉じ込め、索引の違いを吸収する。

**R2 から Cloud Run への受け渡し（4b）。**

| 方式 | 内容 | 引っかかる点 |
|---|---|---|
| push | Worker が Cloud Run を HTTP で呼ぶ | Cloud Run を認証必須にしているので、Worker から Google の ID トークンを作る手間が出る |
| pull | Cloud Run のジョブが Queue を HTTP で引く | 起こす係として Cloud Scheduler が要り、遅延が出る（最短 1 分間隔の見込み）。Cloudflare の API トークンを Secret Manager に置く |

どちらにするかは 4b で試して決める。今の仮説は pull（認証必須の方針を保てる）。

**確認の状況（2026-10-14）。**

- 確認した（公式ドキュメント）: R2 のイベント通知は Queue に届き、consumer Worker か HTTP pull で読める。Supabase の接続方式（direct / session / transaction）の違い。Supabase の PGroonga。Cloud SQL の pgvector と pg_bigm。
- 未確認: Supabase での pg_bigm、Cloud Scheduler の最短間隔、接続数の上限、IPv6 が開発コンテナと Cloud Run から届くか、PDF 変換（LibreOffice など）をこの環境で動かせるか。

**段階 1（会話履歴の DB 保存）の確認の状況（2026-10-14）。**

- 確認した（当時の構成: 開発コンテナ内のローカル Postgres 17、Debian パッケージ。現在のローカルの DB は `docs/development.md` の「ローカルの標準構成」）: 履歴が再起動後も続く（実際の uvicorn の再起動も）、スレッドが混ざらない、初回の同時起動（8 並列）が再試行で収まる（再試行なしでは 8 件中 7 件前後が競合のエラーになる。大半は `UniqueViolation` で、`DuplicateObject` と `DuplicateTable` も出る。CI で `DuplicateTable` が再試行の対象外だったために落ちた）、
  DB 側から接続を切られても次のリクエストが成功する、取り消しで接続が取り残されない、届かない DB は 10 秒で起動に失敗する（終了コード 3）、
  専用ロール（自分のスキーマの所有者）で動き他のテーブルは読めない。接続の確認を外す・再試行を外すと、対応するテストが落ちることも確かめた。
- 確認した（Supabase の Shared pooler の transaction mode（6543）、開発コンテナから、2026-10-14。LLM はモック）: 専用ロール（スキーマの所有者）の接続で起動して
  テーブルを作る、テーブルが `public` ではなくアプリのスキーマにできる（ロールの `search_path` がプーラー経由でも効く）、`/health` が `postgres`、
  `/chat` の往復、再起動をまたいで同じ `thread_id` の続きを読む（チェックポイントの鎖が続く）、`--thread-id` での会話の削除と
  `--older-than-days` の件数の表示（0 件）。テーブルの所在・鎖・ポート（6543）・`grant … to postgres` が要ったことは、利用者の報告による。
- 未確認: Cloud SQL への実接続、
  Cloud Run から届くか、Supabase 上での同時の初回起動・接続切断（`tests/test_history_postgres.py` を Supabase に向けた実行。管理者の URL が要る）、
  Supabase の Exposed schemas にアプリのスキーマが入っていないこと（画面の確認）、`--older-than-days --apply` の Supabase での実行（古い会話がまだ無い）、
  Docker イメージのビルドと `docker compose up`（開発コンテナに Docker が無い）、履歴が増え続けたときのサイズ。
  保存期間の方針は 90 日で、定期実行は 2026-10-20 に Cloud Run Job + Cloud Scheduler で設定した（最初は 7 日。この段落の他の項目は 2026-10-14 時点の記録）。削除の手段は `python -m app.history_admin`（`docs/configuration.md`）。
  CI の `backend` ジョブには Postgres 17 のサービスコンテナを足した（`.github/workflows/ci.yml`）。
