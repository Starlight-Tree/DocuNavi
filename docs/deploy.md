# Cloud Run へのデプロイ（段階 0）

**本番環境で実行済みです（2026-10-20 初回公開）。** 本文書の手順に基づき、Cloud Run へのサービス配備・公開、Cloud Build でのビルド、Secret Manager やサービスアカウントの連携、およびバッチ Job や予算ガードが本番稼働しています（実際に通った手順やつまずきの詳細は末尾の「実際に通った記録（2026-10-20）」を参照）。

**資料の検索が Vectorize + D1 の検索になったため（[search-design.md](search-design.md)）、本物の LLM のデプロイには `D1_API_URL` と `D1_API_TOKEN`（データ用 Worker `docunavi-api`）が必須です**
（下の C。「任意」ではありません）。さらに、質問の埋め込みは API キーで呼ぶので、**A（Vertex AI）でも `GEMINI_API_KEY` の Secret が要ります**
（B の Secret の作り方。`EMBEDDING_USE_VERTEXAI=true` にした場合は、埋め込みも Vertex AI になりキーは不要。[configuration.md](configuration.md) の「埋め込みを Vertex AI で呼ぶ」。本番は未切り替え）。本番では `GEMINI_API_KEY` と `DATABASE_URL` を Secret Manager から注入して稼働させています。
本番の Cloud Run（`docunavi-00025-qey`）には `GOOGLE_GENAI_USE_VERTEXAI` の設定が無く（2026-10-31 に確認）、LLM は B（キー）の経路です。Secret の参照の中身は**要確認**です。

**デプロイの方針:** GCP の操作は原則 gcloud CLI で行います。Cloud Code・Gemini Code Assist・Data Agent Kit・Jupyter 系の VS Code 拡張は導入しません。
LiteLLM gateway is removed and no longer used（Gemini CLI is not part of the docunavi runtime）。画像生成の Cloud Run Job は星庭ジョブ（`hoshiniwa-job`）のリポジトリで管理し、このリポジトリには置きません。

## 誰の権限で実行するか

書き込み系のコマンド（API の有効化、サービスアカウントや権限の作成、デプロイ、公開）は、**あなたが実行します。**
開発コンテナの gcloud は、既定で閲覧専用の `docunavi-viewer`（運用用プロジェクト `docunavi-ops` のサービスアカウント）になりすましています（[.devcontainer/README.md](../.devcontainer/README.md)）。
書き込みは、コマンドごとに「誰の権限か」を明示します。

| 操作 | 使う ID | 書き方 |
|---|---|---|
| 読み取り（ログ、状態の確認） | `docunavi-viewer` | 何も付けない。Cloud Build の閲覧だけ `--billing-project=docunavi-app` を付ける |
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
SERVICE=docunavi
RUN_SA=docunavi-run
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

   **記録（2026-11-02。利用者の報告と、私が確認したことを分ける）:**

   - 利用者の報告: `docunavi-app` で `aiplatform.googleapis.com` を有効化した。開発コンテナ内で、利用者本人の認証により `gemini-3.1-flash-lite-image` の画像生成テストが成功し、`test_image_0.png` ができた。権限の変更は、報告に無い（無しか未確認か、利用者に確認が要る）。
   - 私が確認した: `test_image_0.png`（リポジトリ直下、未追跡）は 1024×1024 の画像として表示できた。**実体は JPEG で、拡張子だけが `.png`**（先頭・末尾のマーカーで確認）。資料画像の取り込み（`app/hero_images`）は中身のバイトで形式を判定するので、JPEG のままでも扱える。
   - 私が確認した（読み取り）: 本番の Cloud Run `docunavi` の実行サービスアカウントは `docunavi-run`（`docunavi-sse-relay` も同じ）。環境変数の名前に `GOOGLE_GENAI_USE_VERTEXAI`・`GOOGLE_CLOUD_*`・なりすましの設定は無く、本番の LLM はキーの経路のまま。今回の有効化は、本番の動作を変えない。
   - 私が確認した（テストのスクリプト `test_image.py`、リポジトリ直下、未追跡）: `genai.Client(vertexai=True, project="docunavi-app", location="global")`、モデル `gemini-3.1-flash-lite-image`、縦横比 1:1。認証はコード内に無く、ADC。生成した画像のバイト列を、形式を見ずに `.png` で保存している（上の JPEG の理由）。
   - **未確認:** `docunavi-run` に `roles/aiplatform.user` が付いているか（閲覧専用の ID は IAM ポリシーを読めない。`docunavi-app` の Cloud Resource Manager API が無効で、私は有効化していない）、API の有効化そのもの、ADC が本人の認証かどうか。利用者の権限で確認する:

     ```bash
     as_me gcloud projects get-iam-policy docunavi-app --flatten=bindings \
       --filter='bindings.members:serviceAccount:docunavi-run@docunavi-app.iam.gserviceaccount.com' --format='value(bindings.role)'
     as_me gcloud services list --enabled --project docunavi-app --filter='config.name:aiplatform.googleapis.com'
     ```

   - **なりすまし設定（`CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`）の影響（gcloud の仕様による整理。このコンテナの認証情報の中身は見ていない）:**

     | 場面 | 使われる認証 | なりすましの影響 |
     |---|---|---|
     | 開発コンテナの gcloud コマンド | 既定で閲覧専用の `docunavi-viewer` | 読み取りだけ。Vertex AI の生成や書き込みは 403 になる設計 |
     | 開発コンテナの Python（`google-genai`、Vertex） | ADC（アプリケーションのデフォルト認証）。gcloud のなりすましの設定は、ADC には効かない | なりすましは効かない。ADC を作った認証（今回は利用者本人）で動く |
     | 本番の Cloud Run | 実行サービスアカウント `docunavi-run`（メタデータサーバー） | 設定が無いので影響なし |

   - 注意: ADC を開発コンテナ内で作った場合、利用者本人のリフレッシュトークンがボリュームに残る。テストが終わったら、残すか消すか（`gcloud auth application-default revoke`。利用者が実行）を決める。

2. **実行用のサービスアカウントを作る**

   ```bash
   as_me gcloud iam service-accounts create "$RUN_SA" --display-name="DocuNavi runtime" --project "$PROJECT_ID"
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
（`roles/run.viewer` を持つ `docunavi-viewer` を含む）に値が見えてしまいます。

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

**無料キーが 429 のときだけ有料キーを使う（任意）。** 無料枠のキー（請求の無いプロジェクト）を `gemini-api-key` に入れて始めるときは、
予算ガード付きのプロジェクト（`$PROJECT_ID`）に作ったキーを、別の Secret `gemini-api-key-paid` に入れておきます
（[configuration.md](configuration.md) の「無料キーが 429 のとき」）。足さなければ、無料キーだけで動きます。
名前は、キー `docunavi-paid`（キーのラベル）、Secret `gemini-api-key-paid`、環境変数 `GEMINI_API_KEY_PAID`。

実際に通した手順（2026-10-20。`as_me` は上の定義、`$PROJECT_ID` は予算ガード付きのプロジェクト）:

```bash
# 1. キーを作る。gcloud は結果（キー本体を含む）を stderr に出すので、2>/dev/null で捨てる（付け忘れると画面とログに出る）
as_me gcloud services enable apikeys.googleapis.com generativelanguage.googleapis.com --project "$PROJECT_ID"
K=$(as_me gcloud services api-keys create --project "$PROJECT_ID" --display-name=docunavi-paid \
      --api-target=service=generativelanguage.googleapis.com \
      --format='value(response.keyString)' 2>/dev/null | tr -d '\n')
echo "length: ${#K}"        # 39 前後を確かめる。値は表示しない
# 2. Secret に入れ、実行用サービスアカウントに読み取りを許す
printf '%s' "$K" | as_me gcloud secrets create gemini-api-key-paid --data-file=- --project "$PROJECT_ID"
unset K
as_me gcloud secrets add-iam-policy-binding gemini-api-key-paid \
  --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID"
# 3. 今のイメージのまま、Secret だけを足す
as_me gcloud run services update "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --update-secrets "GEMINI_API_KEY_PAID=gemini-api-key-paid:latest"
```

- **`--set-secrets` ではなく `--update-secrets` を使う。** `--set-secrets` は既存の Secret を置き換える。上の B のデプロイの例（`GEMINI_API_KEY` と有料キーの 2 つだけ）をそのまま使うと、`D1_API_TOKEN` が外れて起動に失敗する。
  足したあとは、`gcloud run services describe` で `GEMINI_API_KEY` と `D1_API_TOKEN` が残っていることを確かめる。
- **AI Studio の一覧に出すには、プロジェクトのインポートが要る。** 一覧には、取り込んだプロジェクトのキーだけが出る（「プロジェクトをインポート」で `docunavi-app` を選ぶ。`docunavi-ops` は取り込まない）。
  CLI で作ったキーは Cloud Console の「API とサービス → 認証情報」にも出る。
- **有料の階層（Tier 1）にするには、AI Studio で最低 $5 の前払いが要る。** 請求階層が「無料トライアル」・ステータスが「前払いが必要」のうちは、有料の枠として使える保証がない。
  前払い（クレジットの購入）は AI Studio の画面だけで行い、CLI や API では行えない。クレジットは購入から 12 か月で失効し、返金されない。自動チャージは設定しない（設定しなければ、残高が $0 になった時点で止まる）。
  Google Cloud の無料クレジット（ウェルカムクレジット）は、2026-03-02 以降のアカウントでは Gemini API に使えない（公式の請求ドキュメント。AI Studio の画面で確認）。
- **前払いの残高が尽きると、有料キーは 402 を返す。** 切り替えの条件は 429 だけなので、その場合は切り替わらず、無料キーの 429 がそのまま返る。
- 2026-10-20 の記録: `docunavi-app` が Tier 1 と表示され、前払い ¥800 を購入した（利用者の報告）。有料キーでの実 API の呼び出しは確認済み（同日、Secret の値で `gemini-3.1-flash-lite` を 1 回呼んで HTTP 200。キーは 39 文字で、Tier 1 の課金も通っている。利用者が実行した結果の報告）。本物の 429 での切り替えと 60 秒の保持は未確認。

### C. データ用 Worker に接続する（本物の LLM では必須）

`D1_API_URL`（データ用 Worker の URL。公開してよい値）と `D1_API_TOKEN`（Secret `d1-api-token`）は、資料の検索（Vectorize + D1）と会話履歴の保存先を兼ねます。
無いと、本物の LLM では起動に失敗します（[configuration.md](configuration.md) の「資料の検索」「会話履歴の保存先」）。
トークンは Worker の `DATA_API_TOKEN` に対応する値で、キーと同じく **Secret Manager から渡します。** Secret `d1-api-token` は 2026-11-07 に作成済みです
（作り方と移行の経緯は [cloudflare-migration.md](cloudflare-migration.md)）。新しい実行用サービスアカウントで使うときは、読み取りを許します。

```bash
as_me gcloud secrets add-iam-policy-binding d1-api-token \
  --member="serviceAccount:${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role=roles/secretmanager.secretAccessor --project "$PROJECT_ID"
