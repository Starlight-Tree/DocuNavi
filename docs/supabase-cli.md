# Supabase CLI での DB 変更

本番 DB（Supabase、スキーマ `chatbot`）の変更は、Supabase CLI の `db push --linked` で適用する（2026-10-26 から。0006 が最初）。
この文書は現状の記録と手順で、**現状は二重管理であり、どちらのフォルダーを正本にするかは決めていない**（下の「二重管理」）。

## 要点

- CLI は **ホスト（Mac）だけ**にある。開発コンテナには入っていない。`db push` はホストで人が実行する。エージェントは実行しない。
- 変更のファイルは `supabase/migrations/`（本番に適用する側）に置く。**現状の運用では、同じ内容を `db/migrations/`（ローカルの Compose が読む側）にも置く。**
  これは二重管理の現状に合わせた運用で、`db/migrations/` を正本に決めたわけではない。
- `db push` の前に、必ず `migration list --linked` と `db push --linked --dry-run` を見る。

## 現状（2026-10-26 時点）

| 版（本番の台帳） | `supabase/migrations/` | `db/migrations/` | 内容の差 |
|---|---|---|---|
| 20261017184901 | `…_pgvector_embeddings.sql` | `0001_pgvector_embeddings.sql` | 同一 |
| 20261017185742 | `…_embedding_input_hash.sql` | `0002_embedding_input_hash.sql` | 同一 |
| 20261018050236 | `…_document_title.sql` | `0003_document_title.sql` | 同一 |
| 20261018080137 | `…_pgroonga_chunks.sql` | `0004_pgroonga_chunks.sql` | 同一 |
| 20261018175020 | `…_document_collection.sql` | `0005_document_collection.sql` | 同一 |
| 20261025205258 | `…_document_collections.sql` | `0006_document_collections.sql` | **`supabase/` 側だけ、先頭に `set role postgres;` が 1 行ある** |

- 台帳は 6 版で、`supabase/migrations/` の 6 ファイルとバージョンが一致する（読み取りで確認、2026-10-26）。
- 0006 の 1 行の理由: CLI は適用のたびに一時的なロール `cli_login_postgres` を作る。SQL がそのロールのまま走ると、作ったテーブルの所有者がそのロールになりうる。
  それを避けるため、`postgres` に切り替える行を足した。適用後に所有者が `postgres` であることを確認済み。
- `supabase/config.toml` は `supabase init` の既定に近い（`project_id = "docunavi"`、DB は major 17）。ローカルの Supabase（`supabase start`）は使っていない（2026-10-25 に中止）。

## 二重管理（正本は未決定）

`db/migrations/` と `supabase/migrations/` の両方が存在する。**どちらも正本とは決めていない。** 現状の読まれ方は次のとおり。

- 本番への適用は `supabase/migrations/`（CLI）。
- ローカルの Compose は `db/migrations/`（`docker-compose.yml` が `/migrations` にマウントし、`docs/development.md` の手順で手で適用する）。

このため、**現状の運用としては、両方に置く必要がある。** これは `db/migrations/` を正本に決めたことではない。**危険:**

- **片方だけに足すと、もう片方に届かない。** 本番とローカルで、DB の形が食い違う。
- **CLI は版（ファイル名の数字）しか比べない。** 内容が食い違っても検出されない（0006 の 1 行差がその例）。
- 名前の付け方が違う（`0006_…` と `20261025205258_…`）。

**一本化するときの修正対象**（`db/migrations/` を参照している場所）:

- `docker-compose.yml`（`db` のマウント）
- `docs/development.md`（ローカルの DB に手で適用する手順。139・152 行目付近）
- `docs/api.md`（220 行目付近）
- `app/ingestion/service.py`（30 行目のコメント）
- `db/local-init/01-bootstrap.sh`（10 行目のコメント）
- `.devcontainer/README.md`（308 行目付近。下の「注意」も参照）

一本化は、別途承認してから行う。この文書では行わない。

## 新しい変更を本番に入れる手順

実行はホストで、リポジトリのフォルダーに移ってから行う。接続情報・パスワード・トークンは、画面・ログ・チャットに出さない。

1. 新しい版のファイルを作る: `supabase migration new <名前>`（`supabase/migrations/<時刻>_<名前>.sql` ができる）。SQL を書く。
   所有者が問題になる DDL（テーブル・関数の作成）は、先頭に `set role postgres;` を置く（0006 と同じ）。
2. 同じ内容を `db/migrations/<連番>_<名前>.sql` にも置く（`set role postgres;` の行だけは `supabase/` 側のみ）。
   **これは現状の二重管理の運用であり、正本は未決定**（上の「二重管理」）。
3. 台帳との対応を見る: `supabase migration list --linked`
   → 期待: 既存の版が両側にあり、**足したものだけが「ローカルのみ」**。
4. 適用内容を見る: `supabase db push --linked --dry-run`
   → 期待: 足したファイルだけが列挙される。他のファイルが出たら、**止めて原因を調べる**。
5. 適用する: `supabase db push --linked`
6. 確認する（読み取り）: `migration list --linked` で両側が揃っていること、必要なら作ったものの所有者・権限・行数。

`--linked` は省略しない（CLI は、既定が次のメジャーでローカルに変わると警告している）。

## 失敗したとき

- **再試行しない。** エラーの全文（接続情報を含まない範囲）を残す。
- `migration list --linked` と、変更が部分的に入っていないかを読み取りで確認する。
- 元に戻す DROP などの書き込みは、そのつど承認を得てから行う。コードを先に戻す。

## してはいけないこと

- **`supabase db reset --linked`、`supabase db pull`、`supabase db dump` は、計画なし・承認なしで実行しない。**
  `reset --linked` は本番を消す。`db pull` は `_remote_schema` の基準版と重複した履歴を足す。
- **`supabase migration repair` は、対象と影響を示して承認を得てから。**
- **本番 DB への直接の書き込みは承認制。** `apply_migration` は deny（`.claude/settings.json`）。MCP 経由で DDL を適用しない
  （適用すると台帳とファイルが食い違う）。
- **適用済みの migration ファイルを手で編集して、再適用しない。** 直すときは新しい版を足す。
- **台帳とファイルが食い違ったまま、次の適用をしない。** 先に `migration list --linked` で揃っていることを確かめる。

## 注意

- `.devcontainer/README.md` の「Supabase MCP」の節（308 行目付近）に、「DDL は `db/migrations/` に SQL を残してから、`apply_migration` で適用します」とある。
  **この記述は古い。** `apply_migration` は今は deny で、本番への適用は CLI で行う。この文書では修正していない（別途直す）。

## 未確認

- `supabase db reset`（ローカルの再構築）。`supabase/seed.sql` は無く、`config.toml` は seed を有効にしている。0001〜0006 だけで新しい環境が作れるかは試していない。
- 失敗時の挙動（これまでの `db push` は 0006 の 1 回だけで、成功した）。
- `supabase start`（ローカルの Supabase）。使わない方針で、この文書の対象外。

## 関連ファイル

- `supabase/config.toml`、`supabase/migrations/`（本番に適用する側）
- `db/migrations/`（ローカルの Compose が読む側。`docker-compose.yml` の `db`）
- `docs/development.md`（ローカルの DB に手で適用する手順）
- 台帳の確認: Supabase MCP の `list_migrations`（読み取り専用の `supabase-read`）
