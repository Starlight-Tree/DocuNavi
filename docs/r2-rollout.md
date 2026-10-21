# 元 PDF を R2 から読む本番への切り替え（R2 の配線）

アプリ（Cloud Run サービス `docunavi`）の元 PDF の置き場を、イメージの中の `storage/pdf/` から Cloudflare R2 に切り替える手順です。
鍵の発行と入れ替えは [r2-key-issuer.md](r2-key-issuer.md)、コードは `app/r2_store.py`（読む側）と `ops/r2_wire/`（切り替える Job）。

**状態（2026-10-21）: 本番は R2 構成で動いている。** 切り替え（`stage` → `promote`）は済んでいて、リビジョン `docunavi-00005-zl7`（タグ `r2new`。作成 02:32 JST）が 100% を受けていた
（`promote` を実行した時刻は、サービスの監査ログからは取れていない。同日 21:14 の時点で 00005 が 100% だった）。直前のリビジョン `docunavi-00004-hj4` はタグ `r2prev` で 0% のまま残る。
同日 21:17 JST に、タグ付きプレビュー（[deploy.md](deploy.md) の「タグ付きプレビュー」）の昇格で、100% は `docunavi-00009-voj`（イメージ `dc969b9`）に移った。
00009 の環境変数・Secret の参照・R2 のボリュームは、00005 と同じ（`PDF_STORE` と `R2_*` の 4 つ、`r2-app-read` を `/secrets/r2` に）。
**元の PDF は、本番の画面から R2 経由で開けた**（21:38、`/documents/2/file` が 200 で 0.28 秒。R2 のエラーログは 0 件）。
下の手順は、切り替えの記録（上から順にあなたが実行し、私が結果を読んだ）。

## 設計

- **鍵はファイルで渡す。** Secret `r2-app-read` を、サービスのボリューム（`latest`）として `/secrets/r2/credentials.json` にマウントし、
  環境変数 `R2_CREDENTIALS_FILE` でパスを渡す。環境変数に鍵を入れる渡し方（`R2_CREDENTIALS_JSON`）は、起動時の値に固定されて入れ替えに追従できないので使わない
  （`ops/r2_wire` は、残っていれば取り除く）。
- **鍵が入れ替わったとき。** 旧鍵は、入れ替えの 7 日後に `revoke-stale` が取り消す。R2 が 401/403 を返したら、アプリはファイルを読み直し、
  鍵の ID が変わっていれば 1 回だけやり直す（`app/r2_store.py`）。再起動は要らない設計。
- **段階的に切り替える。** `stage`（新しいリビジョンを 0% で作る）→ 確認 → `promote`（100% を移す）。直前のリビジョンには常にタグ `r2prev` が付き、`rollback` で戻せる。
- Job が変えるのは、環境変数 4 つ（`PDF_STORE` `R2_ACCOUNT_ID` `R2_BUCKET` `R2_CREDENTIALS_FILE`）、ボリュームとマウント、トラフィックの配分だけ。
  **イメージ・他の環境変数・Secret は、いまのサービスの設定のまま。**

## 切り替えの前に知っておくこと

1. **本番のイメージ（`cc5709c`）には、R2 を読むコードが入っていない。** R2 のコードは、そのあとのコミット（`48de586` `b353e4a` `6fde00b`）にある。
   配線だけしても、`PDF_STORE=r2` は無視される。先に、いまのコミットのイメージを配備する（手順 1）。`PDF_STORE` が未設定なら、動作は変わらない（`storage/pdf/` を読む）。
2. **`/health` は DB と Gemini しか見ない。** R2 の読み取りは `/health` では分からない。確認は、`r2-verify`（同じ形のボリュームで 6 件を読む）と、切り替えの後に画面で元の PDF を開くことで行う。
3. **画面のログインは、タグの URL（`r2new---…`）では通らない**（Supabase の Redirect URLs に無い）。画面での確認は `promote` の後になる。問題があれば `rollback`（1 コマンド）。
   （切り替え前の記述。タグ付きプレビューの手順 5 のワイルドカードを足せば、タグの URL でも戻れる見込みだが、ログインの戻りは未確認。[deploy.md](deploy.md)）