```

デプロイのコマンドに、次を足します（A では `--set-env-vars` と並べて、B では `--set-secrets` の値をカンマでつなぎます）。

```bash
  --set-env-vars "D1_API_URL=https://docunavi-api.wataru-apps.workers.dev" \
  --set-secrets "D1_API_TOKEN=d1-api-token:latest"
```

- Worker に届かないと、**起動に失敗してリビジョンが Ready になりません**（メモリへは切り替わりません）。ログで原因を確かめます。
- 現在アクセスできるサービスアカウントは `docunavi-run`（API）、`docunavi-mcp`（MCP）、`docunavi-ingester`（取り込みと履歴削除のジョブ）です（2026-11-08 に確認）。
- 会話履歴の削除は、Cloud Run Job `docunavi-history-cleanup`（下の D）です。**2026-11-08 から `--apply` なし（件数の表示だけ）で動いていて、履歴は消えません。**

### D. 会話履歴の削除（Cloud Run Job + Cloud Scheduler。最初は 7 日）

**削除の日数は、最初は 7 日にします**（2026-10-20 に決定）。
方針の 90 日（`docs/configuration.md`）は変えておらず、様子を見て、下の `7` を書き換えて延ばします。
最後の動きから 7 日たった会話は消え、サイドバーの一覧からも消えます（戻せません）。実行は週 1 回なので、実際に消えるのは 7〜14 日後です。

**実行済みです（2026-10-20 JST、利用者が `as_me` で実行）。** B・C まで済んで、サービスが `/health` の `history` を保存先（当時は `postgres`、いまは `d1`）にして動いてからの手順です
（Cloud Run が始まる前は、Job も Scheduler も作れません。請求先アカウントと、上の準備 0・1 が前提です）。
Job と Scheduler の作成は書き込みなので、あなたが実行します（`as_me`）。

**実際に通った記録**（プロジェクト `docunavi-app`、Job `docunavi-history-cleanup`、Scheduler `docunavi-history-cleanup-weekly`）:

- Job を作り（`--apply` なし）、手動で 1 回実行 → ログに「対象の会話: 0 件」、終了コード 0（実行 `…-cr5hf`、04:43 JST）。公開して間もなく、古い会話が無かったため。
- `--apply` を足し、Scheduler 用のサービスアカウントを作って `run.invoker` を付け、Scheduler を作成（毎週日曜 03:00 JST。最初の定時は 2026-10-25）。
- `scheduler jobs run` で 1 回起こし、実行 `…-lx97q` が `history-cleanup-scheduler` の実行として 1 / 1 完了（04:47 JST）。Scheduler から Job を呼ぶ権限は足りている。
- **詰まった点:** サービスアカウントを作った直後の `run jobs add-iam-policy-binding` と `scheduler jobs create` が、「存在しない」「見つからない」で失敗した（IAM の反映待ち）。
  数十秒〜数分後に同じコマンドをやり直して通った。続けて実行するときは、作成の後に少し待つか、`service-accounts describe` が通るのを確かめてから進める。
- ログは、実行の完了から数十秒遅れて読めるようになる。空なら、`run jobs executions list` で完了を見る。

- 削除の実体は `python -m app.history_admin --older-than-days 7 --apply`（[configuration.md](configuration.md)）。同じ条件で何度実行しても、
  消える会話は「7 日以上動きのない会話」だけなので、再実行されても害はありません（一覧を取ってから消すまでに新しい発言が入った会話が消えうる点だけは、`history_admin` の説明のとおり）。
- 削除のために HTTP ルートは作りません（認証が無いため）。Job はアプリのイメージを使い、コマンドだけを上書きします。
- Job は `D1_API_URL` と `D1_API_TOKEN`（C の `d1-api-token`）だけを渡します。LLM のキーは要りません。データ用 Worker に届くことは、C の確認（`/health`）が先です。

```bash
# 1. Cloud Scheduler の API を有効にする（初回だけ）
as_me gcloud services enable cloudscheduler.googleapis.com --project "$PROJECT_ID"

# 2. サービスが今使っているイメージを調べる（読み取り）
IMAGE=$(gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --format='value(spec.template.spec.containers[0].image)')

