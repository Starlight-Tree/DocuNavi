# 本番 Postgres のバックアップ（週 1 回、R2 へ）

Prisma Postgres の Free プランには、ドキュメント化されたバックアップが無い。そのため、Cloud Run Job `docunavi-pg-backup` が週 1 回 `pg_dump` して R2 に置く（2026-11-10 に毎日から週 1 回へ。Free の operations〔200K/月〕を節約するため。`pg_dump` が何 operations と数えられるかは、実行の前後で `ops/prisma_usage_daily.py --watch` を流して測る）。**頻度を下げたぶん、最悪で直近 7 日の変更を失う**（Free には Prisma 側の自動バックアップが無い。Starter 以上は毎日・7 日保持）。

| 項目 | 内容 |
|---|---|
| 保存先 | R2 バケット `docunavi-backups`（非公開、APAC）。キーは `pg/docunavi-YYYYMMDD-HHMMSS.dump`（UTC） |
| 形式 | `pg_dump --format=custom --no-owner --no-privileges`。全表（`account` のトークンと `jwks` の秘密鍵を含む。バケットは非公開のまま） |
| 保持 | 通常のダンプ（`pg/docunavi-*`）は **14 日**。バケットのライフサイクル規則 `delete-after-14d`（プレフィックス `pg/docunavi-`。削除は期限後おおむね 24 時間以内。週 1 回なので 2〜3 世代が残る）。**月次の長期保持**: その月の最初のバックアップを、Job が R2 の中で `pg/monthly/docunavi-YYYYMM.dump`（UTC の月）に写す（サーバー側のコピー。DB には触れない）。規則 `delete-monthly-after-400d`（プレフィックス `pg/monthly/`）が **400 日**で消す。Job は消さない |
| 実行 | Cloud Scheduler `docunavi-pg-backup-daily`（名前は毎日だった頃のまま。IAM を作り直さないため残す）、毎週日曜 00:00 JST（＝土曜 15:00 UTC。cron `0 0 * * 0`、タイムゾーン `Asia/Tokyo`。2026-11-10 に月曜 18:30 UTC から変更） |
| 接続 | Secret Manager `database-url`（版 **1** に固定）。プール経由のホストなので、`pg_dump` が通らなければ直接ホスト用の Secret を足す（未確認） |
| R2 のキー | Secret Manager `r2-backup-rw`（`ops/r2_key_issuer` の `issue-backup-rw` が発行。バケットは `docunavi-backups` だけ）。**ローテーション未実装**（取り消しは Cloudflare のダッシュボードで行う） |
| 流れ | `pg_dump` → `pg_restore --list` で必要な表（`documents` `chunks` `user` `account`）が入っているか検査 → R2 に保存 → 保存したサイズを確認。どこかで失敗したら保存しない |
| ログ | バイト数・表の数・キーだけ。接続文字列・パスワード・R2 のキーは出さない（失敗時の `pg_dump` の出力は先頭 5 行、パスワードとユーザー名を伏せて出す） |
| 終了コード | 0 成功 / 2 設定不足 / 3 `pg_dump` 失敗 / 4 検査失敗 / 5 保存失敗 / 6 月次の写しの失敗（通常のダンプは保存済み）/ 1 想定外 |

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
  --set-env-vars CLOUDFLARE_ACCOUNT_ID=ec39c9c73795d4ff060e7f807ec2ad8b,R2_BUCKET=docunavi-backups,GCP_PROJECT_ID=$P
as_me gcloud run jobs execute r2-issue-backup-rw --project $P --region $R --wait

# 5. バックアップ Job
as_me gcloud run jobs create docunavi-pg-backup --project $P --region $R \
  --image $REPO/pg-backup:$TAG --service-account docunavi-backup@$P.iam.gserviceaccount.com \
  --max-retries 1 --task-timeout 600 --cpu 1 --memory 512Mi \
  --set-env-vars CLOUDFLARE_ACCOUNT_ID=ec39c9c73795d4ff060e7f807ec2ad8b,R2_BUCKET=docunavi-backups \
  --set-secrets DATABASE_URL=database-url:1,R2_CREDENTIALS_JSON=r2-backup-rw:latest

# 6. 1 回だけ手で実行して確かめてから、スケジュールを作る
as_me gcloud run jobs execute docunavi-pg-backup --project $P --region $R --wait

as_me gcloud run jobs add-iam-policy-binding docunavi-pg-backup --project $P --region $R \
  --member serviceAccount:docunavi-backup-scheduler@$P.iam.gserviceaccount.com --role roles/run.invoker
