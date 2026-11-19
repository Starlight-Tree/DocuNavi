# Cloud Run へのデプロイ

**本番環境で実行済みです（2026-10-20 に初回公開）。** Cloud Run のサービス配備・公開、Cloud Build でのビルド、Secret Manager とサービスアカウントの連携、バッチ Job と予算ガードが本番で稼働しています。
日々の配備は、タグ push（下の「タグ push で 0% 配備する」）と `scripts/preview.sh`（「タグ付きプレビュー」）で行い、最初の構築の手順（準備・A〜G）は、新しい環境を作るときの参照です。
この文書の対象は Cloud Run（API・Job）です。画面（Worker `docunavi-web`）と認証の Worker `docunavi-api` は、手動の `.github/workflows/deploy.yml` で出します（末尾の「GitHub Actions のデプロイ用の秘密」、[architecture.md](architecture.md)）。

- **本物の LLM のデプロイに要るもの:** `DB_BACKEND=pg` と `DATABASE_URL`（C）、`GEMINI_API_KEY`（B。LLM はキーで呼ぶ）。埋め込み（取り込みと質問）は、`EMBEDDING_USE_VERTEXAI=true` なら Vertex AI（ADC）で呼び、その場合は埋め込みにキーは要らない（[configuration.md](configuration.md) の「埋め込みを Vertex AI で呼ぶ」）。
  **本番の Cloud Run サービスと取り込み Job 2 本（`docunavi-ingest-sync` `docunavi-ingest-queue`）は、`EMBEDDING_USE_VERTEXAI=true` です**（2026-11-20 に gcloud の読み取りで確認、証拠等級 A。確認できたのは設定だけで、切り替えた日付と、実際の呼び出しが Vertex AI で通っているかは未確認）。
  一方、本番の Cloud Run サービスには `GOOGLE_GENAI_USE_VERTEXAI` の設定が無く（2026-10-31 に確認。2026-11-20 に、配信中のリビジョン `docunavi-00091-juj` で再確認）、LLM は B（キー）の経路です。
- **方針:** GCP の操作は原則 gcloud CLI で行います（Cloud Code など GCP 用の VS Code 拡張は入れない）。画像生成の Cloud Run Job は星庭ジョブ（`HoshiniwaJob`）のリポジトリで管理し、このリポジトリには置きません。

## 誰の権限で実行するか

書き込み系のコマンド（API の有効化、サービスアカウントや権限の作成、デプロイ、公開）は、**あなたが実行します。** 開発コンテナの gcloud は、既定で閲覧専用の `docunavi-viewer`（運用用プロジェクト `docunavi-ops` のサービスアカウント）になりすましています（[.devcontainer/README.md](../.devcontainer/README.md)）。書き込みは、コマンドごとに「誰の権限か」を明示します。

| 操作 | 使う ID | 書き方 |
|---|---|---|
| 読み取り（ログ、状態の確認） | `docunavi-viewer` | 何も付けない。Cloud Build の閲覧だけ `--billing-project=docunavi-app` を付ける（付けないと API が `docunavi-ops` に数えられて失敗する） |
| 一度きりの準備、公開、削除 | あなた自身 | コマンドの先頭に `as_me`（下で定義） |
| デプロイ、ロールバック | `agent-deployer` | `--impersonate-service-account="$DEPLOYER"` |

## 方針

- **最初は認証必須（`--no-allow-unauthenticated`）で配備する。** 公開は、予算アラートと `--max-instances` を決めてから。
- **LLM の認証は、本番ではキー方式（B）で動いています**（2026-11-20 に確認。上）。Vertex AI（ADC。A）は、キーを置かずに済むので最初は第一候補にしていましたが、LLM を Vertex に切り替えるかは別の決定で、事前に確認します（Vertex の LLM は 2026-11-09 の単発 5 回で、Developer API の 2〜3 倍遅かった。[.claude/rules/dev-environment.md](../.claude/rules/dev-environment.md)）。埋め込みだけは Vertex AI を呼ぶ設定です（上。実際の呼び出しが通っているかは未確認）。
- **実行用のサービスアカウントを専用に作る。** 既定の Compute Engine のアカウントは使わない。
- **デプロイ用のサービスアカウント（`agent-deployer`）には、公開と API の有効化の権限は付けない。**

## 準備

ターミナルを開いたあと、一度だけ変数と関数を定義します。

```bash
PROJECT_ID=<プロジェクト ID>
REGION=asia-northeast1            # LLM の呼び出し先（GOOGLE_CLOUD_LOCATION）とは別
SERVICE=docunavi
RUN_SA=docunavi-run
DEPLOYER="agent-deployer@${PROJECT_ID}.iam.gserviceaccount.com"

# 既定の「閲覧専用になりすます」環境変数を、この 1 コマンドだけ外して、あなた自身の権限で実行する
as_me() { env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT "$@"; }
```

0. **課金を有効にし、予算アラートを設定する**（Console の「お支払い」と「予算とアラート」）。Cloud Run・Cloud Build・Artifact Registry・Vertex AI は、課金アカウントが紐づいていないと有効にできません。確認:
   `as_me gcloud billing projects describe "$PROJECT_ID" --format='value(billingEnabled)'`（True なら紐づいている）。同じプロジェクトにある Gemini API キーの利用が、無料枠から有料枠に切り替わる可能性があります（公式の料金ページで確認）。
1. **API を有効にする**（`aiplatform` は Vertex AI を使うとき〔本番は埋め込みで使う設定〕、`secretmanager` はキー方式のとき）:
   `as_me gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com aiplatform.googleapis.com --project "$PROJECT_ID"`。
   有効な API の一覧は `as_me gcloud services list --enabled --project "$PROJECT_ID"`（閲覧専用の ID には、一覧を読む権限がありません）。
   - Vertex AI を開発コンテナの Python（ADC）から使うときは、gcloud のなりすましの設定は ADC に効かず、ADC を作った認証（利用者本人）で動きます。ADC を作ると本人のリフレッシュトークンがボリュームに残るので、テストが終わったら残すか消すか（`gcloud auth application-default revoke`。利用者が実行）を決めます。
   - **未確認:** `docunavi-run` に `roles/aiplatform.user` が付いているか（本番のサービスは Vertex の埋め込みを使う設定なので、要る権限。閲覧専用の ID は IAM ポリシーを読めない。次のコマンドを利用者の権限で実行して確かめる）。
     `as_me gcloud projects get-iam-policy docunavi-app --flatten=bindings --filter='bindings.members:serviceAccount:docunavi-run@docunavi-app.iam.gserviceaccount.com' --format='value(bindings.role)'`
2. **実行用のサービスアカウントを作る:**

   ```bash
   as_me gcloud iam service-accounts create "$RUN_SA" --display-name="DocuNavi runtime" --project "$PROJECT_ID"
   as_me gcloud projects add-iam-policy-binding "$PROJECT_ID" \
     --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --role=roles/aiplatform.user --condition=None
   ```

3. **デプロイ用のサービスアカウントを作る**（内容を表示して確認を取る。スクリプトの中で本人の権限に切り替わる）: `./scripts/setup-agent-deployer.sh "$PROJECT_ID"`。
   付与するのは `roles/run.sourceDeveloper`、`roles/logging.viewer`、実行用 SA への `roles/iam.serviceAccountUser`、あなたへの `roles/iam.serviceAccountTokenCreator` です。出発点なので、権限エラーが出たら、エラーに出た権限を足します（ビルドに使われるサービスアカウントへの `iam.serviceAccountUser` が要ることがある）。
