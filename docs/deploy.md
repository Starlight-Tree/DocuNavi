# Cloud Run へのデプロイ（段階 0）

**この手順は未実行の案です。** コマンドの名前とフラグは `gcloud ... --help` で存在を確認しましたが、実際のデプロイ、
Vertex AI でのモデルの利用、Cloud Build での Docker ビルド、`agent-deployer` の権限の過不足は確かめていません
（Docker イメージのビルドと起動確認は CI が行っています）。

**資料の検索が DB のベクトル検索になったため（[search-design.md](search-design.md)）、本物の LLM のデプロイには `DATABASE_URL` が必須です**
（下の C。「任意」ではありません）。さらに、質問の埋め込みは API キーで呼ぶので、**A（Vertex AI）でも `GEMINI_API_KEY` の Secret が要ります**
（B の Secret の作り方）。A + ADC だけでは、起動の検査で失敗します。この組み合わせの実デプロイは未確認です。

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

- **`--set-secrets` ではなく `--update-secrets` を使う。** `--set-secrets` は既存の Secret を置き換える。上の B のデプロイの例（`GEMINI_API_KEY` と有料キーの 2 つだけ）をそのまま使うと、`DATABASE_URL` が外れて起動に失敗する。
  足したあとは、`gcloud run services describe` で `GEMINI_API_KEY` と `DATABASE_URL` が残っていることを確かめる。
- **AI Studio の一覧に出すには、プロジェクトのインポートが要る。** 一覧には、取り込んだプロジェクトのキーだけが出る（「プロジェクトをインポート」で `docunavi-app` を選ぶ。`docunavi-ops` は取り込まない）。
  CLI で作ったキーは Cloud Console の「API とサービス → 認証情報」にも出る。
- **有料の階層（Tier 1）にするには、AI Studio で最低 $5 の前払いが要る。** 請求階層が「無料トライアル」・ステータスが「前払いが必要」のうちは、有料の枠として使える保証がない。
  前払い（クレジットの購入）は AI Studio の画面だけで行い、CLI や API では行えない。クレジットは購入から 12 か月で失効し、返金されない。自動チャージは設定しない（設定しなければ、残高が $0 になった時点で止まる）。
  Google Cloud の無料クレジット（ウェルカムクレジット）は、2026-03-02 以降のアカウントでは Gemini API に使えない（公式の請求ドキュメント。AI Studio の画面で確認）。
- **前払いの残高が尽きると、有料キーは 402 を返す。** 切り替えの条件は 429 だけなので、その場合は切り替わらず、無料キーの 429 がそのまま返る。
- 2026-10-20 の記録: `docunavi-app` が Tier 1 と表示され、前払い ¥800 を購入した（利用者の報告）。有料キーでの実 API の呼び出しは確認済み（同日、Secret の値で `gemini-3.1-flash-lite` を 1 回呼んで HTTP 200。キーは 39 文字で、Tier 1 の課金も通っている。利用者が実行した結果の報告）。本物の 429 での切り替えと 60 秒の保持は未確認。

### C. DB に接続する（本物の LLM では必須）

`DATABASE_URL` は、資料の検索（DB のベクトル検索）と会話履歴の保存先を兼ねます。無いと、本物の LLM では起動に失敗します（[configuration.md](configuration.md) の「資料の検索」「会話履歴の保存先」）。
接続 URL はパスワードを含むので、キーと同じく **Secret Manager から渡します。**