4. R2 を読めないときも、ローカルの PDF に切り替えない（食い違った版を見せないため）。

### PDF が R2 に無い・読めないとき（テスト済み）

| 状況 | 元 PDF の API（`/documents/{id}/file`） | 資料一覧（`/documents/{id}/chunks`） | 検索・チャット |
|---|---|---|---|
| R2 に無い | 404（固定文） | 200。`original_pdf: false` | 影響なし |
| 名前は同じで中身が違う | 409（固定文） | 200。`original_pdf: false` | 影響なし |
| R2 を読めない（鍵の失効・権限・通信・設定の不足） | 503（固定文。例外の文字列・鍵は出ない） | 200。`original_pdf: false` | 影響なし |

テスト: `tests/test_r2_store.py`（`test_file_endpoint_*`、`test_the_document_screen_keeps_working_whatever_r2_answers`）、`tests/test_r2_store.py` の探索のテスト。
つまり、R2 の障害は「元の PDF を開けない」だけで止まり、検索・チャット・資料一覧は動き続ける。

## 手順

`as_me` は `env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`（あなたの ID。コンテナの外のホストか Cloud Shell で。`gcloud config unset auth/impersonate_service_account` 済みであること）。

### 1. アプリのイメージを今のコミットで配備する（あなた）

コミットしてから行う。**`--set-secrets` / `--set-env-vars` を付けない**（付けると、有料キーの Secret `GEMINI_API_KEY_PAID` など、いまの設定が消える。イメージだけを変える）。

```bash
PROJECT_ID=docunavi-app; REGION=asia-northeast1; SERVICE=docunavi
IMAGE="${REGION}-docker.pkg.dev/${PROJECT_ID}/cloud-run-source-deploy/${SERVICE}:$(git rev-parse --short HEAD)"
# frontend/.env.local の公開値（VITE_SUPABASE_URL / VITE_SUPABASE_PUBLISHABLE_KEY）を読む。画面には出さない
set -a; . frontend/.env.local; set +a
env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud builds submit . --project "$PROJECT_ID" --config cloudbuild.yaml \
  --substitutions "_IMAGE=${IMAGE},_SUPABASE_URL=${VITE_SUPABASE_URL},_SUPABASE_PUBLISHABLE_KEY=${VITE_SUPABASE_PUBLISHABLE_KEY}"
env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud run deploy "$SERVICE" --project "$PROJECT_ID" --region "$REGION" --image "$IMAGE"
```

確認（私）: サービスの設定（環境変数の名前・Secret の参照が増減していないこと）、`/health`。画面で質問が 1 つ通ること（あなた）。

### 2. 配線の Job を作る（あなた）

```bash
./scripts/setup-r2-wire.sh docunavi-app --cloudflare-account <CLOUDFLARE_ACCOUNT_ID>
./scripts/setup-r2-wire.sh docunavi-app --cloudflare-account <CLOUDFLARE_ACCOUNT_ID> --verify
```

サービスは変わらない。Job は 3 つ（`r2-wire-stage` `r2-wire-promote` `r2-wire-rollback`）。実行用 SA `r2-wirer` の権限は、サービス `docunavi` の `run.developer`、
`docunavi-run` の `serviceAccountUser`、`r2-app-read` の `secretmanager.viewer`（値は読めない）、イメージのリポジトリ `cloud-run-source-deploy` の `artifactregistry.reader` だけ。`docunavi-viewer`（私）には、これらを実行する権限を付けない。

### 3. `stage`（あなたが実行 → 私が結果を読む）

```bash
env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud run jobs execute r2-wire-stage --region asia-northeast1 --project docunavi-app --wait
```

期待: 終了コード 0、ログに `staged: new revision … (0%, tag r2new), production stays on … (tag r2prev)`。本番は直前のリビジョンのまま 100%。
私が確認すること: 新しいリビジョンに環境変数 4 つ・ボリューム・マウントがあり、`R2_CREDENTIALS_JSON` が無いこと。値はログに出ない。