4. **アップロードされるファイルを確認する**（読み取りだけ）: `gcloud meta list-files-for-upload . | grep -E '(^|/)\.env$|gcloud-token|node_modules'`（何も出なければ OK）。`.env` と `.gcloud-token*` は `.gitignore` と `.dockerignore` に入っています。

## デプロイ

### A. Vertex AI（推奨）

**2026-11-20 の注記:** 見出しの「推奨」は最初の構築のときの方針で、現状とは違います。本番の LLM は B（キー）で動き、A は使っていません（上の「方針」）。A のコマンドは、B のデプロイの土台としても使います（B は `--set-env-vars` の行を差し替える）。

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

| フラグ | 値 | 理由 |
|---|---|---|
| `--max-instances` | 2 | 費用の上限。足りなくなったら、予算と相談して上げる |
| `--concurrency` | 20 | `20 × LLM_MAX_ATTEMPTS（3）= 60` が SDK の接続プール（既定 100）に収まる。既定の 80 だと 240 になる |
| `--timeout` | 90 | `LLM_TIMEOUT_SECONDS`（60）より長くする |

### B. Gemini API キー（代替。本番はこちら）

キーは **Secret Manager から渡します。** `--set-env-vars` に直接書くと、サービスの設定を読める人（`docunavi-viewer` を含む）に値が見えてしまいます。

```bash
as_me gcloud services enable secretmanager.googleapis.com --project "$PROJECT_ID"
read -rsp 'GEMINI_API_KEY: ' GEMINI_API_KEY; echo      # 入力は画面にもシェルの履歴にも残らない
printf '%s' "$GEMINI_API_KEY" | as_me gcloud secrets create gemini-api-key --data-file=- --project "$PROJECT_ID"
unset GEMINI_API_KEY
as_me gcloud secrets add-iam-policy-binding gemini-api-key \
  --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID"
```

A のコマンドの `--set-env-vars` を、`--set-secrets "GEMINI_API_KEY=gemini-api-key:latest"` に置き換えます。

**無料キーが 429 のときだけ有料キーを使う（任意。[configuration.md](configuration.md) の「無料キーが 429 のとき」）。** 予算ガード付きのプロジェクトに作ったキーを、別の Secret `gemini-api-key-paid`（環境変数 `GEMINI_API_KEY_PAID`）に入れます。足さなければ、無料キーだけで動きます。
キーの作成では、gcloud が結果（キー本体を含む）を stderr に出すので、`2>/dev/null` で捨てます（付け忘れると画面とログに出る）。

```bash
as_me gcloud services enable apikeys.googleapis.com generativelanguage.googleapis.com --project "$PROJECT_ID"
K=$(as_me gcloud services api-keys create --project "$PROJECT_ID" --display-name=docunavi-paid \
      --api-target=service=generativelanguage.googleapis.com \
      --format='value(response.keyString)' 2>/dev/null | tr -d '\n')
echo "length: ${#K}"        # 39 前後を確かめる。値は表示しない
printf '%s' "$K" | as_me gcloud secrets create gemini-api-key-paid --data-file=- --project "$PROJECT_ID"
unset K
as_me gcloud secrets add-iam-policy-binding gemini-api-key-paid \
  --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID"
as_me gcloud run services update "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --update-secrets "GEMINI_API_KEY_PAID=gemini-api-key-paid:latest"
```

- **`--set-secrets` ではなく `--update-secrets` を使う。** `--set-secrets` は既存の Secret を置き換えるので、`DATABASE_URL` などが外れて起動に失敗する。足したあとは、`gcloud run services describe` で既存の Secret が残っていることを確かめる。
- AI Studio の一覧に出すには、プロジェクトのインポートが要ります（`docunavi-app` を選ぶ。`docunavi-ops` は取り込まない）。
- **有料の階層（Tier 1）にするには、AI Studio で最低 $5 の前払いが要ります**（画面だけで行え、CLI・API では不可。クレジットは購入から 12 か月で失効し、返金されない。自動チャージは設定しない）。Google Cloud の無料クレジットは、2026-03-02 以降のアカウントでは Gemini API に使えません（公式の請求ドキュメント）。
  **前払いの残高が尽きると、有料キーは 402 を返します。** 切り替えの条件は 429 だけなので、その場合は切り替わらず、無料キーの 429 がそのまま返ります。
- 2026-10-20: `docunavi-app` が Tier 1 と表示され、前払い ¥800 を購入しました（利用者の報告）。有料キーでの実 API の呼び出しは確認済みです（HTTP 200）。**未確認:** 本物の 429 での切り替えと 60 秒の保持。

### C. データベースの接続（本物の LLM では必須）

本番は Prisma Postgres で、`DB_BACKEND=pg` と `DATABASE_URL`（Secret `database-url`。**プール側の接続文字列**）を渡します（[configuration.md](configuration.md) の「資料の検索」「会話履歴の保存先」）。Secret の値は、キーと同じく Secret Manager から渡します。無いと、本物の LLM では起動に失敗します（メモリへは切り替わりません）。
新しい実行用サービスアカウントで使うときは、読み取りを許します。

```bash
as_me gcloud secrets add-iam-policy-binding database-url \
  --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID"
```

デプロイのコマンドに `--set-env-vars` で `DB_BACKEND=pg`、`--set-secrets` で `DATABASE_URL=database-url:latest` を足します。

- 接続文字列の貼り付けで先頭の 1 文字が欠けると（`ostgresql://…`）、プールは接続を試し続けて時間切れになるので、ネットワークの問題に見えます（初回公開のときに起きた。起動に失敗し `PoolTimeout`）。Secret に入れる前に、先頭が `postgresql://` であることを確かめます。値を見るときは、パスワードを伏せます: `gcloud secrets versions access latest --secret database-url --project "$PROJECT_ID" | sed -E 's#(://[^:@/]+):[^@]*@#\1:***@#; s#\?.*#?…#'`。
- 接続数の上限は [configuration.md](configuration.md) の `DATABASE_POOL_MAX_SIZE`。

### D. 会話履歴の削除（Cloud Run Job + Cloud Scheduler）

最後の動きから **7 日**（2026-11-09 に利用者が決定。最初は 2026-10-20 に 7 日で始め、方針の 90 日は取りやめた）たった会話を削除します（戻せません）。実行は週 1 回なので、実際に消えるのは 7〜14 日後です。実体は `python -m app.history_admin --older-than-days 7 --apply`（[configuration.md](configuration.md)）。
削除のために HTTP ルートは作りません（認証が無いため）。Job はアプリのイメージを使い、コマンドだけを上書きします。Job は `DB_BACKEND` と `DATABASE_URL`（C の Secret）だけを渡し、LLM のキーは要りません。

**現状（2026-11-15）:** Job `docunavi-history-cleanup`（`--older-than-days 7 --apply`）、Scheduler `docunavi-history-cleanup-weekly`（毎週日曜 03:00 JST）。**`--apply` は 2026-11-15 に戻しました（利用者の承認）。最初に消えうるのは 2026-11-22 03:00 JST の実行で、初回のログと DELETE 権限は未確認です**（「保留中の運用の準備」の 4）。
**2026-11-20 の訂正（gcloud のログの読み取り、証拠等級 A）:** 戻した日の 03:00 JST の定期実行（実行 `docunavi-history-cleanup-vx5ln`）は、すでに `--apply` つきで動いていました。ログは「削除した会話: 0 件」、終了コード 0 です。対象が 0 件で DELETE は発行されていない（`app/history_admin.py`）ので、DELETE 権限は未確認のままで、会話が削除された実績はまだありません。次の定期実行は、起動のログと文書の cron からの予定で 2026-11-22 03:00 JST です（Scheduler の設定は閲覧用の権限では読めない）。