# 3. Job を作る（コマンドを上書き。実行用 SA と Secret はサービスと同じ）
JOB=docunavi-history-cleanup
as_me gcloud run jobs create "$JOB" \
  --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" \
  --service-account "${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
  --set-env-vars "D1_API_URL=https://docunavi-api.wataru-apps.workers.dev" \
  --set-secrets "D1_API_TOKEN=d1-api-token:latest" \
  --command python \
  --args=-m,app.history_admin,--older-than-days,7 \
  --max-retries 1 --task-timeout 300
```

3 では、わざと `--apply` を付けていません（件数を表示するだけの Job）。最初の 1 回は手動で実行し、ログで件数を見てから `--apply` を足します。
**2026-11-08 の時点で、本番の Job は D1 に向けて、5 の `--apply` を足していない状態です**（イメージは `docunavi:37ef884`）。`--apply` を戻す時期は未決です。

```bash
# 4. 手動で 1 回実行する（件数を表示するだけ。--wait で終わるまで待つ）
as_me gcloud run jobs execute "$JOB" --project "$PROJECT_ID" --region "$REGION" --wait
#    ログ: 「対象の会話: N 件（削除はしていません。…）」。閲覧専用の ID でログを読めるかは未確認（読めなければ `as_me` を付ける）
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

- **動いたかの確認:** 7 の後、Scheduler の画面（または `as_me gcloud scheduler jobs run "${JOB}-weekly" --location "$REGION"`）で 1 回起こし、
  上の `logging read` で「削除した会話: N 件」を見ます。Scheduler から動いたかは、`run jobs executions list` の「RUN BY」が Scheduler 用のアカウントで、完了していれば分かります（2026-10-20 に確認済み。ただし、その実行の「削除した会話: N 件」の行そのものは読んでいません）。
- **イメージの更新:** Job のイメージは作った時点のものです。サービスをデプロイし直したら、`as_me gcloud run jobs update "$JOB" --image "$IMAGE" …` で合わせます
  （古いイメージでも、`history_admin` が変わらなければ動きます）。
- **確認済み:** Job の実行（イメージに `python` と `history_admin` が入って動く）、Scheduler から Job を呼ぶ権限、`--args` での `--apply` の追加（2026-10-20）。D1 に向けた Job の実行（件数 0 件の表示、`exit(0)`）は 2026-11-08 に確認した。
- **未確認:** 実際に会話が削除される動き。`--older-than-days --apply` で消えた実績は、D1 ではまだ無い（古い会話が無いため）。
- **失敗に気づく:** 失敗しても通知は出ません（Cloud Monitoring のアラートは未設定）。当面は、月に 1 度、`logging read` で最後の実行を見ます。

### 値の根拠

| フラグ | 値 | 理由 |
|---|---|---|
| `--max-instances` | 2 | 費用の上限。足りなくなったら、予算と相談して上げる |
| `--concurrency` | 20 | `20 × LLM_MAX_ATTEMPTS（3）= 60` が SDK の接続プール（既定 100）に収まる。既定の 80 だと 240 になる |
| `--timeout` | 90 | `LLM_TIMEOUT_SECONDS`（60）より長くする |

初回は、Artifact Registry の Docker リポジトリ（`cloud-run-source-deploy`）の作成を聞かれます。

### E. 公開前の守り（ロードマップ 2）

`--no-allow-unauthenticated` は Cloud Run の入口の認証（IAM）です。公開すると（`allUsers` に `roles/run.invoker`）、これが外れるので、
**アプリ側のログイン（`AUTH_MODE=required`）と、費用の上限を先に揃えます。** 上の A〜C のコマンドに、次を足します。
**実行済み（2026-10-20）。** 予算アラートの設定、`--max-instances 2`、`AUTH_MODE=required`、および `CORS_ORIGINS`（画面オリジン）の設定を適用して公開しました。

| 守り | 設定 | 備考 |
|---|---|---|
| 費用の上限 | `--max-instances 2`（上の A に入っている） | |
| 予算アラート | 下の `budgets create` | 通知だけで、使用は止まらない。上限そのものは `--max-instances` |
| ログイン | `AUTH_MODE=required` と `AUTH_BASE_URL`・`AUTH_AUDIENCE`（[configuration.md](configuration.md) の「ログイン（認証）」） | 起動時に JWKS を取得する。取れなければリビジョンが Ready にならない |
| CORS | `CORS_ORIGINS` | 画面は同じオリジンから配信されるので、CORS は要らない。**空**（何も許可しない）か、サービスの URL に絞る。`*` のままにしない |

```bash
# 予算アラート（請求先アカウントの管理者の権限。as_me で実行）。金額としきい値は例
as_me gcloud billing budgets create --billing-account=<請求先アカウント ID> \
  --display-name="docunavi" --budget-amount=<金額。請求先アカウントの通貨で> \
  --filter-projects="projects/${PROJECT_ID}" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

デプロイのコマンドの `--set-env-vars` に足す（`AUTH_BASE_URL` は画面の Worker のオリジンで、公開してよい値）:

```bash
  AUTH_MODE=required,AUTH_BASE_URL=<画面（Worker `docunavi-web`）のオリジン>,AUTH_AUDIENCE=docunavi,CORS_ORIGINS=<画面（Worker `docunavi-web`）のオリジン。例: https://docunavi-web.wataru-apps.workers.dev>
```

**未対応（公開の前に要る）:**

- **画面のログインは、画面側（`frontend`）の環境変数で有効にする（2026-10 の当時は Vercel の `NEXT_PUBLIC_SUPABASE_*`。2026-11-08 からは Worker のビルドで `NEXT_PUBLIC_AUTH_ENABLED=1`、認証は Better Auth）。** Cloud Run のイメージに画面の設定は入らない（2026-10-24 から。画面は Cloud Run が配信しない）。画面にログイン設定が無いと、`AUTH_MODE=required` では質問できない（`/chat/stream` が 401）。
- トークンの期限切れ後の更新は未確認。
- 課金の有効化は、このセッションでは確認できなかった（gcloud にログインしていない）。

### F. イメージのビルド（`cloudbuild.yaml`）

イメージは API だけです（画面は Worker `docunavi-web`（`frontend`）が配信するので、ビルド引数も画面の設定もありません。2026-10-24 から）。
`--source .` でも同じイメージができますが、Artifact Registry に置くイメージの名前（コミットの短縮 SHA）を決めるために、`cloudbuild.yaml` を使います（`scripts/preview.sh build` も同じ）。
配備は、できたイメージを `--image` で行います。
**Cloud Build での実行は確かめました（2026-10-20。約 1 分半で SUCCESS。この時点のイメージは、Vite の画面の設定を埋め込んでいた）。画面を外した今のイメージを Cloud Build でビルドして配備した記録は、まだありません（`cloudbuild.yaml` の内容は `tests/test_cloudbuild.py` が確かめる）。**
開発コンテナには Docker が無く、`docker build` はローカルでは実行していません。

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

配備は、A のコマンドの `--source .` を `--image "$IMAGE"` に置き換えます（ほかのフラグはそのまま）。`agent-deployer` でできるかは未確認です
（`roles/run.sourceDeveloper` はソースからの配備用で、`--image` の配備や、ビルドの権限が足りない可能性があります。エラーに出た権限を足します）。

- イメージのタグはコミットごとに変えます（`:latest` にしない）。ロールバックで、前のイメージを指定できるようにするためです。
- `.dockerignore` が `.env` を除いているので、ビルドコンテキストに秘密は入りません。

### G. 予算の自動停止（課金を紐づけるときに、デプロイより先に）

**実行済み（2026-10-20）。** 関数は `ops/budget_guard/main.py`。2026-10-20 に配線と動作確認（`DRY_RUN=false`）を実施しました。
仕組みは、予算 → Pub/Sub → 関数 → プロジェクトの課金を無効化、という公式の方法です（Cloud Billing の「Disable billing with notifications」）。
**「無料枠を超えそうになったら止める」ことはできません。** 予算は金額で、Gemini の API キーは同じプロジェクトにあり、課金を紐づけると有料枠に切り替わる可能性があります
（公式の料金ページで確認）。実際は「月の費用が予算額に届いたら止める」です。金額は仮で、あとで変えられます。

**注意**

- 課金を無効にすると、**プロジェクトのサービスがすべて止まり、リソースは消えることがあります**（公式の警告。猶予の期間は書かれていません）。復旧は手動で、元に戻る保証はありません。
- 予算の通知は費用の反映より遅れます。**上限は厳密ではなく、止まるまでに予算を超えます。**
- API の有効化（Cloud Functions など）に課金が要るので、1 の直後から 4 までは続けて行います。その間は上限が働いていません。

```bash
BILLING_ACCOUNT=<請求先アカウント ID>
BUDGET=3000                        # 2026-11-01 に 1000 から 3000 に変更。請求先アカウントの通貨で指定する（円なら 3000 円）
TOPIC=budget-alerts
GUARD_SA=budget-guard
```

1. **課金アカウントを紐づける**（Console、またはあなたの権限で）

   ```bash
   as_me gcloud billing projects link "$PROJECT_ID" --billing-account="$BILLING_ACCOUNT"
   ```

2. **Pub/Sub のトピックと予算を作る**（アラートは 50%・90%、停止の判断は関数が 100% で行う）

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

   予算額は、**請求先アカウントの通貨**で指定する（`5USD` のように別の通貨を付けると `INVALID_ARGUMENT` になった。2026-10-20、円のアカウントで、`5USD` は失敗し、`1000`（円）で作成できた）。通貨を付けなければ、アカウントの通貨になる。関数は通貨を見ず、数値だけを比べる。
   `--billing-project` は、予算の API の課金先（クォータプロジェクト）の指定で、無いと `SERVICE_DISABLED` で失敗する（gcloud が用意する別のプロジェクトを使うため）。
   `--credit-types-treatment=exclude-all-credits` は、クレジットを引く前の費用で数える指定です。既定（全クレジットを含める）だと、
   無料トライアルのクレジットが残っている間は費用が 0 のままで、予算に届かず、自動停止が働きません。

3. **関数を作る**（最初は `DRY_RUN=true`。止める判断をログに出すだけで、課金は変えない）

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

4. **SA の権限を付ける**

   ```bash
   as_me gcloud billing accounts add-iam-policy-binding "$BILLING_ACCOUNT" \
     --member="serviceAccount:${GUARD_SA}@${PROJECT_ID}.iam.gserviceaccount.com" --role=roles/billing.admin
   ```

   `roles/billing.admin`（Billing Account Administrator）は、公式の手順が使う役割です。**必要より広く**（請求先アカウントの権限の管理もできる）、
   公式は最小の役割を示していません。狭い役割（カスタムロール）は、必要な権限を確かめてからの、あとの課題です。この SA は、この関数だけに使います。

   続けて、トリガーが関数を呼べるようにする。**これが無いと、通知は関数に届かず**、ログに「The request was not authenticated … lacks {run.routes.invoke}」と出る
   （2026-10-20、`--service-account` を指定してデプロイしたときに、自動では付かなかった）。
   付けてから効くまで 1〜3 分かかる（付けた直後の通知は同じエラーで失敗し、3 分後の再送で「DRY_RUN: would disable billing」が出た）。

   ```bash
   as_me gcloud run services add-iam-policy-binding budget-guard --project "$PROJECT_ID" --region "$REGION" \
     --member="serviceAccount:${GUARD_SA}@${PROJECT_ID}.iam.gserviceaccount.com" --role=roles/run.invoker
   ```

5. **動作を確かめる**（`DRY_RUN=true` のまま）

   ```bash
   as_me gcloud pubsub topics publish "$TOPIC" --project "$PROJECT_ID" --message '{"costAmount": 6, "budgetAmount": 5}'
   gcloud functions logs read budget-guard --gen2 --project "$PROJECT_ID" --region "$REGION" --limit 20   # 「DRY_RUN: would disable billing」
   ```

   これで確かめられるのは、トリガーと判断までです。**課金を実際に外せる権限があるかは、`DRY_RUN=false` で試すまで確かめられません。**
   2026-10-20 に、サービスを置く前の 公開用のプロジェクト で試した。`roles/billing.admin`（請求先アカウント）だけで、課金は外れた
   （`billingEnabled: false`、ログ「billing disabled for the project」）。ただし、最初の版は外す前に `get_project_billing_info` を呼び、
   プロジェクトの閲覧権限が無くて 403 になった（今は呼ばない）。試験で外れたら、手順 6 の復旧で戻す。本番に切り替える:

   ```bash
   as_me gcloud functions deploy budget-guard --gen2 --runtime python312 --project "$PROJECT_ID" --region "$REGION" \
     --source ops/budget_guard --entry-point stop_billing --trigger-topic "$TOPIC" \
     --service-account "${GUARD_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --update-env-vars "DRY_RUN=false"
   ```

   以後は、**上の Pub/Sub への手動の publish をしない**（本物の停止になる）。

6. **止まったあとの復旧**

   - まず、原因（想定外の使用、攻撃、設定の誤り）を確かめる。同じ月に予算を超えたままだと、再び紐づけた直後に止まる。先に `DRY_RUN=true` に戻すか、予算額を上げる。
   - 課金を紐づけ直す（`as_me gcloud billing projects link …`、または Console）。
   - API の有効化・Cloud Run のサービス・Secret が残っているかを確かめ、無ければ作り直す（消えることがある）。

デプロイ（上の A〜F）は、ここまで済んでから行います。

## 確かめる

非公開（`--no-allow-unauthenticated`）の間は、呼び出しの権限（`roles/run.invoker`）が要ります。閲覧専用の ID には無いため、
あなた自身の権限（`as_me`）で実行します。**ID トークン付きの `curl` が、いちばん簡単です**（2026-10-20 に通った方法）。

```bash
URL=$(as_me gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='value(status.url)')
TOKEN=$(as_me gcloud auth print-identity-token)
curl -s -H "Authorization: Bearer $TOKEN" "$URL/health"   # {"status":"ok","mode":"gemini","history":"d1"} なら本物の LLM と D1 に繋がっている
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $TOKEN" "$URL/"   # 200
unset TOKEN
```

公開した後は、トークン無しで `curl -s -o /dev/null -w '%{http_code}\n' "$URL/"`（200）と、
`curl -s -o /dev/null -w '%{http_code}\n' -X POST -H 'Content-Type: application/json' -d '{"message":"test","thread_id":"t"}' "$URL/chat"`（ログイン無しなので 401）を確かめます。

`gcloud run services proxy` でも橋渡しできますが、**バックグラウンド（`&`）で動かすと、端末の入力を待って止まります**（`Stopped`、`curl` は 000）。
使うなら、別の端末のフォアグラウンドで動かします。

```bash
as_me gcloud run services proxy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --port 8080
# 別のターミナルで
curl -s localhost:8080/health
curl -s localhost:8080/chat -H 'Content-Type: application/json' -d '{"message":"ザーハードの大気について教えて"}'
```

- `mode` が `mock` なら、認証の設定が届いていません。
- `history` が `memory` なら、`D1_API_URL` が届いていません（C を使う場合）。
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

## タグ付きプレビュー（本番に流す前に、画面を専用 URL で確かめる）

**実行済み（2026-10-21）。** 手順 1〜4・7 は実際に通った（記録は節の末尾の「実際に通った記録（2026-10-21）」）。
手順 5（ログインの戻り先）と、タグ付き URL でのログインの戻り（手順 6 の前半）は、確かめていない（下の「注意と未確認」）。
フラグの名前は `gcloud run deploy --help` と `gcloud run services update-traffic --help` で確かめた。

書き込みの 4 つ（ビルド・配備・昇格・ロールバック）は、`scripts/preview.sh`（`build` / `deploy <コミット> [<タグ>]` / `promote <タグ>` / `rollback <リビジョン>`）にまとめてある。
あなたが実行する（私は実行しない）。`PREVIEW_DRY_RUN=1` を付けると、実行せずにコマンドだけ表示する。下の手順は、その中身。

新しいリビジョンを 0% で作り、タグ付きの URL でだけ届くようにします。本番のトラフィックは変わりません。
[r2-rollout.md](r2-rollout.md) の `r2-wire-stage` / `promote` / `rollback`（Job）は、R2 の環境変数とボリュームを書き換える専用なので、**この手順では使いません。**
書き込みはすべてあなたが `as_me` で実行します。私が実行するのは、読み取り（`describe`、`curl`）だけです。

```bash
PROJECT_ID=docunavi-app; REGION=asia-northeast1; SERVICE=docunavi
TAG="preview-$(date +%Y%m%d-%H%M)"
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}:$(git rev-parse --short HEAD)"
```

**`IMAGE` のコミットは、ビルドしたものに合わせる。** ビルドのあとに別のコミットを重ねると、`HEAD` は進み、`$(git rev-parse --short HEAD)` はビルドしていないイメージを指す
（2026-10-21 は、ビルドが `dc969b9`、`HEAD` が `3d3ab79` になり、`dc969b9` を明示した）。`scripts/preview.sh` は、`build` で HEAD と一致しないコミットを拒否し、`deploy` はコミットを引数で受ける。

タグは、小文字・数字・ハイフンの短い名前で、重複しないものにします（日時を入れる）。使える文字数の上限は確かめていません。

### 1. 本番のリビジョン名を控える（読み取り）

ロールバック先です。控えてから先へ進みます。

```bash
gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='yaml(status.traffic)'
```

`percent: 100` のリビジョン名を `PROD_REV` として控える。

### 2. イメージをビルドする（あなた）

「実際に通った記録」の F と同じ（2026-10-24 から、画面の設定は渡さない）。
**`as_me` を付ける。** 日常の ID（閲覧専用の `docunavi-viewer`）では、ソースを置くバケット `docunavi-app_cloudbuild` に書けず 403 になる（2026-10-21）。

```bash
as_me gcloud builds submit . --project "$PROJECT_ID" --config cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE}"
```

### 3. 0% でデプロイし、タグを付ける（あなた）

```bash
as_me gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" --no-traffic --tag "$TAG"
```

- **`--set-env-vars` と `--set-secrets` を付けない。** 付けると、既存の Secret の参照（`GEMINI_API_KEY_PAID` `D1_API_TOKEN` など）が置き換わって消える。イメージだけを変える。
- `--no-traffic` は、デプロイ前に `LATEST` へ向いていたトラフィックを、そのときの具体的なリビジョンに固定してから新しいリビジョンを作る（`--help` の記述）。
  このサービスは、すでにリビジョン名で固定されている（`docunavi-00005-zl7`）ので、変わらない。以後の `LATEST` はトラフィックを受けない。

### 4. 0% とタグ付き URL を確かめる（私）

```bash
gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='yaml(status.traffic)'
```

見ること: 新しいリビジョンが `tag: <TAG>` と `url: https://<TAG>---…` を持ち、`percent` が付いていない（0%）こと。`PROD_REV` が `percent: 100` のままであること。
続けて、タグ付き URL の `/health`（`mode` が `gemini`、`history` が `d1`）。