### 4. `r2-verify`（私。実行のたびに承認が出る）

R2 の 6 件が、DB のハッシュと一致して読めること（`資料 6 件、一致しない 0 件`）。

### 5. `promote`（あなたが実行 → 私が結果を読む）

```bash
env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud run jobs execute r2-wire-promote --region asia-northeast1 --project docunavi-app --wait
```

期待: 終了コード 0、`promoted: production is on …`。切り替えの後に本番の `/health` が通らなければ、Job が自動で `r2prev` に戻す（終了コード 4）。

### 6. 切り替えの後の確認

- あなた: 画面の「資料一覧」で、PDF のある資料を開き、元の PDF が表示されること（ここで初めて、本番が R2 から PDF を返すことが分かる）。
- 私: ログに `R2 から元の PDF を読めませんでした` が出ていないこと。

### ロールバック

```bash
# 直前のリビジョンへ 100% 戻す（数秒。設定は消えない。タグ r2prev を使う）
env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud run jobs execute r2-wire-rollback --region asia-northeast1 --project docunavi-app --wait
```

`r2-wire-rollback` が使えないとき（Job が壊れた）の直接の方法:
`env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud run services update-traffic docunavi --region asia-northeast1 --project docunavi-app --to-revisions=<リビジョン名>=100`
（リビジョン名は `gcloud run revisions list` で見る。切り替えの前のリビジョンは、手順 1 のあとのもの）。
アプリのイメージ自体を戻したいときも、同じ方法で手順 1 の前のリビジョン（`docunavi-00003-b9t`）へ戻す。

### 失敗したとき（終了コード）

| 終了コード | 意味 | 本番 |
|---|---|---|
| 2 | 設定・前提の誤り（トラフィックが 1 つのリビジョンに 100% でない、タグが無い、など） | 変えていない |
| 3 | Cloud Run API の失敗（権限など。ログにステータスだけ出る） | 変えていない |
| 4 | 新しいリビジョンが準備できない、または `/health` が通らない | 直前のリビジョンのまま（または戻した） |
| 5 | 想定外のトラフィックの動き（戻そうとした） | ログを見る |

## 確かめたこと・確かめていないこと

確かめた（模擬）: `ops/r2_wire` の動き（`tests/test_r2_wire.py`。Cloud Run は偽物）、配備スクリプトが作るものと権限（`tests/test_r2_scripts.py`。gcloud は偽物）、
PDF が無い・読めないときの API の動き（`tests/test_r2_store.py`）。

確かめていない（本物の Cloud Run・R2 で）:

- `r2-wire-stage` の実行。**1 回目は、終了コード 3（403）で止まった**（2026-10-21）。サービスの更新時に Cloud Run が呼び出し側の `artifactregistry.repositories.downloadArtifacts` を確かめるのに、`r2-wirer` に無かったため。サービスは変わらなかった。`artifactregistry.reader`（リポジトリ単位）を足した。`secretmanager.viewer` が本当に要るかは未確認。
  **2 回目は、終了コード 4 で止まった**（新しいリビジョン `docunavi-00005-zl7` は作られたが、タグも配信も無いので Cloud Run が退役させ、「準備済み」にならなかった。本番は変わらず）。最初の更新で、新しいリビジョンに 0% でタグ（`r2new`）を付ける形に直した。更新が通ったこと（環境変数 4 つ・ボリューム・マウントが入ったリビジョンができること）は、実機で確かめた。
- ボリューム（`latest`）が、動いているインスタンスで、Secret の新しい版に追従するか。追従しなくても、401/403 での読み直しで復帰する設計だが、本物の R2 での 401/403 の経路は未確認。
- 旧鍵の取り消し（`revoke-stale`）のあと、再起動なしで読み続けられること。確かめられるのは、次の本物の入れ替えのあと（早くて 2027 年 1 月）。**この基準は、切り替えの日には確かめられない。**
