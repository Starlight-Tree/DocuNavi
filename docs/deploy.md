# Cloud Run へのデプロイ（段階 0）

**この手順は未実行の案です。** コマンドの名前とフラグは `gcloud ... --help` で存在を確認しましたが、実際のデプロイ、
Vertex AI でのモデルの利用、Cloud Build での Docker ビルドは確かめていません（Docker イメージのビルドと起動確認は CI が行っています）。

書き込み系のコマンド（API の有効化、サービスアカウントや権限の作成、デプロイ）は、**あなたが実行します。**
開発コンテナの gcloud は閲覧専用です（[.devcontainer/README.md](../.devcontainer/README.md)）。ホストか Cloud Shell で実行してください。

## 方針

- **最初は認証必須（`--no-allow-unauthenticated`）で配備する。** 公開は、予算アラートと `--max-instances` を決めてから。
- **LLM の認証は Vertex AI（ADC）を第一候補にする。** キーを置かずに済みます。キー方式（B）は代替です。
- **実行用のサービスアカウントを専用に作る。** 既定の Compute Engine のアカウントは使わない。

## 準備

```bash
PROJECT_ID=<プロジェクト ID>
REGION=asia-northeast1            # LLM の呼び出し先（GOOGLE_CLOUD_LOCATION）とは別。実際の値に読み替える
SERVICE=zahard-chatbot
RUN_SA=zahard-chatbot-run
```

1. **API を有効にする**（`aiplatform` は Vertex AI を使うとき、`secretmanager` はキー方式のとき）

   ```bash
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
     aiplatform.googleapis.com --project "$PROJECT_ID"
   ```

   2026-10 の時点で、このプロジェクトで有効にしてあるのは Gemini API だけでした。現状は
   `gcloud services list --enabled --project "$PROJECT_ID"`（読み取り）で確認できます。

2. **実行用のサービスアカウントを作る**

   ```bash
   gcloud iam service-accounts create "$RUN_SA" --display-name="Zahard chatbot runtime" --project "$PROJECT_ID"
   gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role=roles/aiplatform.user --condition=None
   ```

3. **アップロードされるファイルを確認する**（読み取りだけ。`--source .` はソースを Cloud Build に送ります）

   ```bash
   gcloud meta list-files-for-upload . | grep -E '(^|/)\.env$|gcloud-token|node_modules'   # 何も出なければ OK
   ```

   `.env` と `.gcloud-token*` は `.gitignore` と `.dockerignore` に入っています。

## デプロイ

### A. Vertex AI（推奨）

```bash
gcloud run deploy "$SERVICE" \
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
gcloud services enable secretmanager.googleapis.com --project "$PROJECT_ID"
read -rsp 'GEMINI_API_KEY: ' GEMINI_API_KEY; echo      # 入力は画面にもシェルの履歴にも残らない
printf '%s' "$GEMINI_API_KEY" | gcloud secrets create gemini-api-key --data-file=- --project "$PROJECT_ID"
unset GEMINI_API_KEY
gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID"
```

A のコマンドの `--set-env-vars` を、次の 1 行に置き換えます。

```bash
  --set-secrets "GEMINI_API_KEY=gemini-api-key:latest"
```

### 値の根拠

| フラグ | 値 | 理由 |
|---|---|---|
| `--max-instances` | 2 | 費用の上限。足りなくなったら、予算と相談して上げる |
| `--concurrency` | 20 | `20 × LLM_MAX_ATTEMPTS（3）= 60` が SDK の接続プール（既定 100）に収まる。既定の 80 だと 240 になる |
| `--timeout` | 90 | `LLM_TIMEOUT_SECONDS`（60）より長くする |

初回は、Artifact Registry の Docker リポジトリ（`cloud-run-source-deploy`）の作成を聞かれます。

## 確かめる

認証必須なので、`gcloud run services proxy` でローカルに橋渡しします（あなたのアカウントの資格情報が付きます）。

```bash
gcloud run services proxy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --port 8080
# 別のターミナルで
curl -s localhost:8080/health          # {"status":"ok","mode":"gemini"} なら本物の LLM に繋がっている
curl -s localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"ザーハードの大気について教えて"}'
```

- `mode` が `mock` なら、認証の設定が届いていません。
- 500 は、モデル名やロケーション、権限の誤りの可能性が高いです。Vertex AI はリージョンごとに提供モデルが違うため、
  `GEMINI_MODEL` と `GOOGLE_CLOUD_LOCATION` を確認してください（[configuration.md](configuration.md)）。
- ログは `gcloud run services logs read "$SERVICE" --project "$PROJECT_ID" --region "$REGION"`（読み取りなので、エージェントも実行できます）。

## 公開する・戻す

- **公開する**（予算アラートと `--max-instances` を決めてから）。組織のポリシーで `allUsers` が禁止されていることがあります。

  ```bash
  gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --member=allUsers --role=roles/run.invoker
  ```

- **前のリビジョンに戻す:**

  ```bash
  gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
    --to-revisions=<リビジョン名>=100
  ```

- **止める（削除）:** `gcloud run services delete "$SERVICE" --project "$PROJECT_ID" --region "$REGION"`

## 続き

次の段階（DB）は [roadmap.md](roadmap.md) を参照してください。
