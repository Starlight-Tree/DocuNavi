# Cloud Run へのデプロイ（段階 0）

**この手順は未実行の案です。** コマンドの名前とフラグは `gcloud ... --help` で存在を確認しましたが、実際のデプロイ、
Vertex AI でのモデルの利用、Cloud Build での Docker ビルド、`agent-deployer` の権限の過不足は確かめていません
（Docker イメージのビルドと起動確認は CI が行っています）。

**資料の検索が DB のベクトル検索になったため（[search-design.md](search-design.md)）、本物の LLM のデプロイには `DATABASE_URL` が必須です**
（下の C。「任意」ではありません）。さらに、質問の埋め込みは API キーで呼ぶので、**A（Vertex AI）でも `GEMINI_API_KEY` の Secret が要ります**
（B の Secret の作り方）。A + ADC だけでは、起動の検査で失敗します。この組み合わせの実デプロイは未確認です。

## 誰の権限で実行するか

書き込み系のコマンド（API の有効化、サービスアカウントや権限の作成、デプロイ、公開）は、**あなたが実行します。**
開発コンテナの gcloud は、既定で閲覧専用の `agent-viewer` になりすましています（[.devcontainer/README.md](../.devcontainer/README.md)）。
書き込みは、コマンドごとに「誰の権限か」を明示します。

| 操作 | 使う ID | 書き方 |
|---|---|---|
| 読み取り（ログ、状態の確認） | `agent-viewer` | 何も付けない |
| 一度きりの準備、公開、削除 | あなた自身 | コマンドの先頭に `as_me`（下で定義） |
| デプロイ、ロールバック | `agent-deployer` | `--impersonate-service-account="$DEPLOYER"` |

## 方針

- **最初は認証必須（`--no-allow-unauthenticated`）で配備する。** 公開は、予算アラートと `--max-instances` を決めてから。
- **LLM の認証は Vertex AI（ADC）を第一候補にする。** キーを置かずに済みます。キー方式（B）は代替です。
- **実行用のサービスアカウントを専用に作る。** 既定の Compute Engine のアカウントは使わない。
- **デプロイ用のサービスアカウント（`agent-deployer`）にも、公開と API の有効化の権限は付けない。**

## 準備

ターミナルを開いたあと、一度だけ変数と関数を定義します。

```bash
PROJECT_ID=<プロジェクト ID>
REGION=asia-northeast1            # LLM の呼び出し先（GOOGLE_CLOUD_LOCATION）とは別。実際の値に読み替える
SERVICE=zahard-chatbot
RUN_SA=zahard-chatbot-run
DEPLOYER="agent-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

# 既定の「閲覧専用になりすます」環境変数を、この 1 コマンドだけ外して、あなた自身の権限で実行する
as_me() { env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT "$@"; }
```

0. **課金を有効にし、予算アラートを設定する**（Console の「お支払い」と「予算とアラート」）

   Cloud Run・Cloud Build・Artifact Registry・Vertex AI は、プロジェクトに課金アカウントが紐づいていないと有効にできません。
   2026-10-13 の時点で、このプロジェクトは課金が無効で、見える課金アカウントもありませんでした。確認は次のとおりです。

   ```bash
   as_me gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)'   # True なら紐づいている
   ```

   同じプロジェクトにある Gemini API キーの利用が、無料枠から有料枠に切り替わる可能性があります。公式の料金ページで確認してください。

1. **API を有効にする**（`aiplatform` は Vertex AI を使うとき、`secretmanager` はキー方式のとき）

   ```bash
   as_me gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
     aiplatform.googleapis.com --project "$PROJECT_ID"
   ```

   現状の有効な API は `as_me gcloud services list --enabled --project "$PROJECT_ID"` で確認できます
   （あなた自身の権限が要ります。閲覧専用の ID には、一覧を読む権限がありません）。

2. **実行用のサービスアカウントを作る**

   ```bash
   as_me gcloud iam service-accounts create "$RUN_SA" --display-name="Zahard chatbot runtime" --project "$PROJECT_ID"
   as_me gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role=roles/aiplatform.user --condition=None
   ```