```bash
curl -s "https://${TAG}---<describe に出た、タグを除いたホスト名>/health"
```

サービスが公開（`allUsers`）なので、タグ付き URL も認証なしで届く（2026-10-21 に確認。`/` と `/health` が 200）。
あわせて、新しいリビジョンの環境変数の名前と Secret の参照が、本番と同じであることを確かめる（`gcloud run revisions describe <リビジョン> --format=json` から、`env` の名前と `valueFrom` の有無だけを取り出す。値は出さない）。

### 5. ログインの戻り先（2026-11-08 に Supabase を廃止。手順は未整備）

旧環境では、Supabase の Redirect URLs にタグ付き URL のワイルドカードを足していました。Better Auth では、その設定は不要になりました。
タグ付き URL（Cloud Run のプレビュー）でログインが戻るかは、**確かめていません**（画面の本番は Worker `docunavi-web` で、Cloud Run の API にはログインの戻り先がありません）。
Cloud Run のタグ付き URL は API の確認用で、ログイン込みの画面の確認は Worker 側で行います。

### 6. 画面で確かめる（あなた）

タグ付き URL をブラウザーで開く。

- Google ログインがタグ付き URL に戻り、質問が 1 つ通る。
- **SSE:**
  - 回答が逐次表示される（最初の本文までは 22〜31 秒かかることがある。[roadmap.md](roadmap.md)。そのあとは約 1.3 秒で流れる）。
  - 回答の途中でタブを閉じる・停止すると、サーバーが続きを取り残さない（ログに `499` 相当の INFO。ERROR にならない）。
  - 90 秒（`--timeout`）を超える質問で、`error` チャンクの固定メッセージになり、画面が固まらない。
- 修正した画面（今回変えた箇所）の見た目と動き。

ログ（私が読む）: `gcloud run services logs read "$SERVICE" --project "$PROJECT_ID" --region "$REGION"`。

### 7. 本番へ移す（承認してから。あなた）

```bash
as_me gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --to-tags "$TAG=100"
```

このタグのリビジョンが、本番の 100% を受け持つ。移したあと、私が `describe` と本番の `/health` を読む。

**移したあとの状態（2026-10-21 に確認）:** 100% は、そのタグのリビジョン（`status.traffic` に `tag` 付きで残る）に固定される。`LATEST` は受けない。
以前のリビジョン（`r2prev` `r2new`）は 0% でタグ付きのまま残る。次に配備するときは、先に `describe` で `status.traffic` を読み、
**通常の（`--no-traffic` なしの）`gcloud run deploy` や `services update` が、新しいリビジョンへ 100% を移すか**を確かめてから行う（未確認）。
`LATEST` へ戻すなら `as_me gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --to-latest`（未実行）。
環境変数だけを足す変更も、`services update … --no-traffic --tag <タグ>` で 0% の確認用リビジョンを作ってから、同じ手順 7 で移せる。

### 8. ロールバック（あなた）

手順 1 で控えたリビジョンへ戻す。数秒で済み、設定は消えない。

```bash
as_me gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --to-revisions="<PROD_REV>=100"
```

### 9. タグを外す（不要になったら。あなた）

```bash
as_me gcloud run services update-traffic "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --remove-tags "$TAG"
```

オプション名は `--remove-tags`（複数形。カンマ区切りで複数のタグを外せる）。リビジョンそのものは消えない。

### 注意と未確認

- **プレビューも、本番の DB・Gemini のキー・Secret を使う。** 確認で送った質問は、本番の会話履歴に残る（7 日の掃除で消える）。DB を分ける仕組みは無い。
- サービスが公開なので、タグ付き URL の入口も公開。質問は `AUTH_MODE=required` で守られる。
- 古いタグ `r2new` `r2prev` が残っている。この手順では触らない（外すかは別に決める）。
  [r2-rollout.md](r2-rollout.md) の「タグの URL ではログインが通らない」は、手順 5 の登録をする前の状態の記述。
- 未確認: `agent-deployer` での `--image` のデプロイ（この手順は `as_me` 前提）/ 手動で割り振ったトラフィックが、通常の（`--no-traffic` なしの）デプロイで変わるか
  （この手順は `--no-traffic` を明示するので影響しない）/ Supabase のワイルドカードで、ログインがタグ付き URL に戻ること（2026-10-21 は、タグ付き URL でのログインをせず、昇格のあとに本番の URL で確かめた）/ タグの名前の長さの上限。
  確認済み: タグ付き URL が認証なしで届くこと（2026-10-21）。
- **Gemini の 503（`This model is currently experiencing high demand`）で質問が 502 になることがある。** 新しいリビジョンの不具合ではない（2026-10-21、昇格の直後に 1 回。数分あけて再送したら 200 で 14 秒）。
  429 ではないので有料キーへの切り替えは働かず、この時点では `GEMINI_FALLBACK_MODEL` が本番で未設定だったため、別モデルにも切り替わらなかった。
  同じ日のあとで有効にした（リビジョン `docunavi-00011-vuy`。[configuration.md](configuration.md) の「検証の状況」）。
  確認で 502 が出たら、まずログのコンテナログの例外（`503 UNAVAILABLE` か）を見て、アプリの問題と切り分ける。

### 実際に通った記録（2026-10-21）

時刻は JST。サービス `docunavi`。