```bash
# 1. Cloud Scheduler の API を有効にする（初回だけ）
as_me gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID"

# 2. サービスが今使っているイメージを調べる（読み取り）
IMAGE=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --format='value(spec.template.spec.containers[0].image)')

# 3. Job を作る（わざと --apply を付けない。件数を表示するだけの Job）
JOB=docunavi-history-cleanup
as_me gcloud run jobs create "$JOB" \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "DB_BACKEND=pg" \
  --set-secrets "DATABASE_URL=database-url:latest" \
  --command python \
  --args=-m,app.history_admin,--older-than-days,7 \
  --max-retries 1 --task-timeout 300

# 4. 手動で 1 回実行する。ログ: 「対象の会話: N 件（削除はしていません。…）」
as_me gcloud run jobs execute "$JOB" --project "$PROJECT_ID" --region "$REGION" --wait
gcloud logging read "resource.type=cloud_run_job AND resource.labels.job_name=${JOB}" \
  --project "$PROJECT_ID" --limit 10 --format='value(textPayload)'

# 5. 件数が想定どおりなら、--apply を足す
as_me gcloud run jobs update "$JOB" --project "$PROJECT_ID" --region "$REGION" \
  --args=-m,app.history_admin,--older-than-days,7,--apply

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

- **詰まりやすい点:** サービスアカウントを作った直後の `run jobs add-iam-policy-binding` と `scheduler jobs create` は、IAM の反映待ちで「存在しない」「見つからない」と失敗することがあります（数十秒〜数分後にやり直して通った）。ログは、実行の完了から数十秒遅れて読めるようになります（空なら `run jobs executions list` で完了を見る）。
- **動いたかの確認:** Scheduler を 1 回起こし（`as_me gcloud scheduler jobs run "${JOB}-weekly" --location "$REGION"`）、`run jobs executions list` の「RUN BY」が Scheduler 用のアカウントで完了していることを見ます（2026-10-20 に確認済み）。
- **イメージの更新:** Job のイメージは作った時点のものです。サービスをデプロイし直したら、`as_me gcloud run jobs update "$JOB" --image "$IMAGE" …` で合わせます。**イメージは Artifact Registry のクリーンアップポリシーで消えうる**ので、`pin-` タグの付け替えが要ります（「古いリビジョンとイメージの整理」）。
- **失敗に気づく:** 失敗しても通知は出ません（アラートは未設定）。当面は、月に 1 度、`logging read` で最後の実行を見ます。
- **未確認:** 実際に会話が削除される動き（`--apply` で消えた実績はまだ無い）。

### E. 公開前の守り

`--no-allow-unauthenticated` は Cloud Run の入口の認証（IAM）です。公開すると（`allUsers` に `roles/run.invoker`）、これが外れるので、**アプリ側のログイン（`AUTH_MODE=required`）と、費用の上限を先に揃えます。** 実行済みです（2026-10-20）。

| 守り | 設定 | 備考 |
|---|---|---|
| 費用の上限 | `--max-instances 2`（A に入っている） | |
| 予算アラート | 下の `budgets create` | 通知だけで、使用は止まらない。止めるのは G |
| ログイン | `AUTH_MODE=required` と `AUTH_BASE_URL`・`AUTH_AUDIENCE`（[configuration.md](configuration.md) の「ログイン（認証）」） | 起動時に JWKS を取得する。取れなければリビジョンが Ready にならない |
| CORS | `CORS_ORIGINS` | 画面（Worker `docunavi-web`）のオリジンだけにする。`*` のままにしない（画面と API は別オリジン） |

```bash
# 予算アラート（請求先アカウントの管理者の権限。as_me で実行）。金額としきい値は例
as_me gcloud billing budgets create --billing-account=<請求先アカウント ID> \
  --display-name="docunavi" --budget-amount=<金額。請求先アカウントの通貨で> \
  --filter-projects="projects/${PROJECT_ID}" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

デプロイのコマンドの `--set-env-vars` に足す: `AUTH_MODE=required,AUTH_BASE_URL=<画面（Worker `docunavi-web`）のオリジン>,AUTH_AUDIENCE=docunavi,CORS_ORIGINS=<同じオリジン>`（どちらも公開してよい値）。
画面のログインは、画面側（Worker のビルドで `NEXT_PUBLIC_AUTH_ENABLED=1`）で有効にします。Cloud Run のイメージに画面の設定は入りません。画面にログイン設定が無いと、`AUTH_MODE=required` では質問できません（`/chat/stream` が 401）。トークンの期限切れ後の更新は未確認です。

### F. イメージのビルド（`cloudbuild.yaml`）

イメージは API だけです（画面は Worker `docunavi-web` が配信するので、ビルド引数も画面の設定もありません）。`--source .` でも同じイメージができますが、Artifact Registry に置くイメージの名前（コミットの短縮 SHA）を決めるために、`cloudbuild.yaml` を使います（`scripts/preview.sh build` も同じ）。
開発コンテナには Docker が無く、`docker build` はローカルでは実行していません。`cloudbuild.yaml` の内容は `tests/test_cloudbuild.py` が確かめます。

```bash
REPO=cloud-run-source-deploy      # 初回の --source 配備で作られるリポジトリ。無ければ下で作る
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(git rev-parse --short HEAD)"

# 初回だけ: Artifact Registry のリポジトリを作る（書き込み。as_me）
as_me gcloud artifacts repositories create "$REPO" --repository-format=docker \
  --location "$REGION" --project "$PROJECT_ID"

# ビルドして push（書き込み。as_me）
as_me gcloud builds submit . --project "$PROJECT_ID" --config cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE}"
```

配備は、A のコマンドの `--source .` を `--image "$IMAGE"` に置き換えます。イメージのタグはコミットごとに変えます（`:latest` にしない。ロールバックで前のイメージを指定できるように）。`.dockerignore` が `.env` を除いているので、ビルドコンテキストに秘密は入りません。

### G. 予算の自動停止（課金を紐づけるときに、デプロイより先に）

実行済みです（2026-10-20）。関数は `ops/budget_guard/main.py`。仕組みは、予算 → Pub/Sub → 関数 → プロジェクトの課金を無効化、という公式の方法です。
**「無料枠を超えそうになったら止める」ことはできません。** 予算は金額で、Gemini の API キーは同じプロジェクトにあり、課金を紐づけると有料枠に切り替わる可能性があります。実際は「月の費用が予算額に届いたら止める」です。

**注意**

- 課金を無効にすると、**プロジェクトのサービスがすべて止まり、リソースは消えることがあります**（公式の警告）。復旧は手動で、元に戻る保証はありません。
- 予算の通知は費用の反映より遅れます。**上限は厳密ではなく、止まるまでに予算を超えます。**
- API の有効化（Cloud Functions など）に課金が要るので、1 の直後から 4 までは続けて行います。その間は上限が働いていません。

```bash
BILLING_ACCOUNT=<請求先アカウント ID>
BUDGET=3000                        # 請求先アカウントの通貨で指定する（円なら 3000 円。2026-11-01 に 1000 から変更）
TOPIC=budget-alerts
GUARD_SA=budget-guard
```

