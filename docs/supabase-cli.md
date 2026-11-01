# Supabase CLI での DB 変更

本番 DB（Supabase、スキーマ `chatbot`）の変更は、Supabase CLI の `db push --linked` で適用する（2026-10-26 から。0006 が最初）。
migration の正本は **`supabase/migrations/` だけ**（2026-10-26 に一本化。以前は連番名のフォルダーにも写しを置いていた）。

## 要点

- CLI は **ホスト（Mac）と開発コンテナの両方**に置く（開発コンテナへの導入は 2026-11-02 に設定を追加。**リビルド前で、コンテナ内での実行は未確認**。下の「開発コンテナの Supabase CLI」）。
  本番への `db push` などの変更操作は、どちらの CLI からでも、**操作ごとに内容を示して承認を得てから**行う。エージェントが読み取りの確認（`projects list`、`migration list` など）を実行するのは構わない。
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

実行は、リポジトリのフォルダーに移ってから行う（これまではホスト。開発コンテナの CLI からも実行できるが、承認の扱いは「要点」のとおり）。接続情報・パスワード・トークンは、画面・ログ・チャットに出さない。

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

## 開発コンテナの Supabase CLI（2026-11-02。設定の追加まで。リビルドと初回ログインは未実施）

- **導入:** `.devcontainer/Dockerfile` が、公式のリリース v2.122.0（2026-10-23 公開の安定版）を、公式の `checksums.txt` の SHA-256 と照合して入れる（arm64 だけ。`supabase` と `supabase-go` の 2 つ）。
  取得したファイルのハッシュが `checksums.txt` の値と一致することは、2026-11-02 に作業用の場所で確かめた（証拠等級 A）。**開発コンテナの中での実行は、リビルド後に確かめる（未確認）。**
- **ログイン情報の保存先:** `~/.supabase`（`access-token`。平文、権限 600。ほかに `telemetry.json`）。D-Bus の資格情報ストアが無いこの環境で、**偽のトークン**を使い、`SUPABASE_HOME` を作業用の場所にして確かめた（2026-11-02、A）。
  バイナリには資格情報ストア（`org.freedesktop.secrets`）の文字列もある。ストアが使える環境では、保存先がファイルとは限らない（その場合、`~/.supabase` のマウントだけでは足りない。未確認）。
- **永続化:** 名前付きボリューム `supabase-config` を `~/.supabase` にマウントする（`.devcontainer/docker-compose.yml`）。所有者と権限（700）は `postCreateCommand` の `supabase-volume` が整える。
  Supabase のアクセストークンは、アカウント全体の管理権限を持つ。それがコンテナ内で読める平文のファイルとして残るリスクを、利用者が受け入れた（2026-11-02）。ガードのフック（`guard-cloud-cli.py`）は `supabase` を見ない。
- **初回ログイン:** 利用者が端末で行う。`supabase login --no-browser`（表示された URL をブラウザーで開き、確認コードを端末に入力する）。トークンをチャットに貼らない。`--token` で引数に書かない。この流れは、開発コンテナでは未確認。
- **リビルドで残るもの:** ログイン情報（ボリューム）、`supabase/.temp/`（link の状態。ワークスペースのバインドマウント。Git 管理外）。CLI 本体は、Dockerfile から再導入される。
- **再ログインが要るとき:** トークンの期限切れ、ダッシュボードの Access Tokens での失効、ボリュームの削除（`docker volume rm`、`down -v`、`prune`、OrbStack の仮想ディスクごとの削除）。**ボリュームの削除や開発環境ごとの削除は、このボリュームでは防げない。** トークンの有効期限の長さは、確認していない。
- **消し方:** ボリューム全体は、ホストで `docker volume rm supabase-config-<id>`。中身だけなら、開発コンテナで `rm -rf ~/.supabase/*`。トークン自体の失効は、Supabase のダッシュボードの Access Tokens から行う。
- **DB 接続情報とは別:** CLI のログインは、Python の取り込み（`python -m app.hero_images`）の DB 接続（`HERO_IMAGES_DATABASE_URL`）を設定しない。接続文字列は、既存の `pg-config`（`~/.pg`）に、CLI とは別に置く方針（2026-11-02。保存の処理は未実施）。
- **エージェントの扱い:** CLI や DB 接続の処理が、認証情報を内部で使うことは構わない。トークン・接続文字列・`access-token` の中身を、画面・チャット・ログに出さない。
  変更操作（`db push`、`migration up`、`functions deploy` など）は、操作ごとの承認を得る。`login`・`logout`・`link`・`unlink` も、実行前に確認する。
  `.claude/settings.json` の権限規則は、この方針を補う安全装置にとどまる。規則だけで、秘密情報へのアクセスを完全には遮断できない（別の書き方、`bash -c`、ファイルの直接の読み出しなど）。規則の変更は未適用（案を提示中）。

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
- **`supabase projects api-keys` など、秘密情報を表示するコマンドは実行しない**（キーが画面・会話の記録に出る）。`~/.supabase/access-token` と `~/.pg/` の中身も、表示しない。
- **適用済みの migration ファイルを手で編集して、再適用しない。** 直すときは新しい版を足す。
- **台帳とファイルが食い違ったまま、次の適用をしない。** 先に `migration list --linked` で揃っていることを確かめる。

## 注意

- `.devcontainer/README.md` の「Supabase MCP」の節は、`apply_migration` で適用する記述を直し、CLI の手順（この文書）への案内にした。

## 未確認

- `supabase db reset`（ローカルの再構築）。`supabase/seed.sql` は無く、`config.toml` は seed を有効にしている。0001〜0006 だけで新しい環境が作れるかは試していない。
- ローカルの Compose の `docunavi` DB に、0001〜0003・0005・0006 を `docs/development.md` の手順で通しで適用すること（0006 単体は一時 DB で確認済み）。
- 失敗時の挙動（これまでの `db push` は 0006 の 1 回だけで、成功した）。
- `supabase start`（ローカルの Supabase）。使わない方針で、この文書の対象外。
- 開発コンテナの CLI（2026-11-02 に設定を追加）: リビルドでの導入（イメージのビルドが通ること、`supabase --version` が 2.122.0 を返すこと）、`supabase-config` ボリュームのマウントと権限（700）、
  初回ログイン（`login --no-browser` の流れ）、リビルド後にログインが残ること、トークンの有効期限。

## 関連ファイル

- `supabase/config.toml`、`supabase/migrations/`（本番に適用する側）
- `docker-compose.yml`（`db` が `supabase/migrations/` を `/migrations` に読み取り専用でマウント）、`db/local-init/`（初回の入れ物の作成。migration ではない）
- `docs/development.md`（ローカルの DB に手で適用する手順）
- 台帳の確認: Supabase MCP の `list_migrations`（`supabase`。書き込み可能な登録なので、読み取りのツールだけ使う）