| 時刻 | 内容 |
|---|---|
| 21:08 | ビルド `docunavi:dc969b9`（`gcloud builds submit`、1 分 33 秒、成功）。最初は `as_me` なしで 403、付けて成功 |
| 21:14 | 本番（100%）は `docunavi-00005-zl7`（タグ `r2new`）。`--no-traffic --tag preview-20261021-2114` で配備 → `docunavi-00009-voj`（0%） |
| 21:15 | 確認: 00009 は 0%、本番は 00005 のまま。`/health` 200（`gemini` `postgres`）。環境変数の名前 10 個・Secret 参照 3 つ・ボリューム・実行 SA が 00005 と同じ。イメージのダイジェストはビルドと一致 |
| 21:17 | `update-traffic --to-tags preview-20261021-2114=100` → 100% が 00009 に移った。本番の `/health` 200 |
| 21:30 | 本番の画面（`docunavi-<プロジェクト番号>.asia-northeast1.run.app`）にログイン（`/threads` 200） |
| 21:37 | 質問 1 回目: 502（37.5 秒）。原因は Gemini の `503 UNAVAILABLE`（高負荷）。アプリは固定メッセージを返した |
| 21:38 | 資料一覧から元の PDF を開いた（`/documents/2/file` 200、0.28 秒。R2 のエラーログ 0 件）。本番が R2 から PDF を返すことの確認 |
| 21:41 | 質問を再送 → `/chat/stream` 200（14.1 秒）。WARNING 以上のログは 0 件 |

確認用の会話（21:37 と 21:41 の 2 件）は、画面から削除する（SQL では消さない。チェックポイントのテーブルが相互に関わるため）。

## 古いリビジョンとイメージの整理（2026-11-14 に実施）

- **リビジョン**: 戻し先として、本番と、その前の 2 つ（2026-11-14 時点で `docunavi-00061-sir` `00057-suj` `00056-pal`）だけを残す。0% のリビジョンに料金はかからないが、古いものは DB・認証・環境変数が変わっていて戻し先にならず、一覧が長くなる。
  配備のたびに増えるので、タグを外す（上の「9. タグを外す」）ときに、不要なものを `gcloud run revisions delete` で消す（元に戻せないので、一覧と名前を確かめてから）。
  2026-11-14 に、タグ 21 個を外し、リビジョン 31 個を消した。消した後も、本番の `/health` は 200、100% は `00061-sir` のまま（証拠等級 A）。
- **イメージ（Artifact Registry）**: 保管料がかかるので、クリーンアップポリシーで自動で消す。定義は [ops/artifact-registry/cleanup-policy.json](../ops/artifact-registry/cleanup-policy.json)。
  - `docunavi` のイメージは最新 5 個を残し、それより古いものを消す。5 個にしたのは、戻し先の 3 つのイメージを、配備が増えても守る余裕のため。
  - `docunavi-admin` `docunavi-ingest` `docunavi-mcp` など、`docunavi-` で始まる別のパッケージは触らない。パッケージ名の指定は前方一致で、`docunavi` だけでは、これらにも消す条件が当たるため。
  - **使用中のイメージは `pin-` タグで守る（2026-11-15 に追加。`keep-pinned`）。** 「最新 5 個」は本番と Job が同じ枠を取り合うため、Job のイメージは配備が数回進むと枠外になり、消える（`docunavi-history-cleanup` が一度 `ContainerMissing` になった原因）。タグ `pin-*` が付いた `docunavi` のイメージは、枠外でも残る。
    - 付けたタグ: `pin-prod`（本番・戻し先 `00061-sir`）、`pin-history-cleanup`（`e34b160c…`）、`pin-ingest-sync`（`01bd3e9c…`）、`pin-ingest-queue`（`9255400e…`）。
    - **本番または Job のイメージを更新したら、そのたびにタグを付け替える**（古いイメージは枠外になれば自動で消える）:
      ```bash
      R=asia-northeast1-docker.pkg.dev/docunavi-app/cloud-run-source-deploy/docunavi
      gcloud artifacts docker tags add $R@sha256:<新しいダイジェスト> $R:pin-<名前>
      ```
      `tags add` は `--project` `--location` を受け付けない（URL に含まれる）。
    - 更新のあとに、参照が残っているか確かめる（`gcloud run jobs list` と `gcloud run revisions list` のイメージを、`gcloud artifacts docker images describe` で 1 つずつ）。タグの付け忘れは、これで気づく。
    - `docunavi-` で始まる別パッケージ、`pg-backup`、`r2-key-issuer`、`r2-wire` は、このポリシーの対象外か別枠で、影響を受けない。
    - **未確認:** `keep-pinned` が実際に削除を防ぐこと（次の削除の実行後、`docunavi` が 5 個 + 枠外のタグ付きになり、`pin-` のイメージが残るかで確かめる）。`r2-verify` の `docunavi:60833f5` は 2026-11-15 時点で消えていて、Job は動かない（未対処。差し替えるかどうかは判断待ち）。
  - 設定するとき:
    ```bash
    gcloud artifacts repositories set-cleanup-policies cloud-run-source-deploy \
      --project docunavi-app --location asia-northeast1 \
      --policy=ops/artifact-registry/cleanup-policy.json --no-dry-run
    ```
    `--dry-run` は、設定の書き込みで、削除はしない。結果は Data Access ログに出るが、このプロジェクトでは有効にしていない（`auditConfigs` が空。2026-11-14）ので、結果は見られない。
  - 2026-11-14 に、`--dry-run` を設定してから、実際のポリシー（`--no-dry-run`）に切り替えた。消えるのは、`docunavi` の古い 20 個（25 個のうち、最新 5 個の外）の見込み。
  - **確認済み（2026-11-15 0 時台 JST。証拠等級 A）:** `docunavi` のイメージは 25 個から 6 個になり（最新 5 個 + 新しい配備の 1 個）、他のサービスのイメージは残った。**未確認:** 6 個が 5 個に収まること（次の日次の実行後に確かめる）。
  - 消えたイメージは戻せない。必要なら、そのコミットから再ビルドする。リビジョンが使っているイメージを消すと、そのリビジョンは新しいインスタンスを起動できなくなる（戻し先は、最新 5 個の中に収まるように保つ）。

## タグ push で 0% 配備する（Cloud Build トリガー）

**状態（2026-11-14）: サービスアカウント `docunavi-build`、GitHub の接続 `docunavi-github`（2nd gen。`asia-northeast1`）、リポジトリの登録、トリガー `docunavi-tag-deploy`（承認必須）を作成し、2 回目のタグ push で 0% の配備まで通った（下の「限界と未確認」）。**
`deploy/*` のタグを push したときだけ、Cloud Build がビルドして `--no-traffic --tag` で配備する（`cloudbuild-deploy.yaml`）。
役割の分け方は、`main` への push はテストだけ（2026-11-08 までは Vercel の `frontend` も更新された。いまの画面は手動の `deploy.yml`）、Cloud Run はタグ push だけ。**本番のトラフィックは自動では変えない**（100% への切り替えは `./scripts/preview.sh promote`、戻すのは `rollback`。あなたが実行）。

- タグ名は `deploy/<Cloud Run のタグ名>`（例: `deploy/h2c-20261024` → タグ `h2c-20261024`）。形が違えば、ビルドの前に止まる（検査のシェルは、手元で 7 通りの入力を確認済み）。
- 配備は `--set-env-vars` `--set-secrets` `--use-http2` を付けない。環境変数・Secret・ポート名（`http1` か `h2c`）は、サービスの最新の設定を引き継ぐ。
  h2c にするのは、`--use-http2` を付けた手動の配備（`preview.sh` に口は無い）を 1 回通したあとで、以後はこの経路でも引き継がれる。
- タグを切ったコミットがそのまま配備される。`main` にある・CI が通っていることは、トリガーは確かめない（0% なので本番には出ない）。
- 料金は、タグを push した回数分の Cloud Build だけ。

### 準備（あなた。一度だけ。書き込み。2026-11-14 に実施済み）

```bash
# PROJECT_ID・REGION・SERVICE・RUN_SA・as_me は「準備」で定義したもの。リポジトリは REPO=cloud-run-source-deploy
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

# 4. タグ push のトリガー。2nd gen なので --repository（--repo-owner / --repo-name ではない）と --region を使う。
#    置換変数は渡さない（イメージは API だけ）。--require-approval で、ビルドの開始前に Console で承認する
as_me gcloud builds triggers create github --project "$PROJECT_ID" --region "$REGION" --name=docunavi-tag-deploy \
  --repository="projects/${PROJECT_ID}/locations/${REGION}/connections/docunavi-github/repositories/docunavi" \
  --tag-pattern='^deploy/.*$' --build-config=cloudbuild-deploy.yaml --require-approval \
  --service-account="projects/${PROJECT_ID}/serviceAccounts/${BUILD_SA}"
```

### 使うとき

```bash
git tag deploy/h2c-20261024 && git push origin deploy/h2c-20261024   # push は承認を得てから
```

私は読み取りで確かめる: `gcloud builds list --billing-project=docunavi-app --project docunavi-app`（ビルドの成否）と、
`gcloud run services describe docunavi … --format='yaml(status.traffic)'`（新しいリビジョンが 0% で、本番が 100% のまま）。

配備のあと、最後のステップ `prune-old-revisions` が古いリビジョンを消す（2026-11-14 に追加）。
新しい順に 3 つと、トラフィックが向いている・タグが付いたリビジョンは残す（タグが付いた古いものは、タグを外すまで残る）。それ以外を `gcloud run revisions delete` で消す。
消せなくても警告だけで、ビルドは成功のまま。**実際のビルドで動いた（2026-11-15。証拠等級 A）:** タグ `deploy/prune-check-20261115` のビルド `f3192c40` で、`docunavi-00057-suj` と `docunavi-00056-pal` を削除した（ビルド用 SA の `run.revisions.delete` の権限は足りる）。
手元で、削除を `echo` に置き換え、実際のサービスの情報で保護の判定だけを試した（残す数 1 で、`00057-suj` `00056-pal` が「トラフィックまたはタグあり」として残った。等級 A、読み取りのみ）。
ビルド用のサービスアカウントが、リビジョンの削除を許されるかは、実際に動かすまで分からない（リビジョンが 4 個以上になる配備で確かめる）。

### 限界と未確認