```bash
read -rsp 'DATABASE_URL: ' DATABASE_URL; echo
# 貼り付けの先頭の 1 文字が欠けた事故があったので、先頭を確かめてから登録する（下の「実際に通った記録」）
case "$DATABASE_URL" in
  postgresql://*) printf '%s' "$DATABASE_URL" | as_me gcloud secrets create database-url --data-file=- --project "$PROJECT_ID" ;;
  *) echo "先頭が postgresql:// ではありません。登録しません。" ;;
esac
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
- Cloud Run から Supabase への接続は、**プーラーの URL（ポート 6543、transaction mode）で届くことを確かめました（2026-10-20。`/health` の `history` が `postgres`）。**
  直接接続は既定で IPv6 のため、IPv4 で届くプーラーの接続 URL を使います。
- 会話履歴は、**週 1 回の Job が、最後の動きから 7 日たった会話を削除します**（2026-10-20 に Job と Scheduler を作成。方針の 90 日は変えていません）。手順と確認の記録は、次の D です。

### D. 会話履歴の削除（Cloud Run Job + Cloud Scheduler。最初は 7 日）

**削除の日数は、最初は 7 日にします**（2026-10-20 に決定）。7 日の動きは、ローカルの Postgres で、時刻を書き換えて日がたつ様子を作って確かめました
（`tests/test_history_postgres.py` の `test_purge_at_seven_days_follows_the_clock`。6 日目は残り、8 日目に消え、続きを話した会話は残る）。本物の Supabase では未実行です。
方針の 90 日（`docs/configuration.md`）は変えておらず、様子を見て、下の `7` を書き換えて延ばします。
最後の動きから 7 日たった会話は消え、サイドバーの一覧からも消えます（戻せません）。実行は週 1 回なので、実際に消えるのは 7〜14 日後です。

**実行済みです（2026-10-20 JST、利用者が `as_me` で実行）。** B・C まで済んで、サービスが `/health` の `history` を `postgres` にして動いてからの手順です
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
- Job は `DATABASE_URL` の Secret（C の `database-url`）だけを渡します。LLM のキーは要りません。Cloud Run から DB に届くことは、C の確認（`/health`）が先です。

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
  --set-secrets "DATABASE_URL=database-url:latest" \
  --command python \
  --args=-m,app.history_admin,--older-than-days,7 \
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
- **確認済み（2026-10-20）:** Job の実行（イメージに `python` と `history_admin` が入って動く）、Cloud Run の Job から Supabase への接続（件数 0 件の表示まで）、Scheduler から Job を呼ぶ権限、`--args` での `--apply` の追加。
- **未確認:** 実際に会話が削除される動き。`history_admin` の `--apply` は、Supabase（実 DB）では `--thread-id` でだけ確かめていて、`--older-than-days --apply` で消えた実績はまだ無い（古い会話が無いため。ローカルと CI の Postgres では確認済み）。
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
**この節は手順の案で、未実行です**（課金が無効だったため。2026-10-13 時点）。

| 守り | 設定 | 備考 |
|---|---|---|
| 費用の上限 | `--max-instances 2`（上の A に入っている） | `2 × DATABASE_POOL_MAX_SIZE` が DB の接続数に収まること（C） |
| 予算アラート | 下の `budgets create` | 通知だけで、使用は止まらない。上限そのものは `--max-instances` |
| ログイン | `AUTH_MODE=required` と `SUPABASE_URL`（[configuration.md](configuration.md) の「ログイン（認証）」） | 起動時に JWKS を取得する。取れなければリビジョンが Ready にならない |
| CORS | `CORS_ORIGINS` | 画面は同じオリジンから配信されるので、CORS は要らない。**空**（何も許可しない）か、サービスの URL に絞る。`*` のままにしない |

```bash
# 予算アラート（請求先アカウントの管理者の権限。as_me で実行）。金額としきい値は例
as_me gcloud billing budgets create --billing-account=<請求先アカウント ID> \
  --display-name="docunavi" --budget-amount=<金額。請求先アカウントの通貨で> \
  --filter-projects="projects/${PROJECT_ID}" \
  --threshold-rule=percent=0.5 --threshold-rule=percent=0.9 --threshold-rule=percent=1.0
```

デプロイのコマンドの `--set-env-vars` に足す（`SUPABASE_URL` は公開してよい値だが、このリポジトリには書かない）:

```bash
  AUTH_MODE=required,SUPABASE_URL=<https://<ref>.supabase.co>,CORS_ORIGINS=
```

**未対応（公開の前に要る）:**

- **画面のログインは、下の F のイメージで有効にする。** `--source .` のイメージでは `VITE_SUPABASE_*` が空で、`AUTH_MODE=required` では質問できない（`/chat/stream` が 401）。
- トークンの期限切れ後の更新は未確認（Cloud Run から Supabase の DB と JWKS に届くことは、2026-10-20 に確かめた）。
- 課金の有効化は、このセッションでは確認できなかった（gcloud にログインしていない）。

### F. ログイン付きのイメージ（`cloudbuild.yaml`）

`--source .` は Dockerfile のビルド引数を渡せないので、画面のログイン設定（`VITE_SUPABASE_URL`・`VITE_SUPABASE_PUBLISHABLE_KEY`。どちらも公開してよい値）は、
`cloudbuild.yaml` でイメージをビルドして埋め込みます。配備は、できたイメージを `--image` で行います。
**Cloud Build での実行は確かめました（2026-10-20。約 1 分半で SUCCESS。このイメージで Google ログインと質問が通った）。**
設定の名前が Dockerfile と合っていることは `tests/test_cloudbuild.py` が確かめます。開発コンテナには Docker が無く、`docker build` はローカルでは実行していません。

```bash
REPO=cloud-run-source-deploy      # 初回の --source 配備で作られるリポジトリ。無ければ下で作る
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/${REPO}/${SERVICE}:$(git rev-parse --short HEAD)"

