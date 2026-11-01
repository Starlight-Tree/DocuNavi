# Supabase CLI での DB 変更

本番 DB（Supabase、スキーマ `chatbot`）の変更は、Supabase CLI の `db push --linked` で適用する（2026-10-26 から。0006 が最初）。
migration の正本は **`supabase/migrations/` だけ**（2026-10-26 に一本化。以前は連番名のフォルダーにも写しを置いていた）。

## 要点

- CLI は **ホスト（Mac）だけ**にある。開発コンテナには入っていない。`db push` はホストで人が実行する。エージェントは実行しない。
- 変更のファイルは `supabase/migrations/` に置く。本番への適用（CLI）と、ローカルの Compose（`db` の `/migrations` にマウント。`docs/development.md` の手順で手で適用）の両方が、このフォルダーを読む。
- `db push` の前に、必ず `migration list --linked` と `db push --linked --dry-run` を見る。

## 現状（2026-10-26 時点）

| 版（本番の台帳） | `supabase/migrations/` のファイル |
|---|---|
| 20261017184901 | `20261017184901_pgvector_embeddings.sql` |
| 20261017185742 | `20261017185742_embedding_input_hash.sql` |
| 20261018050236 | `20261018050236_document_title.sql` |
| 20261018080137 | `20261018080137_pgroonga_chunks.sql`（pgroonga。ローカルには入れない） |
| 20261018175020 | `20261018175020_document_collection.sql` |
| 20261025205258 | `20261025205258_document_collections.sql`（先頭に `set role postgres;` が 1 行ある） |

- 台帳は 6 版で、`supabase/migrations/` の 6 ファイルとバージョンが一致する（読み取りで確認、2026-10-26）。
- 0006（`20261025205258`）の先頭の 1 行の理由: CLI は適用のたびに一時的なロール `cli_login_postgres` を作る。SQL がそのロールのまま走ると、作ったテーブルの所有者がそのロールになりうる。
  それを避けるため、`postgres` に切り替える行を足した。適用後に所有者が `postgres` であることを確認済み。
  同じファイルをローカルの `postgres`（スーパーユーザー）で適用しても、自分への切り替えなので害はないはずだが、空の一時 DB（`chatbot` スキーマだけ）への適用は、2026-10-26 にホストで確認済み（エラーなし、初期値 3 行）。
- このファイルの冒頭コメントの「状態: 未適用」は古い（本番には適用済み）。適用済みのファイルは編集しない決まりなので、そのままにしている。
- `supabase/config.toml` は `supabase init` の既定に近い（`project_id = "docunavi"`、DB は major 17）。ローカルの Supabase（`supabase start`）は使っていない（2026-10-25 に中止）。

## 一本化の経緯（2026-10-26）

以前は、本番に適用する `supabase/migrations/` と、ローカルの Compose が読む連番名（`0001_…`〜`0006_…`）のフォルダーが並んでいた。
CLI は版（ファイル名の数字）しか比べず、内容の食い違いを検出できない（0006 の 1 行差がその例）ので、`supabase/migrations/` を正本に決め、連番名のフォルダーを廃止した。
`supabase/` 側のファイルは変えていない（本番の台帳に影響しない）。Compose の `db` のマウント元、`tests/test_collections.py` の読み先、コメント・文書の参照を `supabase/migrations/` に直した。
過去の比較の記録である `docs/target-architecture.md` の表の記述（SQL を連番名のフォルダーに手書きする、という記述）は、記録なので直していない。

## 新しい変更を本番に入れる手順

実行はホストで、リポジトリのフォルダーに移ってから行う。接続情報・パスワード・トークンは、画面・ログ・チャットに出さない。

1. 新しい版のファイルを作る: `supabase migration new <名前>`（`supabase/migrations/<時刻>_<名前>.sql` ができる）。SQL を書く。
   所有者が問題になる DDL（テーブル・関数の作成）は、先頭に `set role postgres;` を置く（0006 と同じ）。
2. ローカルの Compose の DB にも入れるなら、`docs/development.md` の手順に版を足す（別の場所に写しは置かない）。
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

- `.devcontainer/README.md` の「Supabase MCP」の節は、`apply_migration` で適用する記述を直し、CLI の手順（この文書）への案内にした。

## 未確認

- `supabase db reset`（ローカルの再構築）。`supabase/seed.sql` は無く、`config.toml` は seed を有効にしている。0001〜0006 だけで新しい環境が作れるかは試していない。
- ローカルの Compose の `docunavi` DB に、0001〜0003・0005・0006 を `docs/development.md` の手順で通しで適用すること（0006 単体は一時 DB で確認済み）。
- 失敗時の挙動（これまでの `db push` は 0006 の 1 回だけで、成功した）。
- `supabase start`（ローカルの Supabase）。使わない方針で、この文書の対象外。

## 関連ファイル

- `supabase/config.toml`、`supabase/migrations/`（本番に適用する側）
- `docker-compose.yml`（`db` が `supabase/migrations/` を `/migrations` に読み取り専用でマウント）、`db/local-init/`（初回の入れ物の作成。migration ではない）
- `docs/development.md`（ローカルの DB に手で適用する手順）
- 台帳の確認: Supabase MCP の `list_migrations`（`supabase`。書き込み可能な登録なので、読み取りのツールだけ使う）