- **0% が保証されるのは `cloudbuild-deploy.yaml` の中身による。IAM ではない。** `roles/run.developer` は `update-traffic` も含むので、権限だけでは配備と切り替えを分けられない。ファイルを変えるときは、`--no-traffic` が残ることを確かめる。
- タグを push できる人（リポジトリの書き込み権限を持つ人）は、トリガーを起動できる。GitHub のタグ保護（ルールセット）は未設定。
- 確認済み（2026-11-15。証拠等級 A: 実機）:
  - 誤ったタグ名での停止: `deploy/Bad_Check`（大文字と `_`）を push すると、トリガーは起動して承認待ちになり、承認すると `check-tag` が「タグ名は deploy/<…> にしてください」で失敗した（ビルド `b20df98d`）。`build` 以降は QUEUED のまま動かず、リビジョンは増えなかった。タグは同日に消した。
  - トリガーで配備したリビジョンへの直接のリクエスト: `docunavi-00068-lab`（0%）に Cloud Run のタグ `direct-check` を付けたタグ付き URL の `/health` が 200（`mode: gemini`、`history: pg`。初回は約 10.6 秒で、`minScale: 0` のコールドスタート）。本番は 100% `docunavi-00061-sir` のまま。タグは同日に外した。
  - 承認待ちのビルドの取り消し: `gcloud alpha builds reject` は地域指定のビルドを扱えない（`--location` が無く、フル名も受け付けない）。承認待ちを取り消すなら `gcloud builds cancel <ID> --region <リージョン>`（未試行）。
- 確認済み（2026-11-14。証拠等級 A: 実機）:
  - 2nd gen の接続・リポジトリ・トリガーの作成コマンドが通る（接続は `COMPLETE`、トリガーは `approvalRequired: true`）。
  - タグの push でトリガーが起動し、`TAG_NAME` `SHORT_SHA` が入る。`cloud-sdk:slim` を入手できる。上の最小権限（4 つ）で、ビルド・push・`--no-traffic --tag` の配備まで通る。
  - 承認は `gcloud alpha builds approve <ID> --location <リージョン>` でできる（`beta` は地域指定に対応しない）。ただし、エージェントが承認できるので、`--require-approval` は人の目を通す関門ではなく、誤タグへの安全策にとどまる。
  - **1 回目（`deploy/trigger-check-20261114`、ビルド `36457540`）は `check-tag` で失敗した**（配備には進まず、本番に影響なし）。置換変数はシェルの環境変数にならず、`$${TAG_NAME}` が空だった。`check-tag` に `env: TAG_NAME=${TAG_NAME}` を足して直した（`1508883`）。
  - 2 回目（`deploy/trigger-check2-20261114`、ビルド `67773db8`）は 4 ステップとも成功した。承認から約 2 分。リビジョン `docunavi-00059-voy` が 0% で配備され、本番の `docunavi-00057-suj` が 100% のままだった。試験のタグ（Cloud Run の `trigger-check2-20261114`、Git の 2 つ）は同日に消した。リビジョン `docunavi-00059-voy` も、同日に消した（タグなし、0% だった）。
  - ポート名 `h2c` は、トリガーの配備でも引き継がれた（`docunavi-00059-voy` のポート名が `h2c`）。
- HTTP の startup プローブ（2026-11-14。証拠等級 A: 実機、1 回）: 本番のサービスは startup が TCP（ポート 8080）で、liveness は無い。
  手動で `gcloud run deploy --no-traffic --tag probe-check-20261114 --startup-probe httpGet.path=/health,httpGet.port=8080,periodSeconds=10,timeoutSeconds=5,failureThreshold=24` を実行した（リビジョン `docunavi-00061-sir`）。
  ポート名が `h2c` のままでも、HTTP/1 の `/health` のプローブは通り、約 10 秒で正常になった。タグ付き URL の `/health` は 200（`mode: gemini`、`history: pg`）。0% で、本番は 100% のまま。
  Cloud Run には readiness プローブが無い（公式、等級 B）。liveness は入れない（`minScale: 0` で、再起動が必要な固まり方も起きていない。プローブの失敗が続くと再起動を繰り返す恐れだけが残る）。
  プローブは、`--startup-probe` を付けない以後の配備にも、サービスの最新の設定として引き継がれる見込み（gcloud の挙動からの推測。未確認）。
  **2026-11-14 に、`--to-tags probe-check-20261114=100` で本番に載せた**（100% が `docunavi-00057-suj` から `docunavi-00061-sir` へ。利用者の承認）。切り替えの前後で、タグ付き URL と本番の URL の `/health` は 200（`mode: gemini`、`history: pg`）。サービスの startup は HTTP の `/health`、ポート名は `h2c` のまま。
  戻し方: `gcloud run services update-traffic docunavi --project docunavi-app --region asia-northeast1 --to-revisions=docunavi-00057-suj=100`。画面での質問の確認は、していない。
  タグ `probe-check-20261114` は、同日に外した（`--remove-tags`。100% は `docunavi-00061-sir` のまま、本番の `/health` は 200）。
- `agent-deployer`（この文書の別の節）とは別。ここでは使わない。

## 実際に通った記録（2026-10-20）

> 2026-10-20 の初回公開の記録です（当時は Supabase の Postgres・Supabase Auth を使っていました。2026-11-08 に D1 + Better Auth へ移行）。日付つきの記録として残し、書き換えません。現在の手順は上の C・D です。

上の節を、この順に `as_me`（あなた自身の権限）で実行して、公開まで通りました。ID（プロジェクト番号など）は書きません。

**現状:** サービス `docunavi`（`asia-northeast1`、リビジョン `docunavi-00002-…`）、イメージのタグはコミットの短いハッシュ（`cc5709c`）、公開済み（`allUsers` に `roles/run.invoker`）。
確かめたこと: `/health` が `mode: gemini`・`history: postgres`、公開後の `/` が 200、ログイン無しの `/chat` が 401、ブラウザーで Google ログインと質問が通る。

**順序:**

1. G の予算ガード（先に）。
2. API の有効化（`run` `cloudbuild` `artifactregistry` `secretmanager`）と、実行用サービスアカウント。
3. Secret を 2 つ: `gemini-api-key`（B）、`database-url`（C。プーラーの URL）。実行用サービスアカウントに `roles/secretmanager.secretAccessor`。
4. F のイメージのビルド。
5. 非公開でデプロイ。環境変数と Secret は次のとおり。

   ```bash
   as_me gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
     --image "$IMAGE" --service-account "${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --no-allow-unauthenticated --max-instances 2 --concurrency 20 --timeout 90 \
     --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,DATABASE_URL=database-url:latest" \
     --set-env-vars "AUTH_MODE=required,SUPABASE_URL=${SUPABASE_URL},CORS_ORIGINS=${WEB_ORIGIN}"
   ```

   `AUTH_MODE` と `SUPABASE_URL` は `.env` ではなく、このフラグで渡します。`CORS_ORIGINS` は画面（Worker `docunavi-web`）のオリジン（`WEB_ORIGIN`。画面と API は別オリジンなので、空や `*` のままにしない。本番は設定済み）。
6. 「確かめる」の `curl`（ID トークン付き）。
7. 公開（「公開する・戻す」）。
8. **Supabase の Redirect URLs に、サービスの URL を足す**（画面のみ。MCP と SQL では変えられない）。Authentication → URL Configuration → Redirect URLs に `<サービスの URL>/**`。
   ローカル用の `http://localhost:3000` は残す（2026-10-24 までは Vite の `http://localhost:5173` だった）。画面は `redirectTo` に `window.location.origin` を渡す（`frontend/src/auth/supabase.ts`）ので、Site URL が `localhost` のままでもログインは Cloud Run に戻る。
   メールの確認リンクやパスワード再設定を使い始めるとき、または本番を主に使うときに、Site URL を本番の URL に変える（2026-10-20 に変えた。変更後、本番とローカル（`http://localhost:5173`）のログインと質問が通った。利用者の報告）。
9. ブラウザーで Google ログインと質問を 1 つ。
   Google の OAuth クライアントを `docunavi-app` の新しいものに替えたあとも、同じ手順で通った（2026-10-20、利用者の報告）。

**再デプロイ（コードを変えたとき）:** コミットしてから、4（ビルド）と 5（デプロイ。`--image` のタグだけ変わる）と 6 を繰り返します。Secret を更新したときは、`:latest` が新しい版を指すので、
`gcloud run deploy` をもう一度実行して新しいリビジョンを作ります（実行中のリビジョンは、起動時の版のまま）。

**つまずいたこと:**

- **起動に失敗した（`PoolTimeout`、コンテナが `exit(3)`）。** 原因は、Secret `database-url` の最初の版で、先頭の `p` が欠けていたこと（`ostgresql://…`。貼り付けの 1 文字目が `read` の前に取られた）。
  接続文として不正だと、プールは接続を試し続けて時間切れになるので、ネットワークの問題に見える。
  調べ方（`as_me`）: ログは `gcloud logging read 'resource.type="cloud_run_revision" AND resource.labels.revision_name="<リビジョン名>"' --project "$PROJECT_ID" --freshness=1h --order=asc --limit 60 --format='value(textPayload)'`。
  値は、パスワードを伏せて見る: `gcloud secrets versions access latest --secret database-url --project "$PROJECT_ID" | sed -E 's#(://[^:@/]+):[^@]*@#\1:***@#; s#\?.*#?…#'`。先頭だけなら `| cut -c1-14`。
  直し方: 正しい値で新しい版を足し（`gcloud secrets versions add database-url --data-file=-`。C の `case` で先頭を確かめる）、デプロイをやり直す。
  見るところ: 先頭が `postgresql://`、ホストが `…pooler.supabase.com`、ユーザー名が `<ロール>.<プロジェクト ref>`、ポートが 6543（transaction）か 5432（session）。
  `db.<ref>.supabase.co` の直接接続は IPv6 のみで、Cloud Run からは届かない。