# 初回だけ: Artifact Registry のリポジトリを作る（書き込み。as_me）
as_me gcloud artifacts repositories create "$REPO" --repository-format=docker \
  --location "$REGION" --project "$PROJECT_ID"

# ビルドして push（書き込み。as_me）。SUPABASE_URL と SUPABASE_PUBLISHABLE_KEY は、画面用の公開値
as_me gcloud builds submit . --project "$PROJECT_ID" --config cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_SUPABASE_URL=${SUPABASE_URL},_SUPABASE_PUBLISHABLE_KEY=${SUPABASE_PUBLISHABLE_KEY}"
```

配備は、A のコマンドの `--source .` を `--image "$IMAGE"` に置き換えます（ほかのフラグはそのまま）。`agent-deployer` でできるかは未確認です
（`roles/run.sourceDeveloper` はソースからの配備用で、`--image` の配備や、ビルドの権限が足りない可能性があります。エラーに出た権限を足します）。

- イメージのタグはコミットごとに変えます（`:latest` にしない）。ロールバックで、前のイメージを指定できるようにするためです。
- `.dockerignore` が `.env` を除いているので、ビルドコンテキストに秘密は入りません。`VITE_*` に入れる値を、secret key や `DATABASE_URL` にしないこと
  （バンドルに入り、誰でも読めます）。

### G. 予算の自動停止（課金を紐づけるときに、デプロイより先に）

**この節は手順の案で、未実行です。** 関数は `ops/budget_guard/main.py`（判断はテスト済み。Cloud Billing の API の呼び出しと、GCP での動作は未確認）。
仕組みは、予算 → Pub/Sub → 関数 → プロジェクトの課金を無効化、という公式の方法です（Cloud Billing の「Disable billing with notifications」）。
**「無料枠を超えそうになったら止める」ことはできません。** 予算は金額で、Gemini の API キーは同じプロジェクトにあり、課金を紐づけると有料枠に切り替わる可能性があります
（公式の料金ページで確認）。実際は「月の費用が予算額に届いたら止める」です。金額は仮で、あとで変えられます。

**注意**

- 課金を無効にすると、**プロジェクトのサービスがすべて止まり、リソースは消えることがあります**（公式の警告。猶予の期間は書かれていません）。復旧は手動で、元に戻る保証はありません。
- 予算の通知は費用の反映より遅れます。**上限は厳密ではなく、止まるまでに予算を超えます。**
- API の有効化（Cloud Functions など）に課金が要るので、1 の直後から 4 までは続けて行います。その間は上限が働いていません。

```bash
BILLING_ACCOUNT=<請求先アカウント ID>
BUDGET=1000                        # 仮。請求先アカウントの通貨で指定する（円なら 1000 円）。あとで変えてよい
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
curl -s -H "Authorization: Bearer $TOKEN" "$URL/health"   # {"status":"ok","mode":"gemini","history":"postgres"} なら本物の LLM と DB に繋がっている
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

## タグ付きプレビュー（本番に流す前に、画面を専用 URL で確かめる）

**実行済み（2026-10-21）。** 手順 1〜4・7 は実際に通った（記録は節の末尾の「実際に通った記録（2026-10-21）」）。
手順 5（Supabase のワイルドカード）と、タグ付き URL でのログインの戻り（手順 6 の前半）は、確かめていない（下の「注意と未確認」）。
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

「実際に通った記録」の F と同じ。公開してよい値（`VITE_SUPABASE_*`）だけを渡す。
**`as_me` を付ける。** 日常の ID（閲覧専用の `docunavi-viewer`）では、ソースを置くバケット `docunavi-app_cloudbuild` に書けず 403 になる（2026-10-21）。