as_me gcloud scheduler jobs create http docunavi-pg-backup-daily --project $P --location $R \
  --schedule "0 0 * * 0" --time-zone Asia/Tokyo \
  --uri "https://$R-run.googleapis.com/apis/run.googleapis.com/v1/namespaces/$P/jobs/docunavi-pg-backup:run" \
  --http-method POST --oauth-service-account-email docunavi-backup-scheduler@$P.iam.gserviceaccount.com
```

既に毎日のスケジュールがあるときは、作らずに更新する（頻度を週 1 回にする。書き込み系なので、ユーザーがホストで実行）:

```bash
as_me gcloud scheduler jobs update http docunavi-pg-backup-daily --project $P --location $R \
  --schedule "0 0 * * 0" --time-zone Asia/Tokyo
as_me gcloud scheduler jobs describe docunavi-pg-backup-daily --project $P --location $R \
  --format="value(schedule,timeZone,state)"
```

Cloud Scheduler の API が無効なら、先に `as_me gcloud services enable cloudscheduler.googleapis.com --project $P`。

## 月次の長期保持の有効化（ユーザーがホストで。順番を守る）

`pg/monthly/` のキーは `pg/docunavi-` で始まらないので、規則をプレフィックスで分けられる。**先に規則を直し、その後で新しいイメージを配備する**（古い規則が `pg/` 全体に掛かっていると、月次の写しが 14 日で消える）。
`wrangler r2 bucket lifecycle` のサブコマンドと引数は、**この環境では未確認**（先に `list` で今の規則を見る。Cloudflare のダッシュボードの R2 → `docunavi-backups` → Settings → Object lifecycle rules でも同じ）。

```bash
npx wrangler r2 bucket lifecycle list docunavi-backups
# 1. 通常のダンプの規則を、プレフィックス pg/docunavi- に絞る（今の delete-after-14d を消して作り直す）
npx wrangler r2 bucket lifecycle remove docunavi-backups --name delete-after-14d
npx wrangler r2 bucket lifecycle add docunavi-backups delete-after-14d pg/docunavi- --expire-days 14
# 2. 月次の規則を足す
npx wrangler r2 bucket lifecycle add docunavi-backups delete-monthly-after-400d pg/monthly/ --expire-days 400
npx wrangler r2 bucket lifecycle list docunavi-backups   # 2 つの規則とプレフィックスを確かめる
```

規則の入れ替えの間（1 と 2 の間）は、通常のダンプに期限が掛からないだけで、消えるものは無い。その後、イメージを作り直して Job を更新する（`TAG=$(git rev-parse --short HEAD)`。手順 3・5 と同じ形）:

```bash
as_me gcloud builds submit ops/pg_backup --project $P --billing-project $P --tag $REPO/pg-backup:$TAG
as_me gcloud run jobs update docunavi-pg-backup --project $P --region $R --image $REPO/pg-backup:$TAG
```

次の実行（日曜 00:00 JST）のログに `monthly copy: key=pg/monthly/docunavi-YYYYMM.dump`（その月の最初）か `already exists this month` が出る。

## 確認・復元

- 実行の結果: `gcloud run jobs executions list --job docunavi-pg-backup --region asia-northeast1` とログ（`backup ok: …` の行）。
- 失敗の通知: Cloud Monitoring のログベースのアラート 2 つ（[Job の ERROR](../ops/monitoring/alert-pg-backup-failed.yaml)、[Scheduler の ERROR](../ops/monitoring/alert-pg-backup-scheduler-failed.yaml)）。メールの通知先は、既存の `docunavi ops`（[deploy.md](deploy.md) の「アラート」）を使う。作成のコマンドは下の「失敗の通知の作成」。
- **実行されなかったことは、通知できない。** 指標の不在を見るアラートの待ち時間の上限は約 24 時間で、週 1 回の実行には使えない（PromQL で 8 日の窓を見る方法は未確認）。毎週日曜の実行の後（月曜に）、最新のキーの日付を手で確かめる。
- 復元の試験（ローカルの Postgres に戻す）: R2 から取った `.dump` を、`pg_restore --no-owner --dbname <ローカルの接続先> <ファイル>` で戻す。`vector` 拡張が要る（pgvector 入りのイメージ）。
- **ダンプには Prisma 固有の拡張 `prisma_postgres`（とそのコメント）の項目が 2 つ入っており、ローカルの Postgres には無いので、そのまま戻すと失敗する。** 一覧から外して戻す:
  `pg_restore -l <ファイル> | grep -v -i prisma_postgres > list` → `pg_restore --no-owner -L list --dbname <接続先> <ファイル>`。`--exit-on-error` は付けない（付けると、この拡張の項目で止まり、表が作られない）。
- 復元の試験の結果（2026-11-08、証拠等級 A）: `pgvector/pgvector:pg17`（`vector` 0.8.7）に `pg_restore` が exit 0・エラー 0 行で戻った。18 表すべての件数が、バックアップ直前の本番と一致した（`documents` 80、`chunks` 2313、`embedding` が入った `chunks` 2313、`jwks` 1、`user` 1）。
  試験に使ったダンプ（`account` のトークンと `jwks` の秘密鍵を含む）は、試験のあと `shred` で消した。試験の失敗時の通知や、Prisma への戻し（本番 DB への復元）は試していない。

## 復旧訓練（`scripts/pg_restore_drill.sh`）

ダンプを、使い捨ての pgvector の Postgres（Docker）に戻し、表の件数を確かめる。本番 DB にも R2 にも触れない。**四半期に 1 回**と、保持や頻度を変えた後に行い、結果（日付・ダンプのキー・件数・OK/NG）を下の「訓練の記録」に足す。

1. 最新のダンプを手元に取る（ユーザーがホストで。ダンプには `account` のトークンと `jwks` の秘密鍵が入る）。
   Cloudflare のダッシュボードの R2 `docunavi-backups` の `pg/` から、または `wrangler r2 object get docunavi-backups/pg/<キー> --file <出力先> --remote`（**wrangler のこの使い方は未確認**）。
2. 実行する。`--expect` は、バックアップ直前の本番の件数（`documents` と `chunks` など）。`--shred` は、終わったらダンプを消す（macOS は `rm -P`、Linux は `shred`）:
   `./scripts/pg_restore_drill.sh <ダンプ> --expect documents=80,chunks=2313 --shred`
3. 終了コード 0 なら成功。1 は検査の失敗（必要な表〔`documents` `chunks` `user` `account`〕が無い・空、`embedding` の入っていない `chunks`、`--expect` と不一致）、2 は使い方・前提の不足。

スクリプトは、`prisma_postgres` の項目を一覧から外して `pg_restore` し（`--exit-on-error` は付けない）、`pg_restore` の終了コードと標準エラーの行数も出す。出力は表の名前と件数、成否だけ。
Prisma への戻し（本番 DB への復元）は、このスクリプトの範囲外で、未検証。

スクリプト自体の確認（2026-11-10、証拠等級 A）: 擬似のダンプ（4 表。`embedding` の欠けた行ありと無し）で、成功・`--shred`・件数の不一致・必要な表の欠落が、期待どおりの結果と終了コードになった。**本物のバックアップでの訓練は未実施。**

### 訓練の記録

| 日付 | ダンプのキー | 結果 | 備考 |
|---|---|---|---|
| 2026-11-08 | `pg/docunavi-20261108-1040*.dump` | OK（手作業。18 表すべての件数が一致） | スクリプト化の前 |

## 失敗の通知の作成（ユーザーがホストで。書き込み系）

```bash
as_me() { env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT "$@"; }
# 既存の通知先（docunavi ops）を使う。無ければ、deploy.md の「アラート」の手順で作る
as_me gcloud alpha monitoring channels list --project docunavi-app --format='table(name,displayName,type)'
CH=<上の一覧の docunavi ops の name>
# 同じ名前で 2 回作ると重複する。先に一覧で確かめる
as_me gcloud alpha monitoring policies list --project docunavi-app --format='table(name,displayName)'
for f in ops/monitoring/alert-pg-backup-failed.yaml ops/monitoring/alert-pg-backup-scheduler-failed.yaml; do
  as_me gcloud alpha monitoring policies create --project docunavi-app --policy-from-file="$f" --notification-channels="$CH"
done
```

作成の確認: 一覧に 2 つの方針が出る。通知が届くことは、失敗が起きるまで受動確認（わざと起こすと、本物のバックアップが失敗扱いになる）。

**作成済み（2026-11-11、ユーザーがホストで作成し、2 件とも `enabled=True`・通知先 1 件を確認〔証拠等級: ユーザーの報告〕）:**
`projects/docunavi-app/alertPolicies/8035046611342240567` と `projects/docunavi-app/alertPolicies/6567028124981041704`
（どちらが Job 用でどちらが Scheduler 用かは、報告に書かれていない。確かめるには `gcloud alpha monitoring policies describe <名前> --format='value(displayName)'`。閲覧専用の ID には Monitoring の権限が無いので、ユーザー自身の権限で）。
通知が実際に届くことは、まだ確認していない（失敗が起きるまで受動確認）。