- **ログイン後に `localhost:5173` へ戻される（`ERR_CONNECTION_REFUSED`）。** Cloud Run のサービスには URL が 2 種類ある
  （`https://docunavi-<プロジェクト番号>.asia-northeast1.run.app` と、ハッシュ付きの `https://docunavi-xe42miq4da-an.a.run.app`）。
  Supabase の Redirect URLs は文字列で照合するので、登録していない側から入ると、Site URL に戻される（当時の Site URL は `http://localhost:5173`。いまは公開の URL）。
  正規の URL は前者の 1 つとし、Redirect URLs にもそれだけを入れる。ハッシュ付きの URL は使わない。
- **`gcloud run services proxy` をバックグラウンドで動かすと止まる。** 「確かめる」を参照（ID トークン付きの `curl` を使う）。
- **閲覧専用の ID が、このプロジェクトのログやビルドを読めなかった（2026-10-20 に解消）。** 旧 `agent-viewer` は別のプロジェクト
  （`wataru-sandbox`）のサービスアカウントで、このプロジェクトには設定していなかった。運用用のプロジェクト `docunavi-ops` に
  `docunavi-viewer` を作り（旧 `agent-viewer` は削除済み）、`docunavi-app` に `run.viewer` `logging.viewer` `cloudbuild.builds.viewer` と
  `serviceusage.serviceUsageConsumer` を付けた。Cloud Run の状態・ログ・ビルドが読めることを、この ID のなりすましで確認した。
  通らなかった点: ① 作成直後は IAM の反映に数分かかる（`does not exist`、`Failed to impersonate`）。
  ② `gcloud builds list` は、なりすまし中の API の利用を `docunavi-ops` に数えるので、`Cloud Build API has not been used`（`SERVICE_DISABLED`）
  になる。`docunavi-ops` で有効にするには課金の紐づけが要る。紐づけず、`--billing-project=docunavi-app` を付けて `docunavi-app` に数えさせると通る
  （`serviceUsageConsumer` が要る）。
- 予算ガードの詰まりは G を参照。

**ロールバック:** 前のリビジョンに戻すのは、上の「公開する・戻す」の `update-traffic`。この記録の通りには実行していない（コマンドの確認のみ）。
イメージのタグがコミットごとに違うので、前のリビジョンのイメージが残る。

## 管理画面（`docunavi-admin`。ロードマップ ⑧ のフェーズ 4a）

ロール別の資料グループの割り当て画面（Django Admin）は、別の非公開の Cloud Run サービス（`docunavi-admin`）として配備していました（2026-10-26、ステージ R0〜R9 完了）。**2026-11-08 に、サービス `docunavi-admin` とジョブ `docunavi-admin-migrate` を削除し、`ops/access_admin/` もリポジトリから削除しました。** 当時の手順・環境変数・Secret・戻し方は、廃止の記録として [access-admin.md](access-admin.md) に残してあります。

## 保留中の運用の準備（2026-11-09。実行は利用者の操作待ち）

ロードマップの「将来対応」と未確認のうち、手順を整えたもの。書き込み（本番の設定・DB・配備）は、すべて利用者が実行する。`as_me` の意味は「誰の権限で実行するか」の節。

### 1. 引用の配備（`b004b5f`。ロードマップ 段階 4c）

**検証済み（2026-11-09、証拠等級 A）:** `1161423` の CI は成功（画面の単体テスト・型検査・ビルドを含む）。ローカルで `pytest` 3081 件通過（143 件スキップ）、画面の単体テスト 542 件通過、`tsc` のエラー 0。
`PREVIEW_DRY_RUN=1 ./scripts/preview.sh deploy|promote …` で、`--no-traffic --tag` と `--to-tags …=100`、設定を触らないこと（`--set-env-vars` `--set-secrets` なし）を確かめた。
**注意:** `e7ec2fb` の CI は、画面の単体テスト 1 件（`DocumentPanel.test.tsx`。`Blob` の型エラー）で一度失敗した。コードは同じで、次の `94ceb47` `1161423` は成功した。再現はせず、原因は未調査。

**順序は 画面 → API。** 引用の記法 `[1「語句」]` は画面が解釈する（[api.md](api.md)）。API を先に出すと、古い画面では記法が文字のまま出る（コードからの推測。未確認）。画面を先に出せば、記法を付けない古い API でも害は無い。

1. 画面: `deploy.yml` を `main` で起動する（利用者が GitHub の Actions で行うか、利用者の明示の承認を得て、私が GitHub MCP で行う）。Environment「production」の必須レビュアーは外してあり、承認なしで始まる（許可するブランチは `main` だけ。非公開の無料プランでは効かなかった〔2026-11-09〕。公開後の 2026-11-15 に設定して承認待ちで止まることを確認したが、毎回の承認が煩雑なので外した）。Worker は `docunavi-api`（認証）→ `docunavi-web` の順に出る。
2. API（利用者。コミットは HEAD のままでよい）: `./scripts/preview.sh build` → `./scripts/preview.sh deploy "$(git rev-parse --short HEAD)" citations-20261109`（0%）。
3. 0% のリビジョンで確かめる（次の 2）。`API_URL` に、タグ付きの URL（`https://citations-20261109---docunavi-xe42miq4da-an.a.run.app`）を渡す。
4. 通れば `./scripts/preview.sh promote citations-20261109`。戻し先は、いまの本番 `docunavi-00050-wuw`（`./scripts/preview.sh rollback docunavi-00050-wuw`）。

### 2. 引用が本番で正しく動くことの確認（2026-11-09 に実施。結果は下の「実施の記録」）

`scripts/prod_smoke_chat.py --stream`（2026-11-09 に追加。テストは `tests/test_prod_smoke_chat.py`）が、`/chat/stream` に 1 回送り、出典の資料グループ・`hit_count`・回答の文末の引用を、画面と同じ規則（番号が実在し、語句が抜粋に空白を除いて含まれる）で判定する。ネットワークを使わない部分（SSE の解析と引用の判定）だけをテストした。**本番での実行は未確認。**

1. テスト用ログインを有効にする（ホスト。[development.md](development.md) の「本番の /chat の自動テスト」）: `bash workers/api/test-login.sh on`
2. 質問を 1 回ずつ送る（LLM と埋め込みを使う。課金される。質問ごとに 1 回）:

   ```bash
   API_URL=https://citations-20261109---docunavi-xe42miq4da-an.a.run.app \
   infisical sm run --silent --projectId "$INFISICAL_PROJECT_ID_DEV" --env=dev -- \
     python scripts/prod_smoke_chat.py --stream --question "入寮の申込みの期限と、退寮の届出の期限を教えてください"
   ```

   質問の候補（2026-11-09 の実 API の確認と同じ種類）: 入寮・退寮の期限（本文に無い期限が作られた質問）、「みなとコーヒーはいつ創業しましたか。」（`cafe`）、「ザーハードのエネルギーが暴走するとどうなりますか」（`zahard`）。
3. 見るところ: `hit_count` が 1 以上、出典の `collection` が `default` ロールの範囲（`cafe` `guides` `synthetic-test` `zahard`）、引用の判定に `番号が無い` が無いこと。`原文に無い` が出たら、画面はその引用を番号ごと消す（想定の動作。件数を記録する）。
4. 画面（利用者の目。<https://docunavi-web.wataru-apps.workers.dev>、`promote` の後）: 文末の番号が押せる。押すと資料ビューアで語句が強調される。原文に無い語句の引用は出ない。
5. 指標: 1 回送るごとに、`chat_requests_total` が 1 増えること（Cloud Console の Metrics Explorer、プロジェクト `docunavi-app`。URL は未確認）。
6. 終わったら `bash workers/api/test-login.sh off`。

**実施の記録（2026-11-09、証拠等級 A。実行は利用者、判定は私）:** 画面は `ed2301b` で配備済み。API は `./scripts/preview.sh build` → `deploy`（イメージ `fc62c99`、リビジョン `docunavi-00052-nic`、タグ `citations-20261109`、0%）。`b004b5f` の後に `app/` の変更は無く、HEAD をビルドした（`build` は HEAD のイメージ名しか作れない）。
- タグ付き URL で `prod_smoke_chat.py --stream` を 1 回（入寮・退寮の期限）: 200、11.95 秒、`hit_count` 2、出典は `synthetic-test` の 2 件（`default` の範囲内）。引用は 原文どおり 1 件・原文に無い 2 件（「2月28日」「4月10日」）・`番号が無い` 0 件。画面が消す引用は 2 件。**限界:** 本文の作り話の期限は直らない。
- 利用者が `promote` を実行し、`docunavi-00052-nic` が 100%（本番の `/health` は ok）。戻し先は `docunavi-00050-wuw`。
- 画面（利用者のスクリーンショット 2 枚、質問「入寮の申込みはいつまでにすればよいですか？」。`tsubasa-dorm-application-guide-02` p.2 と `-03` p.1）: 文末に番号 1・2 が出る。参照資料の 2 番にカーソルを置くと抜粋が出る。番号を押すと資料ビューアが開き（`?tab=chat&doc=2137&view=text&page=1&chunk=9`）、該当チャンクに「根拠の箇所」が付いて、引用の文が枠で囲まれる。
- **未確認:** 原文に無い引用が画面から消えること（この質問では引用がすべて原文どおりで、消える場面が出なかった）。指標 `chat_requests_total` の増加。

### 3. ⑧ ロールを割り当てた利用者の `/chat`（2026-11-09 に実行済み。結果はロードマップの ⑧）

**確認した事実（2026-11-09、読み取り、証拠等級 A）:** 本番の `roles` は `cafe-staff`（`cafe` だけ）と `default`（`cafe` `guides` `synthetic-test` `zahard`）。`user_roles` は 0 行、`"user"` は 2 行、`access_audit` は 0 行。ロールの判定にキャッシュは無い（`app/access.py`）ので、割り当てはすぐ効く。

テスト専用ユーザー（Infisical の `TEST_LOGIN_EMAIL`。実在しないドメイン）にロールを割り当て、`/chat/stream` で範囲を確かめ、戻す。

