# 検証環境（AlloyDB Express）の構築メモ

> **状態（2026-11-18）:** Preview の製品です。料金は公式の価格表と照合していません（未確認）。消えても再作成できる前提で、作り直してかまいません。**これは検証環境で、本番の移行ではありません。本番は Prisma Postgres です。**

概要・構成図・SSE と起動の測定は [architecture.md](architecture.md#検証環境alloydb-express)（正本）。ここは再作成の名前・手順・落とし穴だけで、値（パスワード・接続 URL・プロジェクト ID・画面の URL）は書きません。Cloud SQL 版（`docunavi-gcp-db`）も作りましたが、2026-11-17 に削除しました。

## 目的と前提

- 本番の Prisma 上限に当たったときの代替と、別の PostgreSQL 互換 DB での動作確認です。データはダミーで、ローカルから同じマイグレーションで反映します。
- 接続は Private Service Connect（PSC）だけです（作成後に変えられない）。手元からは届かないので、SQL・マイグレーション・投入は Cloud Run Job で流します。
- 未確認: PSC と AlloyDB の料金（貼られた資料の「課金開始 2026-12」も未照合）。クレジットの期限（2027-01 中旬ごろ）も未確認です。

## リソース名（`docunavi-gcp-*`、東京）

| 種類 | 名前 |
|---|---|
| クラスタ / インスタンス | `docunavi-gcp-alloy` / `docunavi-gcp-alloy-primary`（Express、PostgreSQL 18.4、1 vCPU、ZONAL、バックアップなし、`ENCRYPTED_ONLY`） |
| PSC | サブネット `docunavi-gcp-subnet`、サービス接続ポリシー `docunavi-gcp-alloydb-psc`。API は `alloydb` `networkconnectivity` `dns` |
| DB / 拡張 | `docunavi` / `vector` 0.8.7.google-1 |
| Cloud Run | API `docunavi-gcp`（h2c）、認証 `docunavi-gcp-auth`。ともに Direct VPC egress。画面は Worker `docunavi-gcp-web` |
| Job | `docunavi-gcp-psql`（準備 SQL）、`docunavi-gcp-migrate`、`docunavi-gcp-ingest` |

API の `AUTH_BASE_URL`・`CORS_ORIGINS` は画面の origin、`AUTH_JWKS_URL` は認証サービス直です。

## サービスアカウントと Secret（名前だけ）

- `docunavi-gcp-run`（psql・migrate）: プロジェクトロールなし。`alloy-postgres-password` `alloy-migrator-url` `alloy-ca` の accessor だけ。
- `docunavi-gcp-auth`: プロジェクトロールなし。`alloy-app-url` `alloy-ca` `auth-secret` `google-client-id` `google-client-secret` の accessor だけ。
- `docunavi-gcp-api`（API・ingest）: `roles/aiplatform.user`（プロジェクト）と、`alloy-app-pg-url`・`alloy-ca` の accessor。
- SA の作成直後は binding が失敗するので、20 秒待って再実行します。
- Secret Manager の名前は接頭辞 `docunavi-gcp-`。`alloy-migrator-url`・`alloy-app-pg-url` は libpq 形式で、`sslmode=verify-ca` と `sslrootcert=/certs/ca.pem` を付ける。`alloy-app-url`（認証用）に問い合わせ文字列はない。
- Infisical prod（正本）: `GCP_ALLOYDB_POSTGRES_PASSWORD` `GCP_ALLOYDB_MIGRATOR_PASSWORD` `GCP_ALLOYDB_APP_PASSWORD` `GCP_BETTER_AUTH_SECRET`。Google のクライアントは prod の `GOOGLE_CLIENT_*` から作ります（`docunavi-local` は使わない）。
- **パスワードの複雑さ:** hex だけ（小文字と数字）は拒否されます。`A_` + hex 48 桁（50 文字）は通りました（2026-11-17、A）。
- リージョン Secret の癖（Cloud SQL の `execute-sql` で踏んだ。AlloyDB では使わない）: `--password-secret-version` はリージョン Secret だけで、作成には環境変数 `CLOUDSDK_API_ENDPOINT_OVERRIDES_SECRETMANAGER`（東京の `rep.googleapis.com`）と `--location` が要ります。

## TLS とサーバー CA

- 接続先は PSC の IP なので `verify-ca`（CA の署名だけ検証。ホスト名は見ない）です。[../db/migrate.py](../db/migrate.py) は `verify-ca` 以上と CA ファイルを必須にし、認証サービスは `DATABASE_CA_FILE` で同じ検証をします。`docunavi-gcp-psql` の準備 SQL だけは `sslmode=require` です。
- **CA の取得:** AlloyDB API の `POST …/clusters/docunavi-gcp-alloy:generateClientCertificate` に使い捨ての RSA 公開鍵を渡すと、応答の `caCert` が返ります（`root.alloydb`、期限 2036-11。`getConnectionInfo` に証明書チェーンはない）。CA は Secret に入れ、イメージには焼きません。

## ユーザーの分離と権限の検証

- `postgres`: 準備 SQL だけ。`docunavi_nopriv`: 権限なしの空ロール（`--db-roles` の受け皿）。
- `docunavi_migrator`: マイグレーション。`public` の USAGE+CREATE、表の所有者。
- `docunavi_app`: API・認証の実行時。表の DML・シーケンスだけ（DDL なし、所有者でない）。
- 作成: `gcloud alloydb users create <名前> --db-roles=docunavi_nopriv`。
- 準備 SQL（postgres）: `CREATE EXTENSION IF NOT EXISTS vector`（migrator は作れないので先に）、両ユーザーに `CONNECT`、`PUBLIC` の `CONNECT` を外す、migrator に `public` の `USAGE, CREATE`（PG15 以降は自動では付かない）、app に `USAGE`。
- 表の権限は `MIGRATE_APP_ROLE=docunavi_app` で `migrate.py --apply` が付けます（`schema_migrations` は外す）。**`GRANT … ON ALL TABLES` は使いません。** AlloyDB の内部表（`public`）で permission denied になるため、migrator 所有の表を名指しします。
- 検証（読み取り）: `pg_roles` の `rolsuper` 系 5 属性が migrator・app とも false、`pg_has_role(…, 'alloydbsuperuser', 'member')` が false、所属が `docunavi_nopriv` だけ、app は `public` に CREATE 不可（migrator は可）、app 所有の表が 0 行。結果（2026-11-17、A）: 0001〜0005 が通り、app は 18 表に DML（`schema_migrations` は不可）。

## Job と `gcloud run jobs update --args`

- **イメージ:** `scripts/preview.sh build`（ビルドと push だけ）を migrate と ingest で使い、認証は `SERVICE=docunavi-gcp-auth BUILD_CONFIG=cloudbuild-auth.yaml` つきの同じ `build` です。タグ配備（`deploy/*`）は本番に版を作るので使いません。
- **`docunavi-gcp-psql`:** `postgres:18-alpine`、`PGPASSWORD` は Secret。`--args` は `-c` の重複を拒否するので、`sh -c 'psql … -c … -c …'` の 1 本に包みます。
- **区切り:** `--args` はカンマ区切りです。引数にカンマ（SQL など）があれば、先頭で区切りを宣言します（`--args='^@^…@…'`。本文に `@` があれば `^#^`）。
- **`-` で始まる値:** `--args -m,…` は別フラグと解釈されます。`--command=python '--args=-m,app.ingestion.embed,…'` と `=` 形式にします。
- `docunavi-gcp-migrate` は `db/migrate.py --apply`。`docunavi-gcp-ingest` は `-m app.ingestion.embed --apply --max-calls N`、続けて `-m app.ingestion.suggest --apply`。**埋め込みは 1 件 1 回の呼び出しなので、`--max-calls` は件数以上に**（20 では止まった）。Vertex の 429 は途中で止まり、再実行で続きから入ります（原因は未確認）。

## 作り直す順序

1. API 有効化、PSC、クラスタ・インスタンス（Express と PSC の併用は可、2026-11-17、A）。
2. パスワードを Infisical と Secret Manager に作り、CA を取得して Secret に入れる。
3. `docunavi-gcp-psql` で準備 SQL と検証、イメージをビルドして `docunavi-gcp-migrate`、`docunavi-gcp-ingest`。
4. 認証サービスと API を作る（`/health` が 200、無認証の `/chat` が 401）。
5. 画面の Worker を、ホストで `--name docunavi-gcp-web` を付けて配備（`deploy-web.sh` の既定値は本番。architecture.md）。
6. Google OAuth の承認済みリダイレクト URI に、画面の origin の `/api/auth/callback/google` を足す（ユーザーの操作）。`app_members` に本人を入れ、`MEMBERSHIP_MODE=enforce` にする。

検証環境の URL は README・About に載せません。