3. **デプロイ用のサービスアカウントを作る**（内容を表示して確認を取ります。スクリプトの中で本人の権限に切り替わります）

   ```bash
   ./scripts/setup-agent-deployer.sh "$PROJECT_ID"
   ```

   付与するのは `roles/run.sourceDeveloper`（ソースからのデプロイ）、`roles/logging.viewer`（ビルドログ）、
   実行用 SA への `roles/iam.serviceAccountUser`、あなたへの `roles/iam.serviceAccountTokenCreator` です。
   これは出発点です。デプロイで権限エラーが出たら、エラーに出た権限を足します。特に、ビルドに使われる
   サービスアカウントに対する `iam.serviceAccountUser` が要ることがあります。

4. **アップロードされるファイルを確認する**（読み取りだけ。`--source .` はソースを Cloud Build に送ります）

   ```bash
   gcloud meta list-files-for-upload . | grep -E '(^|/)\.env$|gcloud-token|node_modules'   # 何も出なければ OK
   ```

   `.env` と `.gcloud-token*` は `.gitignore` と `.dockerignore` に入っています。

## デプロイ

### A. Vertex AI（推奨）

```bash
gcloud run deploy "$SERVICE" \
  --impersonate-service-account="$DEPLOYER" \
  --project "$PROJECT_ID" --region "$REGION" \
  --source . \
  --no-allow-unauthenticated \
  --service-account "${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --max-instances 2 \
  --concurrency 20 \
  --timeout 90 \
  --set-env-vars "GOOGLE_GENAI_USE_VERTEXAI=true,GOOGLE_CLOUD_PROJECT=${PROJECT_ID},GOOGLE_CLOUD_LOCATION=global"
```

### B. Gemini API キー（代替）

キーは **Secret Manager から渡します。** `--set-env-vars` に直接書くと、サービスの設定を読める人
（`roles/run.viewer` を持つ `agent-viewer` を含む）に値が見えてしまいます。

```bash
as_me gcloud services enable secretmanager.googleapis.com --project "$PROJECT_ID"
read -rsp 'GEMINI_API_KEY: ' GEMINI_API_KEY; echo      # 入力は画面にもシェルの履歴にも残らない
printf '%s' "$GEMINI_API_KEY" | as_me gcloud secrets create gemini-api-key --data-file=- --project "$PROJECT_ID"
unset GEMINI_API_KEY
as_me gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID"
```

A のコマンドの `--set-env-vars` を、次の 1 行に置き換えます。

```bash
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest"
```

### C. DB に接続する（本物の LLM では必須）

`DATABASE_URL` は、資料の検索（DB のベクトル検索）と会話履歴の保存先を兼ねます。無いと、本物の LLM では起動に失敗します（[configuration.md](configuration.md) の「資料の検索」「会話履歴の保存先」）。
接続 URL はパスワードを含むので、キーと同じく **Secret Manager から渡します。**

```bash
read -rsp 'DATABASE_URL: ' DATABASE_URL; echo
printf '%s' "$DATABASE_URL" | as_me gcloud secrets create database-url --data-file=- --project "$PROJECT_ID"
unset DATABASE_URL
as_me gcloud secrets add-iam-policy-binding database-url \
  --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID"
```

デプロイのコマンドに、次を足します（A では `--set-env-vars` と並べて、B では `--set-secrets` の値をカンマでつなぎます）。

```bash
  --set-secrets "DATABASE_URL=database-url:latest"
```

- `--max-instances` × `DATABASE_POOL_MAX_SIZE`（既定 5）が、DB の接続数の上限に収まるようにします（2 インスタンスなら 10 本）。
- DB に届かないと、**起動に失敗してリビジョンが Ready になりません**（メモリへは切り替わりません）。ログで原因を確かめます。
- Cloud Run から Supabase への接続は未確認です（開発コンテナからは、プーラーの URL で届くことを確かめました。2026-10-14）。
  直接接続は既定で IPv6 のため、IPv4 で届くプーラーの接続 URL を使う想定です。
  届くかどうかは、デプロイして `/health` の `history` が `postgres` になるかで確かめます。