1. **課金アカウントを紐づける:** `as_me gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"`
2. **Pub/Sub のトピックと予算を作る**（アラートは 50%・90%、停止の判断は関数が 100% で行う）:

   ```bash
   as_me gcloud services enable billingbudgets.googleapis.com cloudbilling.googleapis.com pubsub.googleapis.com --project "$PROJECT_ID"
   as_me gcloud pubsub topics create "$TOPIC" --project "$PROJECT_ID"
   as_me gcloud billing budgets create --billing-account="$BILLING_ACCOUNT" --billing-project="$PROJECT_ID" \
     --display-name="docunavi" --budget-amount="$BUDGET" --calendar-period=month \
     --filter-projects="projects/${PROJECT_ID}" \
     --credit-types-treatment=exclude-all-credits \
     --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0 \
     --notifications-rule-pubsub-topic="projects/${PROJECT_ID}/topics/${TOPIC}"
   ```

   予算額は**請求先アカウントの通貨**で指定します（`5USD` のように別の通貨を付けると `INVALID_ARGUMENT` になった）。`--billing-project` は予算の API の課金先の指定で、無いと `SERVICE_DISABLED` で失敗します。`--credit-types-treatment=exclude-all-credits` は、クレジットを引く前の費用で数える指定です（既定だと、無料トライアルのクレジットが残っている間は費用が 0 のままで、自動停止が働かない）。
3. **関数を作る**（最初は `DRY_RUN=true`。止める判断をログに出すだけで、課金は変えない）:

   ```bash
   as_me gcloud iam service-accounts create "$GUARD_SA" --display-name="Budget guard" --project "$PROJECT_ID"
   as_me gcloud services enable cloudfunctions.googleapis.com run.googleapis.com eventarc.googleapis.com \
     cloudbuild.googleapis.com artifactregistry.googleapis.com --project "$PROJECT_ID"
   as_me gcloud functions deploy budget-guard --gen2 --runtime python312 \
     --project "$PROJECT_ID" --region "$REGION" \
     --source ops/budget_guard --entry-point stop_billing --trigger-topic "$TOPIC" \
     --service-account "${GUARD_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --set-env-vars "PROJECT_ID=${PROJECT_ID},DRY_RUN=true"
   ```

4. **SA の権限を付ける:** `as_me gcloud billing accounts add-iam-policy-binding "$BILLING_ACCOUNT" --member="serviceAccount:${GUARD_SA}@${PROJECT_ID}.iam.gserviceaccount.com" --role=roles/billing.admin`。
   `roles/billing.admin` は公式の手順が使う役割で、**必要より広い**（公式は最小の役割を示していない）。この SA は、この関数だけに使います。続けて、トリガーが関数を呼べるようにします。**これが無いと、通知は関数に届かず**、ログに「lacks {run.routes.invoke}」と出ます（付けてから効くまで 1〜3 分）:
   `as_me gcloud run services add-iam-policy-binding budget-guard --project "$PROJECT_ID" --region "$REGION" --member="serviceAccount:${GUARD_SA}@${PROJECT_ID}.iam.gserviceaccount.com" --role=roles/run.invoker`
5. **動作を確かめる**（`DRY_RUN=true` のまま）: `as_me gcloud pubsub topics publish "$TOPIC" --project "$PROJECT_ID" --message '{"costAmount": 6, "budgetAmount": 5}'` のあと、`gcloud functions logs read budget-guard --gen2 --project "$PROJECT_ID" --region "$REGION" --limit 20` に「DRY_RUN: would disable billing」が出ます。
   確かめられるのはトリガーと判断までで、**課金を実際に外せる権限は、`DRY_RUN=false` で試すまで確かめられません**（2026-10-20 に、サービスを置く前の別のプロジェクトで試し、`roles/billing.admin` だけで外れた）。本番に切り替えるには、同じ `functions deploy` を `--update-env-vars "DRY_RUN=false"` で実行します。**以後は、上の Pub/Sub への手動の publish をしません**（本物の停止になる）。
6. **止まったあとの復旧:** まず原因（想定外の使用、攻撃、設定の誤り）を確かめます（同じ月に予算を超えたままだと、再び紐づけた直後に止まる。先に `DRY_RUN=true` に戻すか、予算額を上げる）。課金を紐づけ直し（`as_me gcloud billing projects link …`）、API の有効化・Cloud Run のサービス・Secret が残っているかを確かめ、無ければ作り直します。

デプロイ（上の A〜F）は、ここまで済んでから行います。

## 確かめる

非公開（`--no-allow-unauthenticated`）の間は、呼び出しの権限（`roles/run.invoker`）が要ります。閲覧専用の ID には無いため、あなた自身の権限（`as_me`）で実行します。**ID トークン付きの `curl` が、いちばん簡単です。**

```bash
URL=$(as_me gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')
TOKEN=$(as_me gcloud auth print-identity-token)
curl -s -H "Authorization: Bearer $TOKEN" "$URL/health"   # {"status":"ok","mode":"gemini","history":"pg"} なら本物の LLM と Postgres に繋がっている
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" "$URL/"   # 404（API は画面を配信しない）
unset TOKEN
```

公開した後は、トークン無しで `/health`（200）と、`POST /chat`（ログイン無しなので 401）を確かめます。`gcloud run services proxy` でも橋渡しできますが、**バックグラウンド（`&`）で動かすと、端末の入力を待って止まります**。使うなら、別の端末のフォアグラウンドで動かします。

- `mode` が `mock` なら、キーの設定が届いていません。`history` が `memory` なら、`DB_BACKEND` と `DATABASE_URL` が届いていません。
- 500 は、モデル名やロケーション、権限の誤りの可能性が高いです。Vertex AI はリージョンごとに提供モデルが違うため、`GEMINI_MODEL` と `GOOGLE_CLOUD_LOCATION` を確認します（[configuration.md](configuration.md)）。
- ログは `gcloud run services logs read "$SERVICE" --project "$PROJECT_ID" --region "$REGION"`（読み取りなので、閲覧専用の ID で動く）。

## 公開する・戻す

- **公開する**（予算アラートと `--max-instances` を決めてから。`agent-deployer` には公開の権限が無いので、あなた自身の権限で。組織のポリシーで `allUsers` が禁止されていることがある）:
  `as_me gcloud run services add-iam-policy-binding "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --member=allUsers --role=roles/run.invoker`
- **前のリビジョンに戻す:** `gcloud run services update-traffic "$SERVICE" --impersonate-service-account="$DEPLOYER" --project "$PROJECT_ID" --region "$REGION" --to-revisions=<リビジョン名>=100`
- **止める（削除）:** `as_me gcloud run services delete "$SERVICE" --project "$PROJECT_ID" --region "$REGION"`

## タグ付きプレビュー（本番に流す前に、専用 URL で確かめる）

新しいリビジョンを 0% で作り、タグ付きの URL でだけ届くようにします。本番のトラフィックは変わりません。実際に通りました（2026-10-21）。
書き込みの 4 つ（ビルド・配備・昇格・ロールバック）は、`scripts/preview.sh`（`build` / `deploy <コミット> [<タグ>]` / `promote <タグ>` / `rollback <リビジョン>`）にまとめてあります。あなたが実行します（`PREVIEW_DRY_RUN=1` を付けると、実行せずにコマンドだけ表示）。下の手順は、その中身です。
[r2-rollout.md](r2-rollout.md) の `r2-wire-stage` / `promote` / `rollback`（Job）は、R2 の環境変数とボリュームを書き換える専用なので、**この手順では使いません。**

```bash
PROJECT_ID=docunavi-app; REGION=asia-northeast1; SERVICE=docunavi
TAG="preview-$(date +%Y%m%d-%H%M)"      # 小文字・数字・ハイフンの短い名前。重複させない（文字数の上限は未確認）
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}:<ビルドしたコミット>"
```

**`IMAGE` のコミットは、ビルドしたものに合わせます**（ビルドのあとに別のコミットを重ねると、`HEAD` はビルドしていないイメージを指す）。`scripts/preview.sh` は、`build` で HEAD と一致しないコミットを拒否し、`deploy` はコミットを引数で受けます。