1. `bash workers/api/test-login.sh on`（ホスト）。
2. 割り当て前の確認（書かない）: `python -m app.access_admin show --email "$TEST_LOGIN_EMAIL"`（`default` と見えるグループが出る）。書き込み用の `DATABASE_URL` が要る（運用者の手元）。
3. 割り当て: `python -m app.access_admin assign --email "$TEST_LOGIN_EMAIL" --role cafe-staff --apply`。
4. `cafe` の質問（範囲内）と `zahard` の質問（範囲外）を、それぞれ 1 回送る:

   ```bash
   infisical sm run --silent --projectId "$INFISICAL_PROJECT_ID_DEV" --env=dev -- \
     python scripts/prod_smoke_chat.py --stream --expect-collections cafe --question "みなとコーヒーはいつ創業しましたか。"
   ```

   期待: `cafe` の質問は `hit_count` 1 以上で出典が `cafe` だけ。`zahard` の質問は出典が無い（`hit_count` 0。資料に無い旨の回答）。どちらも「範囲外の出典: なし」で終了コード 0。
5. 戻す: `python -m app.access_admin revoke --email "$TEST_LOGIN_EMAIL" --role cafe-staff --allow-widen --apply`（最後のロールを外すと見える範囲が広がるので `--allow-widen` が要る）。戻した後、`zahard` の質問に出典が付くこと（`default` に戻ったこと）を 1 回確かめる。
6. `bash workers/api/test-login.sh off`。`access_audit` に 2 行（割り当てと解除）残る。

**限界:** テスト用ログイン（メール + パスワード）で、Google ログインの利用者ではない。JWT の `sub` からロールを引く経路は同じ（コードの読み取り。未確認）。
LLM と埋め込みを使う（課金される）。回数は、割り当て中が 2 回、戻した後が 1 回の計 3 回。

### 4. 履歴削除ジョブ（`--apply` を戻す時期の案と、戻す前の確認）

**現状（2026-11-09、読み取り、証拠等級 A）:** Job `docunavi-history-cleanup` は `--older-than-days 7`、`--apply` なし。Postgres の会話は 5 件で、最古は 2026-11-08 10:20 UTC。`history_admin` の件数の表示は、`--older-than-days 1` でも 0 件。
最初に消えうるのは、2026-11-22 03:00 JST の定期実行（日曜）。2026-11-15 03:00 JST の実行は、最古の会話がまだ 7 日未満なので 0 件（最後の動きから 7 日たった会話だけが対象）。

**更新（2026-11-15、読み取りで確認、証拠等級 A）:** 利用者の承認を得て、`--apply` を戻した（args は `-m app.history_admin --older-than-days 7 --apply`）。実行はしていない。
戻す更新は、Job のイメージ `docunavi:60833f5` が Artifact Registry のクリーンアップポリシーで消えていたため一度失敗した（`Ready=False`、`ContainerMissing`、2026-11-14 15:43Z から。他の Job は `Ready=True`）。
そこで、本番 `docunavi-00061-sir` と同じ `docunavi@sha256:e34b160c…` に差し替え、`Ready=True` に戻った。**未確認:** 初回の実行（2026-11-22 03:00 JST が最初に消えうる回）のログ、`checkpoints` と `checkpoint_writes` の DELETE 権限、Prisma への復元。
**残る弱点:** Job のイメージもポリシー（`docunavi` は最新 5 個）で消えうる。イメージを更新するたびに、Job が参照するイメージが残っているかを確かめる。
下の「案」「戻す前の確認」の 6 は実施済み。

**案:** 2026-11-21 までに `--apply` を戻す（最初に消える可能性がある実行は 2026-11-22 03:00 JST。そのときは週次のバックアップが 2〜3 世代あり〔直前は 2026-11-22 00:00 JST の実行。2026-11-10 に毎日から週 1 回へ変更〕、別の pgvector への復元は確認済み。Prisma への復元は未確認）。急ぎではない（いま消える会話は無い）。

**戻す前の確認:**
1. 日数は **7 日に決まった**（利用者の決定、2026-11-09。Job の 7 日のまま。方針の 90 日は [configuration.md](configuration.md) に残り、D の記述どおり様子を見て延ばす）。
2. 直前の件数の確認（書かない）: `python -m app.history_admin --older-than-days <日数>`（または Job を手動で 1 回実行して、ログの「対象の会話: N 件」を見る）。
3. 直近 24 時間以内のバックアップが R2 にあること（[pg-backup.md](pg-backup.md)）。
4. Job が使う DB の接続（Secret `database-url`）の役割に、`checkpoints` と `checkpoint_writes` の DELETE 権限があること。**未確認**（読み取りの権限では見えない）。最初の `--apply` は、既知の 1 会話に `--thread-id` で試すのが安全。
5. Scheduler `docunavi-history-cleanup-weekly` が有効で、日曜 03:00 JST のままであること（`gcloud scheduler jobs describe …`。閲覧用の権限では読めない）。
6. 戻す: `as_me gcloud run jobs update docunavi-history-cleanup --project docunavi-app --region asia-northeast1 --args=-m,app.history_admin,--older-than-days,<日数>,--apply`。
7. 戻した後: 次の実行のログで「削除した会話: N 件」と、削除後の `/chat` と会話一覧が動くこと。あわせて [deploy.md](deploy.md) の D と [configuration.md](configuration.md) の該当の記述を直す。

### 5. `budget-guard` の `DRY_RUN` を `false` に戻す条件と、戻す前の確認

**現状（2026-11-09、読み取り、証拠等級 A）:** リビジョン `budget-guard-00004-b7r`、`DRY_RUN=true`。ログの「would disable billing」は 2026-11-01 01:02〜01:10 UTC に集中し（費用 1004〜1051 に対して予算 1000.0）、それ以降はない。
2026-11-01 00:30:59 UTC の再発は、同じ月の費用がすでに予算を超えた状態で課金を紐づけ直したためと読める（ログの費用が予算を超えていたことと、上の G の注意「同じ月に予算を超えたままだと、再び紐づけた直後に止まる」に合う。推測）。同じ 0.2 秒の間に「billing disabled」と、`resourcemanager.projects.deleteBillingAssignment` の 403（`IAM_PERMISSION_DENIED`）が両方出ている。2 つのインスタンスが同時に起動していて、一方が成功したあとに他方が失敗している。権限が無いのではなく、二重の処理が外れた課金に対して失敗したと読める（推測。ログの事実は等級 A）。

**戻す条件（利用者の決定）:**
1. いまの予算額が ¥3,000 であること（利用者の説明: 途中で 1000 から 3000 に変更した。ログの 1000.0 は変更前）と、通貨・対象の範囲を、Console で確かめる。
2. 止まる影響を受け入れる。いまの本番は、課金が外れると Cloud Run の API・MCP と埋め込み（Vertex）が止まる（画面と認証の Worker、Prisma Postgres は GCP の外）。復旧は手動で、リソースが消えることがある（公式の警告）。
3. 課金を外す権限が、本番のプロジェクトで通ること。`docunavi-app` でも 2026-11-01 00:31 に一度は外れた（「billing disabled」）。403 は二重の処理の失敗と読める（上。推測）。
4. 止まったあとの戻し方（`budget-guard-disarm`、または課金の再紐づけ）が実際に通ること。`budget-guard-disarm` の動作確認は利用者の報告だけ。
5. 月の費用が予算に十分届かない状態で切り替える（届いた状態で切り替えると、すぐ止まる）。

**戻す前の確認:** `gcloud run services describe budget-guard`（`DRY_RUN` と PROJECT_ID）、予算の 50・90・100% の通知先 `budget-alerts` に届くこと、直近の月の費用（Console）。切り替えは、上の G の「本番に切り替える」と同じコマンド（`--update-env-vars "DRY_RUN=false"`）。**Pub/Sub への手動の publish はしない**（本物の停止になる）。

### 6. 監視のアラート（通知先としきい値）

ログベース指標 4 つは作成済み（2026-11-09）。アラートは次の 2 つ。**作成した（2026-11-09、利用者が実行。通知先は利用者宛てのメール `docunavi ops` 1 件）。** 閲覧用の権限では通知先とアラートの一覧を読めないので、作成の確認は利用者が貼った一覧（アラート 2 本、有効、通知先 1 件ずつ。証拠等級は利用者の報告）による。重複して作られた 2 本は、利用者が削除した。通知先の確認メールを押したかと、実際に通知が届くかは未確認。

| アラート | 条件 | 理由 |
|---|---|---|
| チャットの失敗 | `chat_failures_total` が 10 分に 3 件以上（[ops/monitoring/alert-chat-failures.yaml](../ops/monitoring/alert-chat-failures.yaml)） | 利用が少ない（`chat_metrics` は 30 日で 2 件）ので、率でなく件数で見る |
| Cloud Run の 5xx | `docunavi` の 5xx が 5 分に 5 件以上（[ops/monitoring/alert-cloudrun-5xx.yaml](../ops/monitoring/alert-cloudrun-5xx.yaml)） | チャットの指標に載らない失敗（起動・DB の 503 など）を拾う |

作成に使ったコマンド（記録。同じ名前で 2 回作ると重複する。作り直すときは、先に一覧で確かめる。1 回目は `resource.type` が無く失敗した。直して `819c1af`）:

```bash
CH=$(as_me gcloud alpha monitoring channels create --project docunavi-app --display-name "docunavi ops" \
  --type=email --channel-labels=email_address="$EMAIL" --format='value(name)')
for f in ops/monitoring/alert-chat-failures.yaml ops/monitoring/alert-cloudrun-5xx.yaml; do
  as_me gcloud alpha monitoring policies create --project docunavi-app --policy-from-file="$f" --notification-channels="$CH"
done
```

指標の値が増えることの確認は、上の 2 の手順 5。失敗時の `status` が本番のログで文字列になることは、本番で失敗が起きるまで受動確認（コードとテストでは文字列。わざと起こすには、0% のリビジョンに誤ったモデル名を渡す方法があるが、本番の指標とアラートに数えられるので、やる前に確認する）。

## 続き

次の段階（DB）は [roadmap.md](roadmap.md) を参照してください。