```bash
set -a; . frontend/.env.local; set +a
as_me gcloud builds submit . --project "$PROJECT_ID" --config cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_SUPABASE_URL=${VITE_SUPABASE_URL},_SUPABASE_PUBLISHABLE_KEY=${VITE_SUPABASE_PUBLISHABLE_KEY}"
```

### 3. 0% でデプロイし、タグを付ける（あなた）

```bash
as_me gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
  --image "$IMAGE" --no-traffic --tag "$TAG"
```

- **`--set-env-vars` と `--set-secrets` を付けない。** 付けると、既存の Secret の参照（`GEMINI_API_KEY_PAID` `DATABASE_URL` など）が置き換わって消える。イメージだけを変える。
- `--no-traffic` は、デプロイ前に `LATEST` へ向いていたトラフィックを、そのときの具体的なリビジョンに固定してから新しいリビジョンを作る（`--help` の記述）。
  このサービスは、すでにリビジョン名で固定されている（`docunavi-00005-zl7`）ので、変わらない。以後の `LATEST` はトラフィックを受けない。

### 4. 0% とタグ付き URL を確かめる（私）

```bash
gcloud run services describe "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --format='yaml(status.traffic)'
```

見ること: 新しいリビジョンが `tag: <TAG>` と `url: https://<TAG>---…` を持ち、`percent` が付いていない（0%）こと。`PROD_REV` が `percent: 100` のままであること。
続けて、タグ付き URL の `/health`（`mode` が `gemini`、`history` が `postgres`）。

```bash
curl -s "https://${TAG}---<describe に出た、タグを除いたホスト名>/health"
```

サービスが公開（`allUsers`）なので、タグ付き URL も認証なしで届く（2026-10-21 に確認。`/` と `/health` が 200）。
あわせて、新しいリビジョンの環境変数の名前と Secret の参照が、本番と同じであることを確かめる（`gcloud run revisions describe <リビジョン> --format=json` から、`env` の名前と `valueFrom` の有無だけを取り出す。値は出さない）。

### 5. Supabase の Redirect URLs（初回だけ。あなた）

Authentication → URL Configuration → Redirect URLs に、次を 1 行足す（画面のみ。MCP と SQL では変えられない）。

```
https://*---docunavi-xe42miq4da-an.a.run.app/**
```

- 画面は `redirectTo` に `window.location.origin` を渡す（`frontend/src/auth/supabase.ts`）ので、コードの変更は要らない。Google 側の設定も変えない（Google の戻り先は Supabase）。
- `*` は `.` と `/` を含まない文字列に一致する（Supabase の公式）。タグには `.` が入らないので、どのタグにも一致する。**実際の URL の形は、手順 4 の `describe` の出力で確かめる**（ハッシュ付きの URL の形で出る）。
- **サービスの URL は 2 つの形がある。** `https://docunavi-xe42miq4da-an.a.run.app`（`gcloud` が出す形。タグ付き URL もこの形）と、
  `https://docunavi-<PROJECT_NUMBER>.asia-northeast1.run.app`（プロジェクト番号の形。2026-10-21 に、本番の画面を開いていた URL）。どちらも同じサービスに届く。
  上のワイルドカードは前者のタグ付き URL にだけ一致する。後者の形でログインを確かめるなら、`https://*---docunavi-<PROJECT_NUMBER>.asia-northeast1.run.app/**` も要る（未登録・未確認）。
- 本番の Redirect URLs は `docs/deploy.md` の「実際に通った記録」の 8 のとおり、正規の URL 1 つ。ハッシュ付きの URL を本番の入口にしない方針は変えない。タグ付き URL は確認専用。
- 登録後、手順 6 でログインがタグ付き URL に戻ることを確かめる。戻らない（Site URL に飛ぶ）なら、登録した文字列と実 URL を見比べる。

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
  429 ではないので有料キーへの切り替えは働かず、`GEMINI_FALLBACK_MODEL` は本番で未設定のため別モデルにも切り替わらない（[configuration.md](configuration.md) の「主モデルが落ちたとき」）。
  確認で 502 が出たら、まずログのコンテナログの例外（`503 UNAVAILABLE` か）を見て、アプリの問題と切り分ける。

### 実際に通った記録（2026-10-21）

時刻は JST。サービス `docunavi`。