1. **本番のリビジョン名を控える（読み取り）:** `gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='yaml(status.traffic)'`。`percent: 100` のリビジョン名を `PROD_REV` として控える（ロールバック先）。
2. **イメージをビルドする（あなた）:** F と同じ。**`as_me` を付ける**（日常の ID では、ソースを置くバケットに書けず 403）。
3. **0% でデプロイし、タグを付ける（あなた）:** `as_me gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE" --no-traffic --tag "$TAG"`。
   **`--set-env-vars` と `--set-secrets` を付けない**（付けると、既存の Secret の参照が置き換わって消える。イメージだけを変える）。`--no-traffic` は、`LATEST` へ向いていたトラフィックを具体的なリビジョンに固定してから新しいリビジョンを作るので、以後の `LATEST` はトラフィックを受けません。
4. **0% とタグ付き URL を確かめる（読み取り）:** 手順 1 の `describe` で、新しいリビジョンが `tag: <TAG>` と `url: https://<TAG>---…` を持ち、`percent` が付いていない（0%）こと、`PROD_REV` が `percent: 100` のままであることを見ます。続けて、タグ付き URL の `/health`（`mode` が `gemini`、`history` が `pg`）。
   サービスが公開（`allUsers`）なので、タグ付き URL も認証なしで届きます（2026-10-21 に確認）。新しいリビジョンの環境変数の名前と Secret の参照が本番と同じことも、`gcloud run revisions describe <リビジョン> --format=json` から名前と `valueFrom` の有無だけを取り出して確かめます（値は出さない）。
5. **画面で確かめる（あなた）:** Cloud Run のタグ付き URL は API の確認用です。ログイン込みの画面の確認は Worker 側で行います（タグ付き URL でのログインの戻りは、確かめていません）。SSE は、回答が逐次表示されること、回答の途中で停止してもサーバーが続きを取り残さないこと（ログに 499 相当の INFO。ERROR にならない）、90 秒（`--timeout`）を超える質問で `error` チャンクの固定メッセージになり画面が固まらないことを見ます。
6. **本番へ移す（承認してから。あなた）:** `as_me gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --to-tags "$TAG=100"`。移したあと、`describe` と本番の `/health` を読みます。100% は、そのタグのリビジョンに固定され、`LATEST` は受けません。
   次に配備するときは、先に `describe` で `status.traffic` を読みます（通常の `--no-traffic` なしの `gcloud run deploy` が 100% を新しいリビジョンへ移すかは、未確認）。環境変数だけを足す変更も、`services update … --no-traffic --tag <タグ>` で 0% の確認用リビジョンを作ってから、同じ手順で移せます。
7. **ロールバック（あなた）:** 手順 1 で控えたリビジョンへ戻します（数秒。設定は消えない）: `as_me gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --to-revisions="<PROD_REV>=100"`。
8. **タグを外す（不要になったら。あなた）:** `as_me gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --remove-tags "$TAG"`（複数形。カンマ区切りで複数のタグを外せる。リビジョンそのものは消えない）。

**注意と未確認**

- **プレビューも、本番の DB・Gemini のキー・Secret を使います。** 確認で送った質問は、本番の会話履歴に残ります（7 日の掃除で消える）。DB を分ける仕組みはありません。サービスが公開なので、タグ付き URL の入口も公開で、質問は `AUTH_MODE=required` で守られます。
- 未確認: `agent-deployer` での `--image` のデプロイ（この手順は `as_me` 前提）、タグの名前の長さの上限。
- **Gemini の 503（`This model is currently experiencing high demand`）で質問が 502 になることがあります。** 新しいリビジョンの不具合ではありません（2026-10-21 に昇格の直後に 1 回。数分あけて再送したら 200）。429 ではないので有料キーへの切り替えは働きません。確認で 502 が出たら、まずコンテナログの例外（`503 UNAVAILABLE` か）を見て、アプリの問題と切り分けます（フォールバックは [configuration.md](configuration.md) の「主モデルが落ちたとき」）。

## 古いリビジョンとイメージの整理

- **リビジョン:** 戻し先として、本番と、その前の 2 つだけを残します。0% のリビジョンに料金はかかりませんが、古いものは DB・認証・環境変数が変わっていて戻し先にならず、一覧が長くなります。配備のたびに増えるので、タグを外す（上の手順 8）ときに、不要なものを `gcloud run revisions delete` で消します（元に戻せないので、一覧と名前を確かめてから）。
  タグ push の配備では、最後のステップ `prune-old-revisions` が自動で消します（次の節）。
- **イメージ（Artifact Registry）:** 保管料がかかるので、クリーンアップポリシーで自動で消します。定義は [ops/artifact-registry/cleanup-policy.json](../ops/artifact-registry/cleanup-policy.json)。
  - `docunavi` のイメージは最新 5 個を残し、それより古いものを消します。`docunavi-admin` `docunavi-ingest` `docunavi-mcp` など、`docunavi-` で始まる別のパッケージには触りません（パッケージ名の指定は前方一致のため）。
  - **使用中のイメージは `pin-` タグで守ります（`keep-pinned`）。** 「最新 5 個」は本番と Job が同じ枠を取り合うため、Job のイメージは配備が数回進むと枠外になり、消えます（`docunavi-history-cleanup` が一度 `ContainerMissing` になった原因）。タグ `pin-*` が付いた `docunavi` のイメージは、枠外でも残ります。
    付けるタグは `pin-prod`・`pin-rollback`（切り替え前の本番のイメージ）・`pin-history-cleanup`・`pin-ingest-sync`・`pin-ingest-queue` です。`./scripts/release.sh` は、このうち `pin-prod` と `pin-rollback` を付け替えます（上の「タグ push で 0% 配備する」。2026-11-17 の実機の通しで実行）。レジストリのタグの一覧は閲覧用の権限では読めず（2026-11-20 に PERMISSION_DENIED）、いまの付き方は未確認です。**本番または Job のイメージを更新したら、そのたびにタグを付け替えます**（古いイメージは枠外になれば自動で消える）。
    `R=asia-northeast1-docker.pkg.dev/docunavi-app/cloud-run-source-deploy/docunavi; gcloud artifacts docker tags add $R@sha256:<新しいダイジェスト> $R:pin-<名前>`（`tags add` は `--project` `--location` を受け付けない）。更新のあと、`gcloud run jobs list` と `gcloud run revisions list` のイメージが残っているかを、`gcloud artifacts docker images describe` で 1 つずつ確かめます。
    `r2-verify` は `pin-` を付けません（利用者の決定。イメージは再ビルド・再プッシュできる）。消えたら、再ビルドするか差し替えます。
  - 設定するとき: `gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy --project docunavi-app --location asia-northeast1 --policy=ops/artifact-registry/cleanup-policy.json --no-dry-run`。`--dry-run` は設定の書き込みで、削除はしません（結果の Data Access ログは、このプロジェクトでは有効にしていないので見られない）。
  - **確認済み（2026-11-15。証拠等級 A）:** `docunavi` のイメージは 25 個から 6 個になり、他のサービスのイメージは残りました。**未確認:** 6 個が 5 個に収まること、`keep-pinned` が実際に削除を防ぐこと（次の日次の実行後に確かめる）。
  - 消えたイメージは戻せません（必要なら、そのコミットから再ビルド）。リビジョンが使っているイメージを消すと、そのリビジョンは新しいインスタンスを起動できなくなります。

## タグ push で 0% 配備する（Cloud Build トリガー）

