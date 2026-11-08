# 本番 Postgres のバックアップ（毎日 1 回、R2 へ）

Prisma Postgres の Free プランには、ドキュメント化されたバックアップが無い。そのため、Cloud Run Job `docunavi-pg-backup` が毎日 1 回 `pg_dump` して R2 に置く。

| 項目 | 内容 |
|---|---|
| 保存先 | R2 バケット `docunavi-backups`（非公開、APAC）。キーは `pg/docunavi-YYYYMMDD-HHMMSS.dump`（UTC） |
| 形式 | `pg_dump --format=custom --no-owner --no-privileges`。全表（`account` のトークンと `jwks` の秘密鍵を含む。バケットは非公開のまま） |
| 保持 | 14 日。バケットのライフサイクル規則 `delete-after-14d`（作成から 14 日で期限切れ。削除は期限後おおむね 24 時間以内なので、14〜15 世代が残る）。Job は消さない |
| 実行 | Cloud Scheduler `docunavi-pg-backup-daily`、毎日 03:30（Asia/Tokyo） |
| 接続 | Secret Manager `database-url`（版 **1** に固定）。プール経由のホストなので、`pg_dump` が通らなければ直接ホスト用の Secret を足す（未確認） |
| R2 のキー | Secret Manager `r2-backup-rw`（`ops/r2_key_issuer` の `issue-backup-rw` が発行。バケットは `docunavi-backups` だけ）。**ローテーション未実装**（取り消しは Cloudflare のダッシュボードで行う） |
| 流れ | `pg_dump` → `pg_restore --list` で必要な表（`documents` `chunks` `user` `account`）が入っているか検査 → R2 に保存 → 保存したサイズを確認。どこかで失敗したら保存しない |
| ログ | バイト数・表の数・キーだけ。接続文字列・パスワード・R2 のキーは出さない（失敗時の `pg_dump` の出力は先頭 5 行、パスワードとユーザー名を伏せて出す） |
| 終了コード | 0 成功 / 2 設定不足 / 3 `pg_dump` 失敗 / 4 検査失敗 / 5 保存失敗 / 1 想定外 |

本番 DB には読み取りしかしない。D1・Vectorize・既存の Cloud Run サービスとリビジョンには触れない。

## 権限

| サービスアカウント | 付ける権限 |
|---|---|
| `docunavi-backup`（Job） | `secretmanager.secretAccessor` を Secret `database-url` と `r2-backup-rw` だけに |
| `docunavi-backup-scheduler` | `run.invoker` を Job `docunavi-pg-backup` だけに |
| `r2-key-issuer`（既存） | `secretmanager.secretAccessor` と `secretVersionAdder` を Secret `r2-backup-rw` だけに |

## 配備（ユーザーがホストで実行。書き込み系の gcloud）