| 時刻 | 内容 |
|---|---|
| 21:08 | ビルド `docunavi:dc969b9`（`gcloud builds submit`、1 分 33 秒、成功）。最初は `as_me` なしで 403、付けて成功 |
| 21:14 | 本番（100%）は `docunavi-00005-zl7`（タグ `r2new`）。`--no-traffic --tag preview-20261021-2114` で配備 → `docunavi-00009-voj`（0%） |
| 21:15 | 確認: 00009 は 0%、本番は 00005 のまま。`/health` 200（`gemini` `postgres`）。環境変数の名前 10 個・Secret 参照 3 つ・ボリューム・実行 SA が 00005 と同じ。イメージのダイジェストはビルドと一致 |
| 21:17 | `update-traffic --to-tags preview-20261021-2114=100` → 100% が 00009 に移った。本番の `/health` 200 |
| 21:30 | 本番の画面（`docunavi-<PROJECT_NUMBER>.asia-northeast1.run.app`）にログイン（`/threads` 200） |
| 21:37 | 質問 1 回目: 502（37.5 秒）。原因は Gemini の `503 UNAVAILABLE`（高負荷）。アプリは固定メッセージを返した |
| 21:38 | 資料一覧から元の PDF を開いた（`/documents/2/file` 200、0.28 秒。R2 のエラーログ 0 件）。本番が R2 から PDF を返すことの確認 |
| 21:41 | 質問を再送 → `/chat/stream` 200（14.1 秒）。WARNING 以上のログは 0 件 |

確認用の会話（21:37 と 21:41 の 2 件）は、画面から削除する（SQL では消さない。チェックポイントのテーブルが相互に関わるため）。

## 実際に通った記録（2026-10-20）

上の節を、この順に `as_me`（あなた自身の権限）で実行して、公開まで通りました。ID（プロジェクト番号など）は書きません。

**現状:** サービス `docunavi`（`asia-northeast1`、リビジョン `docunavi-00002-…`）、イメージのタグはコミットの短いハッシュ（`cc5709c`）、公開済み（`allUsers` に `roles/run.invoker`）。
確かめたこと: `/health` が `mode: gemini`・`history: postgres`、公開後の `/` が 200、ログイン無しの `/chat` が 401、ブラウザーで Google ログインと質問が通る。

**順序:**

1. G の予算ガード（先に）。
2. API の有効化（`run` `cloudbuild` `artifactregistry` `secretmanager`）と、実行用サービスアカウント。
3. Secret を 2 つ: `gemini-api-key`（B）、`database-url`（C。プーラーの URL）。実行用サービスアカウントに `roles/secretmanager.secretAccessor`。
4. F のイメージのビルド（`frontend/.env.local` の公開値を `--substitutions` に渡す）。
5. 非公開でデプロイ。環境変数と Secret は次のとおり。

   ```bash
   as_me gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" \
     --image "$IMAGE" --service-account "${RUN_SA}@${PROJECT_ID}.iam.gserviceaccount.com" \
     --no-allow-unauthenticated --max-instances 2 --concurrency 20 --timeout 90 \
     --set-secrets "GEMINI_API_KEY=gemini-api-key:latest,DATABASE_URL=database-url:latest" \
     --set-env-vars "AUTH_MODE=required,SUPABASE_URL=${SUPABASE_URL},CORS_ORIGINS="
   ```

   `AUTH_MODE` と `SUPABASE_URL` は `.env` ではなく、このフラグで渡します。`CORS_ORIGINS` は空（同じオリジンで配信するため）。
6. 「確かめる」の `curl`（ID トークン付き）。
7. 公開（「公開する・戻す」）。
8. **Supabase の Redirect URLs に、サービスの URL を足す**（画面のみ。MCP と SQL では変えられない）。Authentication → URL Configuration → Redirect URLs に `<サービスの URL>/**`。
   ローカル用の `http://localhost:5173` は残す。画面は `redirectTo` に `window.location.origin` を渡す（`frontend/src/auth/supabase.ts`）ので、Site URL が `localhost` のままでもログインは Cloud Run に戻る。
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
  （`https://docunavi-<PROJECT_NUMBER>.asia-northeast1.run.app` と、ハッシュ付きの `https://docunavi-xe42miq4da-an.a.run.app`）。
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

## 続き

次の段階（DB）は [roadmap.md](roadmap.md) を参照してください。