`deploy/*` のタグを push したときだけ、Cloud Build がビルドして `--no-traffic --tag` で配備します（`cloudbuild-deploy.yaml`）。役割の分け方は、`main` への push はテストだけ（画面は手動の `deploy.yml`）、Cloud Run はタグ push だけです。**本番のトラフィックは自動では変えません**（100% への切り替えは `./scripts/preview.sh promote`、戻すのは `rollback`。あなたが実行）。
**切り替えからあとの手順は `./scripts/release.sh <ビルド ID>` にまとめてあります**（ホストで、あなたが実行。承認待ちなら承認 → 完了待ち → 新リビジョンが 0% で `/health` が 200 か確認 → **切り替え前の確認は 1 回だけ** → 切り替え → 本番の資格確認〔`scripts/prod_check.sh --chat` を呼ぶ。テスト用ログインを一時的に有効にし、必ず削除。単独の確認は [testing.md](testing.md) の「本番の /chat の自動テスト」〕→ `pin-prod`・`pin-rollback` の付け替え → 戻すコマンドの表示。`PREVIEW_DRY_RUN=1` で書き込みを実行せず確認、`--no-check` で資格確認を省く）。2026-11-17 に追加。**実機の通しは 2026-11-17 に 1 回確認済み**（ビルド `05265f65`、承認 → 完了待ち → 0% と `/health` 200 → 切り替え → 資格確認〔403 → 200、LLM 1 回〕→ ピン留め。証拠等級 A）。この回は、承認と実行を、Claude がホストから、ユーザーの指示のもとで行った。`pin-rollback` は、直前の本番のイメージ 1 つだけを守る（次の切り替えで付け替わる）。
**状態（2026-11-14）:** サービスアカウント `docunavi-build`、GitHub の接続 `docunavi-github`（2nd gen）、トリガー `docunavi-tag-deploy`（承認必須）を作成し、2 回目のタグ push で 0% の配備まで通りました。
**2026-11-17 の配備の実績と失敗（gcloud の読み取り、証拠等級 A。2026-11-20 に確認）:** 現在 100% を受けているのは、`docunavi-00091-juj`（タグ `suggest-hook-1117`。タグ push `deploy/suggest-hook-1117` のビルド `12ebab42`、コミット `7e5919d`、2026-11-17 02:42 JST 作成）です。
これより後の、新しい形のタグの最初の 2 回、`deploy/v20261117`（ビルド `d24cc1ec`、03:48 JST）と `deploy/v20261117-2`（ビルド `f1c62c9d`、03:53 JST。どちらもコミット `6119ae5`）は、`check-tag`・ビルド・push が通ったあと、**`deploy-no-traffic` が失敗しました**（「The user-provided container failed the configured startup probe checks」。`prune-old-revisions` は実行されていない）。できたリビジョン `docunavi-00093-siw` と `docunavi-00094-nem` は Ready=False で、0%・タグ無しのまま残っています。
両方のログに、起動時の JWKS の取得（認証用 Worker の `/api/auth/jwks`）が `HTTP Error 500` で失敗した `AuthConfigError` があります（上の「E」のとおり、取れなければリビジョンは Ready にならない）。**認証用 Worker が 500 を返した原因は未確認**で、Prisma Free の上限（[decisions/0012](decisions/0012-accept-prisma-free-limit-and-cut-operations.md)）との関係も未確認です。本番のトラフィックは変わっていません。`deploy/v20261117-1` のビルドは取り消し（CANCELLED）でした。
2026-11-20 の時点で、タグ push のビルドは 2026-11-17 03:53 JST が最後で、サービスの最新のリビジョンは `docunavi-00094-nem`（2026-11-17 03:54 JST）です。それ以降に新しいリビジョンは作られていないので、配信中の `docunavi-00091-juj` のコミット `7e5919d` より後のコミット（`6119ae5` と、その手前の依存の更新 `a138209` などを含む）は、Cloud Run のサービスに配備されていません（取り込み Job 2 本も、`docunavi-00091-juj` と同じイメージ）。

- **タグの名前（2026-11-17 に決定）:** Git のタグは、その日の 1 回目が `deploy/vYYYYMMDD`（例: `deploy/v20261117`）、2 回目以降が `deploy/vYYYYMMDD-N`（例: `deploy/v20261117-2`）。形が違えば、ビルドの前に `check-tag` が止めます。
  - 日付は **JST** の、実在する日で、未来でない日（`check-tag` が確かめる）。N は **2 から・先頭ゼロなし**（`-1` `-0` `-02` は止める。1 回目は日付だけ）。内容（`suggest` など）は名前に入れず、タグを切るコミットのメッセージに書く。
  - **使ったタグと番号は再利用しない。** 配備を失敗して取り消した番号も飛ばして、次の番号を使う（Git は同じ名前のタグを作らせない。削除して作り直す、`-f` で付け替えることもしない）。Cloud Run は、同じタグ名を別のリビジョンへ付けると、前のリビジョンから黙って外す。この再利用は `check-tag` では止まらない（未確認の穴）。
  - **変換:** Cloud Run のタグ名は、Git のタグから先頭の `deploy/` を外したもの（`deploy/v20261117` → `v20261117`、`deploy/v20261117-2` → `v20261117-2`。タグ付き URL は `https://v20261117---…`）。先頭の `v` は、Cloud Run のタグ名が英字始まりのため。
  - **旧形式の Git タグ 4 つ**（`deploy/py314-1116` `deploy/suggest-20261117` `deploy/suggest-hook-1117` `deploy/membership-1117`）は、2026-11-17 に削除しました（名前とコミットの対応は Cloud Build の履歴にあります）。Cloud Run のタグ・リビジョンは変更しません。旧形式の名前は、あらためて push しても `check-tag` が止めます。戻すのは、旧リビジョンへの `./scripts/preview.sh rollback <リビジョン>`（リビジョン名で指定するので、タグの形に左右されない）。`promote <Cloud Run のタグ名>` と `release.sh` のタグの検査は、旧形式も通る緩い形のままで、旧ビルドの ID でも動きます。
  - `scripts/preview.sh deploy` のプレビュー用のタグ（`preview-YYYYMMDD-HHMM`）は別系統で、この規則の対象外です。
  - Cloud Build のトリガー `docunavi-tag-deploy` のパターン `^deploy/.*$` は、そのままです。形の違うタグを push すると、承認待ちのビルドは作られますが、`check-tag` で失敗して、リビジョンは増えません。
- 配備は `--set-env-vars` `--set-secrets` `--use-http2` を付けません。環境変数・Secret・ポート名（`http1` か `h2c`）は、サービスの最新の設定を引き継ぎます。
- タグを切ったコミットがそのまま配備されます（`main` にある・CI が通っていることは、トリガーは確かめない。0% なので本番には出ない）。料金は、タグを push した回数分の Cloud Build だけです。

### 準備（あなた。一度だけ。書き込み。2026-11-14 に実施済み）