```bash
as_me() { env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT "$@"; }
P=docunavi-app R=asia-northeast1
REPO=$R-docker.pkg.dev/$P/cloud-run-source-deploy
TAG=$(git rev-parse --short HEAD)

# 1. サービスアカウント
as_me gcloud iam service-accounts create docunavi-backup --project $P --display-name "DB backup job"
as_me gcloud iam service-accounts create docunavi-backup-scheduler --project $P --display-name "DB backup scheduler"

# 2. Secret（空。キーは issue-backup-rw が入れる）と権限
as_me gcloud secrets create r2-backup-rw --project $P --replication-policy automatic
for m in secretAccessor secretVersionAdder; do
  as_me gcloud secrets add-iam-policy-binding r2-backup-rw --project $P \
    --member serviceAccount:r2-key-issuer@$P.iam.gserviceaccount.com --role roles/secretmanager.$m
done
as_me gcloud secrets add-iam-policy-binding r2-backup-rw --project $P \
  --member serviceAccount:docunavi-backup@$P.iam.gserviceaccount.com --role roles/secretmanager.secretAccessor
as_me gcloud secrets add-iam-policy-binding database-url --project $P \
  --member serviceAccount:docunavi-backup@$P.iam.gserviceaccount.com --role roles/secretmanager.secretAccessor

# 3. イメージ（発行用は新しいモードを含むので作り直す。既存の Job は古いイメージのまま）
as_me gcloud builds submit ops/r2_key_issuer --project $P --billing-project $P --tag $REPO/r2-key-issuer:backup-$TAG
as_me gcloud builds submit ops/pg_backup     --project $P --billing-project $P --tag $REPO/pg-backup:$TAG

# 4. キーの発行 Job を作って 1 回実行する
as_me gcloud run jobs create r2-issue-backup-rw --project $P --region $R \
  --image $REPO/r2-key-issuer:backup-$TAG --args issue-backup-rw \
  --service-account r2-key-issuer@$P.iam.gserviceaccount.com --max-retries 0 --task-timeout 300 \
  --cpu 1 --memory 512Mi \
  --set-env-vars CLOUDFLARE_ACCOUNT_ID=<CLOUDFLARE_ACCOUNT_ID>,R2_BUCKET=docunavi-backups,GCP_PROJECT_ID=$P
as_me gcloud run jobs execute r2-issue-backup-rw --project $P --region $R --wait

# 5. バックアップ Job
as_me gcloud run jobs create docunavi-pg-backup --project $P --region $R \
  --image $REPO/pg-backup:$TAG --service-account docunavi-backup@$P.iam.gserviceaccount.com \
  --max-retries 1 --task-timeout 600 --cpu 1 --memory 512Mi \
  --set-env-vars CLOUDFLARE_ACCOUNT_ID=<CLOUDFLARE_ACCOUNT_ID>,R2_BUCKET=docunavi-backups \
  --set-secrets DATABASE_URL=database-url:1,R2_CREDENTIALS_JSON=r2-backup-rw:latest

# 6. 1 回だけ手で実行して確かめてから、スケジュールを作る
as_me gcloud run jobs execute docunavi-pg-backup --project $P --region $R --wait

as_me gcloud run jobs add-iam-policy-binding docunavi-pg-backup --project $P --region $R \
  --member serviceAccount:docunavi-backup-scheduler@$P.iam.gserviceaccount.com --role roles/run.invoker
as_me gcloud scheduler jobs create http docunavi-pg-backup-daily --project $P --location $R \
  --schedule "30 3 * * *" --time-zone Asia/Tokyo \
  --uri "https://$R-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$P/jobs/docunavi-pg-backup:run" \
  --http-method POST --oauth-service-account-email docunavi-backup-scheduler@$P.iam.gserviceaccount.com
```

Cloud Scheduler の API が無効なら、先に `as_me gcloud services enable cloudscheduler.googleapis.com --project $P`。

## 確認・復元

- 実行の結果: `gcloud run jobs executions list --job docunavi-pg-backup --region asia-northeast1` とログ（`backup ok: …` の行）。
- 失敗の通知は無い（ログだけ）。数日おきに、最新のキーの日付を確かめる。
- 復元の試験（ローカルの Postgres に戻す）: R2 から取った `.dump` を、`pg_restore --no-owner --dbname <ローカルの接続先> <ファイル>` で戻す。`vector` 拡張が要る（pgvector 入りのイメージ）。
- **ダンプには Prisma 固有の拡張 `prisma_postgres`（とそのコメント）の項目が 2 つ入っており、ローカルの Postgres には無いので、そのまま戻すと失敗する。** 一覧から外して戻す:
  `pg_restore -l <ファイル> | grep -v -i prisma_postgres > list` → `pg_restore --no-owner -L list --dbname <接続先> <ファイル>`。`--exit-on-error` は付けない（付けると、この拡張の項目で止まり、表が作られない）。
- 復元の試験の結果（2026-11-08、証拠等級 A）: `pgvector/pgvector:pg17`（`vector` 0.8.7）に `pg_restore` が exit 0・エラー 0 行で戻った。18 表すべての件数が、バックアップ直前の本番と一致した（`documents` 80、`chunks` 2313、`embedding` が入った `chunks` 2313、`jwks` 1、`user` 1）。
  試験に使ったダンプ（`account` のトークンと `jwks` の秘密鍵を含む）は、試験のあと `shred` で消した。試験の失敗時の通知や、Prisma への戻し（本番 DB への復元）は試していない。