- 会話履歴の保存期間は 90 日という方針ですが、**削除の定期実行は未設定なので、いまは保証していません**（`docs/configuration.md`）。定期実行の手順は、次の D です（未実施）。

### D. 会話履歴の 90 日削除（Cloud Run Job + Cloud Scheduler）

**この節は手順の案で、未実行です。** 実行するのは、B・C まで済んで、サービスが `/health` の `history` を `postgres` にして動いてから
（Cloud Run が始まる前は、Job も Scheduler も作れません。請求先アカウントと、上の準備 0・1 が前提です）。
Job と Scheduler の作成は書き込みなので、あなたが実行します（`as_me`）。フラグは `gcloud ... --help` で存在を確かめましたが、実行はしていません。

- 削除の実体は `python -m app.history_admin --older-than-days 90 --apply`（[configuration.md](configuration.md)）。同じ条件で何度実行しても、
  消える会話は「90 日以上動きのない会話」だけなので、再実行されても害はありません（一覧を取ってから消すまでに新しい発言が入った会話が消えうる点だけは、`history_admin` の説明のとおり）。
- 削除のために HTTP ルートは作りません（認証が無いため）。Job はアプリのイメージを使い、コマンドだけを上書きします。
- Job は `DATABASE_URL` の Secret（C の `database-url`）だけを渡します。LLM のキーは要りません。Cloud Run から DB に届くことは、C の確認（`/health`）が先です。

```bash
# 1. Cloud Scheduler の API を有効にする（初回だけ）
as_me gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID"

# 2. サービスが今使っているイメージを調べる（読み取り）
IMAGE=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --format='value(spec.template.spec.containers[0].image)')

# 3. Job を作る（コマンドを上書き。実行用 SA と Secret はサービスと同じ）
JOB=zahard-chatbot-history-cleanup
as_me gcloud run jobs create "$JOB" \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-secrets "DATABASE_URL=database-url:latest" \
  --command python \
  --args=-m,app.history_admin,--older-than-days,90 \
  --max-retries 1 --task-timeout 300
```

3 では、わざと `--apply` を付けていません（件数を表示するだけの Job）。最初の 1 回は手動で実行し、ログで件数を見てから `--apply` を足します。

```bash
# 4. 手動で 1 回実行する（件数を表示するだけ。--wait で終わるまで待つ）
as_me gcloud run jobs execute "$JOB" --project "$PROJECT_ID" --region "$REGION" --wait
#    ログ: 「対象の会話: N 件（削除はしていません。…）」。閲覧専用の ID でログを読めるかは未確認（読めなければ `as_me` を付ける）
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=${JOB}" \
  --project "$PROJECT_ID" --limit 10 --format='value(textPayload)'

# 5. 件数が想定どおりなら、--apply を足す
as_me gcloud run jobs update "$JOB" --project "$PROJECT_ID" --region "$REGION" \
  --args=-m,app.history_admin,--older-than-days,90,--apply

# 6. Scheduler 用の専用サービスアカウントを作り、この Job を呼ぶ権限だけを付ける
SCHED_SA=history-cleanup-scheduler
as_me gcloud iam service-accounts create "$SCHED_SA" --display-name="History cleanup scheduler" --project "$PROJECT_ID"
as_me gcloud run jobs add-iam-policy-binding "$JOB" --project "$PROJECT_ID" --region "$REGION" \
  --member="serviceAccount:${SCHED_SA}@${PROJECT_ID}.iam.gserviceaccount.com" --role=roles/run.invoker

# 7. 週 1 回（毎週日曜 3:00 JST）に Job を実行する Scheduler を作る
as_me gcloud scheduler jobs create http "${JOB}-weekly" \
  --project "$PROJECT_ID" --location "$REGION" \
  --schedule "0 3 * * 0" --time-zone "Asia/Tokyo" \
  --http-method POST \
  --uri "https://run.googleapis.com/v2/projects/${PROJECT_ID}/locations/${REGION}/jobs/${JOB}:run" \
  --oauth-service-account-email "${SCHED_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --max-retry-attempts 1
```