```bash
# PROJECT_ID・REGION・SERVICE・RUN_SA・as_me は「準備」で定義したもの
REPO=cloud-run-source-deploy
BUILD_SA="docunavi-build@${PROJECT_ID}.iam.gserviceaccount.com"

# 1. このトリガー専用のサービスアカウント（長期キーは作らない）
as_me gcloud iam service-accounts create docunavi-build --display-name="DocuNavi tag deploy" --project "$PROJECT_ID"

# 2. 最小の権限: イメージの push、Cloud Run サービスの更新、実行用 SA としての配備、ビルドのログ
as_me gcloud artifacts repositories add-iam-policy-binding "$REPO" --location "$REGION" --project "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/artifactregistry.writer
as_me gcloud run services add-iam-policy-binding "$SERVICE" --region "$REGION" --project "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/run.developer
as_me gcloud iam service-accounts add-iam-policy-binding "${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" --project "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/iam.serviceAccountUser
as_me gcloud projects add-iam-policy-binding "$PROJECT_ID" \
  --member="serviceAccount:${BUILD_SA}" --role=roles/logging.logWriter

# 3. GitHub の接続（2nd gen）。作成時に Cloud Build のサービスエージェント
#    （service-<プロジェクト番号>@gcp-sa-cloudbuild）へ roles/secretmanager.admin が要る
#    （OAuth トークンのシークレットを作るため。接続が済んだら外す。2026-11-14 はそうした）。
#    PENDING_USER_OAUTH のあと、ブラウザーで GitHub の OAuth とアプリのインストール（Only select repositories で docunavi だけ）。
as_me gcloud builds connections create github docunavi-github --region "$REGION" --project "$PROJECT_ID"
as_me gcloud builds connections describe docunavi-github --region "$REGION" --project "$PROJECT_ID"   # installationState.stage が COMPLETE になるまで
as_me gcloud builds repositories create docunavi --connection docunavi-github --region "$REGION" --project "$PROJECT_ID" \
  --remote-uri=https://github.com/Starlight-Tree/DocuNavi.git

# 4. タグ push のトリガー。2nd gen なので --repository と --region を使う。
#    置換変数は渡さない（イメージは API だけ）。--require-approval で、ビルドの開始前に Console で承認する
as_me gcloud builds triggers create github --project "$PROJECT_ID" --region "$REGION" --name=docunavi-tag-deploy \
  --repository="projects/${PROJECT_ID}/locations/${REGION}/connections/docunavi-github/repositories/docunavi" \
  --tag-pattern='^deploy/.*$' --build-config=cloudbuild-deploy.yaml --require-approval \
  --service-account="projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}"
```

### 使うとき

使うタグは、`D=$(TZ=Asia/Tokyo date +%Y%m%d); git ls-remote --tags origin "deploy/v${D}*"; git tag -l "deploy/v${D}*"` で、リモートとローカルの両方の使用済みのタグを見て決めます（無ければ `deploy/v${D}`、あれば `-2` から）。`git tag deploy/v20261117 && git push origin deploy/v20261117`（push は承認を得てから）。タグは、実績のある軽量タグにします（注釈付きタグでトリガーが動くかは確かめていません）。承認は `gcloud alpha builds approve <ID> --location <リージョン>`（`beta` は地域指定に対応しない）でできます。読み取りでの確認は、`gcloud builds list --billing-project=docunavi-app --project docunavi-app`（成否）と、`gcloud run services describe docunavi … --format='yaml(status.traffic)'`（新しいリビジョンが 0% で、本番が 100% のまま）です。
配備のあと、最後のステップ `prune-old-revisions` が古いリビジョンを消します。新しい順に 3 つと、トラフィックが向いている・タグが付いたリビジョンは残します（タグが付いた古いものは、タグを外すまで残る）。消せなくても警告だけで、ビルドは成功のままです（2026-11-15 の実際のビルドで動作を確認。証拠等級 A）。

### 限界と確認

- **0% が保証されるのは `cloudbuild-deploy.yaml` の中身によります。IAM ではありません。** `roles/run.developer` は `update-traffic` も含むので、権限だけでは配備と切り替えを分けられません。ファイルを変えるときは、`--no-traffic` が残ることを確かめます。
- タグを push できる人（リポジトリの書き込み権限を持つ人）は、トリガーを起動できます。GitHub のタグ保護（ルールセット）は未設定です。**エージェントも承認できるので、`--require-approval` は人の目を通す関門ではなく、誤タグへの安全策にとどまります。**
- **確認済み（2026-11-14〜15。証拠等級 A: 実機）:** 接続・リポジトリ・トリガーの作成コマンドが通る / タグの push でトリガーが起動し、上の最小権限（4 つ）で、ビルド・push・`--no-traffic --tag` の配備まで通る（承認から約 2 分）/ ポート名 `h2c` が引き継がれる / 誤ったタグ名（`deploy/Bad_Check`）は `check-tag` で止まり、リビジョンは増えない / トリガーで配備したリビジョンのタグ付き URL の `/health` が 200（初回は `minScale: 0` のコールドスタートで約 10.6 秒）。
  1 回目のタグ push は、置換変数がシェルの環境変数にならず `check-tag` で失敗しました（`env: TAG_NAME=${TAG_NAME}` を足して直した）。承認待ちのビルドの取り消しは、`gcloud alpha builds reject` が地域指定を扱えないので、`gcloud builds cancel <ID> --region <リージョン>`（未試行）です。
- **HTTP の startup プローブ（2026-11-14。1 回）:** 本番のサービスの startup は HTTP の `/health`（ポート 8080。ポート名が `h2c` のままでも、HTTP/1 のプローブは通る）で、liveness は入れていません（Cloud Run に readiness は無い。`minScale: 0` で、再起動が必要な固まり方も起きていない）。プローブは、`--startup-probe` を付けない以後の配備にも引き継がれる見込みです（gcloud の挙動からの推測。未確認）。
  戻し方: `gcloud run services update-traffic docunavi --project docunavi-app --region asia-northeast1 --to-revisions=<戻し先のリビジョン>=100`。画面での質問の確認は、していません。
- `agent-deployer`（この文書の別の節）とは別で、ここでは使いません。

## 保留中の運用の準備（実行は利用者の操作待ち）

書き込み（本番の設定・DB・配備）は、すべて利用者が実行します。`as_me` の意味は「誰の権限で実行するか」の節です。1〜3（引用の配備と確認、ロールを割り当てた利用者の `/chat`）は 2026-11-09 に実施済みなので、この節から外しました（結果はロードマップの 4c と ⑧。手順は Git の履歴）。

### 4. 履歴削除ジョブ（`--apply` を戻した後の確認）

**現状（2026-11-15、読み取りで確認、証拠等級 A）:** Job `docunavi-history-cleanup` は `--older-than-days 7 --apply`（`--apply` は利用者の承認を得て、2026-11-15 に戻した。実行はしていない）。最初に消えうるのは、2026-11-22 03:00 JST の定期実行（日曜）です。

**2026-11-20 の訂正（gcloud の読み取り、証拠等級 A）:** 上の「実行はしていない」は、戻した直後（記録のコミットは 2026-11-15 00:45 JST）の時点の記述です。同日 03:00 JST の定期実行は `--apply` つきで動き、ログは「削除した会話: 0 件」、終了コード 0 でした。Job の定義は、引数 `--older-than-days 7 --apply`、イメージ `docunavi@sha256:e34b160c…`、SA `docunavi-run` のままです（最後の実行も 2026-11-15 03:00 JST）。

戻す更新は、Job のイメージ（`docunavi:60833f5`）が Artifact Registry のクリーンアップポリシーで消えていたため一度失敗しました（`ContainerMissing`）。本番と同じダイジェスト `docunavi@sha256:e34b160c…` に差し替え、`Ready=True` に戻りました。**残る弱点:** Job のイメージもポリシーで消えうるので、イメージを更新するたびに、Job が参照するイメージが残っているかを確かめます（`pin-history-cleanup` の付け替え）。

**未確認:** 対象が 1 件以上あったときの実行（2026-11-22 03:00 JST が最初に当たりうる）のログ、`checkpoints` と `checkpoint_writes` の DELETE 権限（読み取りの権限では見えない。2026-11-15 の実行は対象が 0 件で、DELETE を流していない）、Prisma への復元。

**初回の実行の前後の確認:**
1. 直近 24 時間以内のバックアップが R2 にあること（[pg-backup.md](pg-backup.md)。週 1 回、日曜 00:00 JST。別の pgvector への復元は確認済みで、Prisma への復元は未確認）。
2. Scheduler `docunavi-history-cleanup-weekly` が有効で、日曜 03:00 JST のままであること（`gcloud scheduler jobs describe …`。閲覧用の権限では読めない）。
3. 実行の後、ログの「削除した会話: N 件」と、DELETE 権限のエラーが無いこと、削除後の `/chat` と会話一覧が動くことを確かめ、[configuration.md](configuration.md) の「会話履歴の保存先」を直します。

### 5. `budget-guard` の `DRY_RUN` を `false` に戻す条件と、戻す前の確認

**現状（2026-11-09、読み取り、証拠等級 A）:** リビジョン `budget-guard-00004-b7r`、`DRY_RUN=true`。ログの「would disable billing」は 2026-11-01 01:02〜01:10 UTC に集中し（費用 1004〜1051 に対して予算 1000.0）、それ以降はありません。
2026-11-20 に読み取りで再確認（証拠等級 A）: 最新の準備済みのリビジョンは `budget-guard-00004-b7r`、`DRY_RUN=true`、`PROJECT_ID=docunavi-app` のままです。
2026-11-01 00:30 UTC の再発は、同じ月の費用がすでに予算を超えた状態で課金を紐づけ直したためと読めます（推測）。同じ 0.2 秒の間に「billing disabled」と、`resourcemanager.projects.deleteBillingAssignment` の 403 が両方出ており、2 つのインスタンスが同時に起動して、一方が成功したあとに他方が失敗したと読めます（推測。ログの事実は等級 A）。

**戻す条件（利用者の決定）:**
1. いまの予算額が ¥3,000 であること（途中で 1000 から 3000 に変更した。ログの 1000.0 は変更前）と、通貨・対象の範囲を、Console で確かめる。
2. 止まる影響を受け入れる。課金が外れると、Cloud Run の API・MCP と埋め込み（Vertex）が止まる（画面と認証の Worker、Prisma Postgres は GCP の外）。復旧は手動で、リソースが消えることがある（公式の警告）。
3. 課金を外す権限が、本番のプロジェクトで通ること（`docunavi-app` でも 2026-11-01 00:31 に一度は外れた）。
4. 止まったあとの戻し方（`budget-guard-disarm`、または課金の再紐づけ）が実際に通ること（`budget-guard-disarm` の動作確認は利用者の報告だけ）。
5. 月の費用が予算に十分届かない状態で切り替える（届いた状態で切り替えると、すぐ止まる）。

**戻す前の確認:** `gcloud run services describe budget-guard`（`DRY_RUN` と PROJECT_ID）、予算の 50・90・100% の通知先 `budget-alerts` に届くこと、直近の月の費用（Console）。切り替えは、G の手順 5 のコマンド（`--update-env-vars "DRY_RUN=false"`）。**Pub/Sub への手動の publish はしません**（本物の停止になる）。

### 6. 監視のアラート

ログベース指標 4 つとアラート 2 つを、2026-11-09 に作成しました（利用者が実行。通知先は利用者宛てのメール `docunavi ops` 1 件）。閲覧用の権限では通知先とアラートの一覧を読めないので、作成の確認は利用者が貼った一覧による（証拠等級は利用者の報告）。**未確認:** 通知先の確認メールを押したか、実際に通知が届くか。

| アラート | 条件 | 理由 |
|---|---|---|
| チャットの失敗 | `chat_failures_total` が 10 分に 3 件以上（[ops/monitoring/alert-chat-failures.yaml](../ops/monitoring/alert-chat-failures.yaml)） | 利用が少ない（`chat_metrics` は 30 日で 2 件）ので、率でなく件数で見る |
| Cloud Run の 5xx | `docunavi` の 5xx が 5 分に 5 件以上（[ops/monitoring/alert-cloudrun-5xx.yaml](../ops/monitoring/alert-cloudrun-5xx.yaml)） | チャットの指標に載らない失敗（起動・DB の 503 など）を拾う |

別に、バックアップ失敗のアラート 2 本を 2026-11-11 に作成したと、利用者から報告を受けています（閲覧用の権限ではアラートの一覧を読めず、確認できていない。証拠等級は利用者の報告。[pg-backup.md](pg-backup.md) の「失敗の通知の作成」）。

作り直すときは、同じ名前で 2 回作ると重複するので、先に一覧で確かめます:

```bash
CH=$(as_me gcloud alpha monitoring channels create --project docunavi-app --display-name "docunavi ops" \
  --type=email --channel-labels=email_address="$EMAIL" --format='value(name)')
for f in ops/monitoring/alert-chat-failures.yaml ops/monitoring/alert-cloudrun-5xx.yaml; do
  as_me gcloud alpha monitoring policies create --project docunavi-app --policy-from-file="$f" --notification-channels="$CH"
done
```

指標の値が増えることの確認は、`scripts/prod_smoke_chat.py --stream`（[testing.md](testing.md) の「本番の /chat の自動テスト」。1 回送るごとに `chat_requests_total` が 1 増える）で行います（未確認）。失敗時の `status` が本番のログで文字列になることは、本番で失敗が起きるまで受動確認です（コードとテストでは文字列）。

## GitHub Actions のデプロイ用の秘密（OIDC・Infisical・Cloudflare）

2026-11-08 に決めた構成です（2026-11-18 に `.claude/rules/secrets.md` から移した。本文は変えていない。方針の要約は `secrets.md` に残してある）。
`.github/workflows/deploy.yml` が、GitHub の OIDC で Infisical にログインし、Cloudflare の API トークンを実行時に取る。GitHub には Cloudflare の秘密を置かない。

- Infisical: プロジェクト `docunavi-deploy`（環境 `prod`）。秘密は `CLOUDFLARE_API_TOKEN` と `CLOUDFLARE_ACCOUNT_ID` だけ。ほかの用途の秘密を入れない
  （無料プランは復元ができないので、プロジェクトを分けて影響を限る）。`docunavi-devcontainer` は、このプロジェクトに入れない（MCP から届くため）。
- マシン ID `github-actions-deploy`（OIDC Auth、プロジェクトの Viewer）:
  - Discovery URL・Issuer: `https://token.actions.githubusercontent.com`
  - Subject: `repo:Starlight-Tree@154563471/DocuNavi@892933857:environment:production`（数値 ID つき。リポジトリの改名・移管で変わる）
  - Audience: `https://github.com/Starlight-Tree`、Access Token TTL: 600 秒
- Cloudflare の API トークン: アカウントに限定。権限は Workers Scripts: Edit と D1: Edit から始め、足りなければ足す。有効期限は 90 日。
  （2026-11-20 の注記: `docunavi-api` の D1 の束縛は、2026-11-08 に `workers/api/wrangler.jsonc` から外した〔`e556133`。配備済みの Worker の束縛は未確認〕。いまの `wrangler.jsonc` の束縛は Hyperdrive だけ。いまのトークンに D1: Edit が付いているか、まだ要るかは未確認。）
  期限前に作り直し、Infisical の値を更新する。IP 制限は、GitHub のランナーの IP が不定なので使えない。
  現在のトークン `docunavi-github-actions-deploy` は 2026-11-08 に作成し、**期限は 2027-02-06**（作り直しは 2027-01 中）。
- GitHub: Environment `production`（必須レビュアーは 2026-11-15 に外した〔非公開の無料プランでは効かず、2026-11-09 に承認なしで始まった。公開後は効くことを確認したが、毎回の承認が煩雑なため〕。許可するブランチは `main` だけ。承認は起動前の会話で取る）、Variable `INFISICAL_IDENTITY_ID`（公開してよい ID）。
- 無料プランで OIDC Auth が使えることは、ユーザーが判断した（公式の文書では確認できていない。2026-11-08）。使えなくなったら、この経路は止まる。

## 続き

次の段階は [roadmap.md](roadmap.md) を参照してください。