- **動いたかの確認:** 7 の後、Scheduler の画面（または `as_me gcloud scheduler jobs run "${JOB}-weekly" --location "$REGION"`）で 1 回起こし、
  上の `logging read` で「削除した会話: N 件」を見ます。これで初めて「定期実行が動いた」と言えます。それまでは、動くと確かめていません。
- **イメージの更新:** Job のイメージは作った時点のものです。サービスをデプロイし直したら、`as_me gcloud run jobs update "$JOB" --image "$IMAGE" …` で合わせます
  （古いイメージでも、`history_admin` が変わらなければ動きます）。
- **未確認:** この Job の実行（イメージに `python` と `history_admin` が入って動くこと）、Cloud Run から Supabase への接続、Scheduler から Job を呼ぶ権限の過不足、
  `--args` の書き方での `--apply` の追加。`history_admin` の `--apply` は、Supabase（実 DB）では `--thread-id` でだけ確かめています。
- **失敗に気づく:** 失敗しても通知は出ません（Cloud Monitoring のアラートは未設定）。当面は、月に 1 度、`logging read` で最後の実行を見ます。

### 値の根拠

| フラグ | 値 | 理由 |
|---|---|---|
| `--max-instances` | 2 | 費用の上限。足りなくなったら、予算と相談して上げる |
| `--concurrency` | 20 | `20 × LLM_MAX_ATTEMPTS（3）= 60` が SDK の接続プール（既定 100）に収まる。既定の 80 だと 240 になる |
| `--timeout` | 90 | `LLM_TIMEOUT_SECONDS`（60）より長くする |

初回は、Artifact Registry の Docker リポジトリ（`cloud-run-source-deploy`）の作成を聞かれます。

## 確かめる

認証必須なので、`gcloud run services proxy` でローカルに橋渡しします。呼び出しの権限（`roles/run.invoker`）は
閲覧専用の ID に無いため、あなた自身の権限（`as_me`）で実行します。

```bash
as_me gcloud run services proxy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --port 8080
# 別のターミナルで
curl -s localhost:8080/health          # {"status":"ok","mode":"gemini","history":"memory"} なら本物の LLM に繋がっている
curl -s localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"ザーハードの大気について教えて"}'
```

- `mode` が `mock` なら、認証の設定が届いていません。
- `history` が `memory` なら、`DATABASE_URL` が届いていません（C を使う場合）。
- 500 は、モデル名やロケーション、権限の誤りの可能性が高いです。Vertex AI はリージョンごとに提供モデルが違うため、
  `GEMINI_MODEL` と `GOOGLE_CLOUD_LOCATION` を確認してください（[configuration.md](configuration.md)）。
- ログは `gcloud run services logs read "$SERVICE" --project "$PROJECT_ID" --region "$REGION"`
  （読み取りなので、閲覧専用の ID で動き、エージェントも実行できます）。

## 公開する・戻す

- **公開する**（予算アラートと `--max-instances` を決めてから）。`agent-deployer` には公開の権限が無いので、あなた自身の権限で行います。
  組織のポリシーで `allUsers` が禁止されていることがあります。

  ```bash
  as_me gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --member=allUsers --role=roles/run.invoker
  ```

- **前のリビジョンに戻す:**

  ```bash
  gcloud run services update-traffic "$SERVICE" --impersonate-service-account="$DEPLOYER" \
    --project "$PROJECT_ID" --region "$REGION" --to-revisions=<リビジョン名>=100
  ```

- **止める（削除）:** `as_me gcloud run services delete "$SERVICE" --project "$PROJECT_ID" --region "$REGION"`

## 続き

次の段階（DB）は [roadmap.md](roadmap.md) を参照してください。
