# 開発コンテナの gcloud と GitHub CLI（ログインとガード）

この開発コンテナ（Claude Code を動かす環境）に、あなた自身のログインやトークンを置くときの手順と、それを守る層の説明です。
アプリを動かすだけなら読む必要はありません。コマンドはリポジトリのルートで実行します。
エージェントが守ることは [CLAUDE.md](../CLAUDE.md) にあります。

## 開発環境の gcloud（ログインとガード）

開発コンテナには gcloud が入ります（イメージのビルド時。`.devcontainer/Dockerfile`）。GCP の参照（Cloud Build のトリガー、Cloud Run のログなど）は、
**あなた自身のアカウントでログイン**して行います。コンテナにログイン情報があるので、次の層で守っています。

| 層 | 内容 | 効くもの |
|---|---|---|
| 日常の ID | コンテナの環境変数 `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`（`devcontainer.json` の `containerEnv`）で、閲覧専用の `docunavi-viewer`（運用用プロジェクト `docunavi-ops` のサービスアカウント）に固定します。起動した時点から、ターミナルでもエージェントでも閲覧専用で、書き込みは 403 になります | 誤操作、素朴な指示の埋め込み、戻し忘れ |
| PreToolUse フック | `.claude/hooks/guard-cloud-cli.py` が、実行前に gcloud などの呼び出しを検査します。削除・IAM・課金の変更・認証情報の取得・ID の切り替えは拒否し、書き込み系は確認を強制します | 同上、設定の書き換え |
| 権限ルール | `.claude/settings.json`: 読み取り系の gcloud だけ許可し、`~/.config/gcloud` と `.gcloud-token` の読み取りを拒否し、bypass モードを無効にします | 同上 |
| GCP 側 | 予算アラート、プロジェクトの削除防止（lien） | 最後の砦 |

**限界**: これらはセキュリティ境界ではありません。コンテナにはあなたのログイン情報があるので、フックを通らない経路
（スクリプトが認証情報のファイルを直接読むなど）までは防げません。2026-10-21 から、このログイン情報（リフレッシュトークンを含む）は
名前付きボリュームに残るので、再ビルドのあとも、同じコンテナ内のプロセス（拡張機能を含む）と、ホストでボリュームに触れる人から読めます。
Read の拒否ルールとフックは、これを完全に隔離するものではありません。権限ルールもコマンド文字列の照合で、Claude Code の公式文書も
「境界ではない」と明記しています。`--dangerously-skip-permissions` は使わないでください（設定で無効にしています）。

### 初回の設定

> ログインと設定は、あなたがターミナルで実行します。Claude Code の VS Code 拡張のチャット欄は、コマンドを直接実行する
> `!` の入力（シェルモード）に対応していません（CLI の `claude` では使えます）。

1. **ログインする**（コンテナ内。あなたが入力します。VS Code の統合ターミナル、Cmd+`（Windows/Linux は Ctrl+`）で実行します。
   開発コンテナに接続した VS Code では、このターミナルがコンテナの中で動きます）

   ```bash
   gcloud auth login --no-launch-browser
   gcloud config set project <プロジェクト ID>
   ```

2. **閲覧専用のサービスアカウントを作る**（一度だけ。ホスト、Cloud Shell、または手順 1 の直後のコンテナの統合ターミナルで実行します）

   サービスアカウントは、閲覧の対象（アプリのプロジェクト）とは別の**運用用プロジェクト**に置きます（このリポジトリでは
   `docunavi-ops`。アプリ用の権限と閲覧用の権限を混ぜないため）。運用用プロジェクトは先に作っておきます。課金は付けません。

   ```bash
   ./scripts/setup-agent-viewer.sh <運用用プロジェクト ID> <対象プロジェクト ID>      # 内容を表示して確認を取ります。何度実行しても構いません
   ```

   実際の操作は次のコマンドと同じです。

   ```bash
   OPS_PROJECT=<運用用プロジェクト ID>        # docunavi-ops
   TARGET_PROJECT=<対象プロジェクト ID>       # docunavi-app
   ME=<あなたのアカウント>
   SA=docunavi-viewer@${OPS_PROJECT}.iam.gserviceaccount.com

   gcloud services enable iamcredentials.googleapis.com --project "$OPS_PROJECT"
   gcloud iam service-accounts create docunavi-viewer --display-name="DocuNavi viewer" --project "$OPS_PROJECT"
   gcloud iam service-accounts add-iam-policy-binding "$SA" \
     --member="user:${ME}" --role=roles/iam.serviceAccountTokenCreator --project "$OPS_PROJECT"
   for role in roles/cloudbuild.builds.viewer roles/run.viewer roles/logging.viewer roles/serviceusage.serviceUsageConsumer; do
     gcloud projects add-iam-policy-binding "$TARGET_PROJECT" \
       --member="serviceAccount:${SA}" --role="$role" --condition=None
   done
   ```

   `logging.viewer` は対象プロジェクト内のログをすべて読めます。同じプロジェクトに別用途のログがあるなら、外すか絞ってください。
   `serviceusage.serviceUsageConsumer` は、なりすまし中の API の利用を対象プロジェクトに数えさせる（下の「読み取りの書き方」の
   `--billing-project`）ために付けます。付けないと、運用用プロジェクトで Cloud Build などの API を有効にする必要が出て、
   有効化に課金の紐づけが要ります。

   作成した直後は IAM の反映に数分かかります。`does not exist` や `Failed to impersonate` が出たら、待ってやり直してください。

3. **日常の ID は、環境変数で閲覧専用に固定されています**（手順はありません）

   `.devcontainer/devcontainer.json` の `containerEnv` が `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` を設定します。
   コンテナを作り直したあとも、ログイン情報はボリュームに残るので、通常は再ログインなしで、閲覧専用の ID で動きます
   （設定を打ち直す必要もありません）。失効や組織のポリシー等で再認証が要ることはあり、そのときは手順 1 をやり直します。
   ボリュームを足した最初の再ビルドだけは、ボリュームが空なので、手順 1 のログインが 1 回要ります。

   ターミナルで `echo "$CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT"` を実行して空なら、`containerEnv` を足す前に作った古いコンテナです。
   「Dev Containers: Rebuild Container」で作り直してください。手動の `export` は、新しいターミナルで消えて、気づかないうちに
   自分の権限で動くことになるため、案内しません。

   `gcloud config set auth/impersonate_service_account` では設定しません。外す・戻すの操作が要り、戻し忘れが起きるためです。
   書き込みをするときは、下の「書き込みをするとき」を参照してください。

4. **確認する**

   ```bash
   gcloud auth list                                   # ACTIVE があなたのアカウント
   gcloud builds triggers list --project <プロジェクト ID>
   gcloud builds triggers list --project <プロジェクト ID> --region=asia-northeast1   # リージョンは実際の値に読み替える
   ```

   出力の見方:

   - `WARNING: ... All API calls will be executed as [docunavi-viewer@...]` が出ていれば、閲覧専用の ID で動いています。
   - `PERMISSION_DENIED: Failed to impersonate` は、作成直後の IAM の反映待ちです（数分で解消します）。
   - `API [...] not enabled on project <運用用プロジェクトの番号>` は、なりすまし中の API の利用が、サービスアカウントを置いた
     運用用プロジェクトに数えられている、という意味です。Cloud Build など一部の API で出ます。コマンドに
     `--billing-project=<対象プロジェクト ID>` を付けてください（`serviceUsageConsumer` が要ります）。
   - 閲覧専用の ID は、付与した 3 つのロールの範囲しか読めません。IAM やサービスの一覧などが拒否されるのは、設計どおりです。

5. **ログアウトは、毎回は要りません**（続けて使うあいだは維持してよい）。長期に使わないとき、共有の端末、不審な挙動のときは、コンテナの統合ターミナルでログアウトします。ログイン情報はボリュームに残るので（2026-10-21 から）、再ビルドでは消えません。確実に消すには、`gcloud auth revoke --all`
   （サーバー側でも失効する）、ホストでの `docker volume rm gcloud-config-<id>`、Google アカウントの「サードパーティのアクセス」から Google Cloud SDK を外す、のいずれかを行います。
   `revoke` はコンテナ内のログインを消す操作で、ホストの gcloud のログインを消す手順ではありません
   （ただし `gcloud auth revoke` はサーバー側でもトークンを失効させます。公式の説明には、同じアカウントで使っている
   他の端末への影響は書かれていません）。代替方式（ホストで短期トークンを発行する）を使うなら、
   ホストのログインが要るので、ホストでは `revoke` しないでください。

   ```bash
   gcloud auth revoke --all
   ```

### 書き込みをするとき

既定は閲覧専用です。書き込み（API の有効化、サービスアカウントや権限の作成、デプロイ、サービスの公開）は、**あなたがコマンドごとに
明示して**実行します。設定を外したままにする運用はしません。

| やること | 方法 | 使う ID |
|---|---|---|
| 一度きりの準備（API の有効化、SA の作成、権限の付与、サービスの公開） | コマンドの先頭に `env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` を付ける（この 1 コマンドだけ変数が外れる） | あなた自身 |
| 日常のデプロイ | `--impersonate-service-account=agent-deployer@<プロジェクト ID>.iam.gserviceaccount.com` を付ける（フラグは環境変数より優先される） | `agent-deployer`（`scripts/setup-agent-deployer.sh` で作る） |
| 読み取り | 何も付けない（Cloud Build の閲覧は `--billing-project=<対象プロジェクト ID>` を付ける） | `docunavi-viewer` |

- `setup-agent-viewer.sh` と `setup-agent-deployer.sh` は、スクリプトの中で変数を外すので、そのまま実行できます。
- あなたが project の `owner` のままなので、`agent-deployer` は「あなたの権限を減らす」ものではありません。
  デプロイが必要な権限だけで動き、監査ログで区別でき、デプロイ中にうっかり IAM や削除を触らない、ための分離です。
- `agent-deployer` には、サービスを公開する権限（`allUsers` への `roles/run.invoker`）と API を有効にする権限を付けません。
  それらは、あなた自身の ID（`env -u`）で行います。
- エージェント（Claude Code）は、これらのコマンドを実行しません。`--impersonate-service-account` や `CLOUDSDK_*` の
  設定は、フックが拒否します（あなたがターミナルで打つ分には、フックは関係しません）。

### GCP 側の保護（最後の砦）

- **予算アラート**: Console の「お支払い → 予算とアラート」で、想定外の課金に気づけるようにします。
- **プロジェクトの削除防止（lien）**: `roles/resourcemanager.lienModifier` を持つアカウントで、次を実行します。
  lien が防ぐのはプロジェクトの削除だけで、中のリソースの削除は防ぎません
  （[公式ドキュメント](https://docs.cloud.google.com/resource-manager/docs/project-liens)）。

  ```bash
  gcloud alpha resource-manager liens create --project=<プロジェクト ID> \
    --restrictions=resourcemanager.projects.delete --reason="agent guard" --origin=agent-guard
  ```

### 代替：ログインを持たせない方式

ホストで短期トークン（約 1 時間）を発行し、`--access-token-file` で使う方式も残しています（`scripts/issue-agent-token.sh`）。
ログインを置かないので、いちばん安全です。ただし、フックは ID を切り替える `--access-token-file` を拒否するので、
この方式を使うなら `.claude/hooks/guard-cloud-cli.py` の `IDENTITY_FLAGS` から外してください。
また、この方式ではトークンがすでに `docunavi-viewer` のものなので、環境変数の既定（なりすまし）を外して使います
（`env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud ... --access-token-file=.gcloud-token`）。
外さないと、`docunavi-viewer` が自分自身になりすまそうとして失敗します。

## 開発環境の GitHub CLI（gh）

開発コンテナには `gh` が入ります（`.devcontainer/install-gh.sh`）。CI の実行結果やログ、ルールセットなど、GitHub の参照に使います。
gcloud と同じ考え方で、エージェントには**読み取り専用**だけを持たせ、書き込み（PR の作成、設定の変更）はあなたが行います。

| 層 | 内容 |
|---|---|
| トークン | **このリポジトリだけ**の細粒度トークン（fine-grained PAT）。`Actions` と `Metadata` の読み取り専用で、期限は 90 日。書き込みの権限は付けません。これが主な制御です。1 リポジトリ・読み取りだけなので、漏れたときの被害が小さく、更新の手間とのバランスで 90 日にしています（これより長くはしません） |
| PreToolUse フック | `.claude/hooks/guard-cloud-cli.py` が gh の呼び出しも検査します（Bash と Monitor）。認証・トークンの取得、削除、秘密と設定の変更、拡張とエイリアス（コードの実行）、書き込みの GraphQL、URL 指定は拒否し、書き込み系は確認を強制します。`git credential`（保存された認証情報の取り出し）も拒否します。コマンドは、改行・`;`・パイプ・引用符・コマンド置換・ヒアドキュメントを読んで 1 つずつに分け、`bash -c`・`xargs`・`sudo`・`find -exec` などの中身も調べます。同じコマンドの中で代入した変数（`g=gh; $g ...`）、ブレース展開、`ssh host gh ...` のように文字列に現れるものも調べ、中身を検査できない `curl ... \| sh` は確認にします |
| 権限ルール | `.claude/settings.json`: 読み取り系の gh（`run` / `pr` / `issue` / `workflow` / `ruleset` / `release` / `repo view`）だけ許可し、gh の設定ディレクトリの読み取りを拒否します。**`gh api` は許可リストに入れていません**（毎回確認します。フックの見落としが、そのまま実行にならないようにするためです） |

**gcloud との違い**: gh の設定（トークン）は、再ビルドしても残ります。名前付きボリュームを `~/.gh-config` にマウントし、
`GH_CONFIG_DIR` で gh にそこを使わせています（`~/.config/gh` に直接マウントすると、`~/.config` が root 所有で作られ、
gcloud などが書けなくなるおそれがあるためです）。読み取り専用で期限付きのトークンだけを置く前提なので、残しています。
コンテナにはキーリングが無く、トークンは平文のファイルとして保存されます。フックは境界ではありません（gcloud と同じ限界です）。
コマンド文字列の検査なので、実行時に決まる値に隠した操作（`read` で受けた値や、コマンドの出力をプログラム名にする書き方）、
書いてから実行するスクリプト、パスの難読化（`cd` した後の相対パスなど）は追えません。`ssh` や `docker exec` に渡した
コマンドの文字列は調べますが、その先の環境（別のマシンの認証情報）は見えません。主な制御は、トークンの権限（読み取り専用）です。

**あなた自身の gh の操作は、コンテナの外で行います。**

- **コンテナの中では、`gh auth login`（ブラウザ・デバイスフロー）を実行しません。** `GH_CONFIG_DIR` は人間とエージェントで共有なので、
  あなたのアカウント権限（全リポジトリへの書き込み）のトークンが保存されると、エージェントの gh もそれを使えてしまい、
  読み取り専用にした意味がなくなります。コンテナの中で行うログインは、下の手順 2 の `--with-token`（読み取り専用トークン）だけです。
- **書き込み（PR の作成・マージ、設定やルールセットの変更）は、ホスト側（Mac）の gh かブラウザで行います。** コンテナの gh は
  読み取り専用トークンなので、実行しても権限不足で失敗します。エージェントは、書き込みのコマンドを提示するだけです。

### 初回の設定

1. **トークンを作る**（GitHub のブラウザ操作）。Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token。
   - Repository access: Only select repositories → このリポジトリ
   - Repository permissions: `Actions` を Read-only（`Metadata` は自動で Read-only になります）。
     ルールセットを読みたいときは、`Administration` を Read-only にします（まず付けずに試し、読めなければ足します）
   - Expiration: 90 days
   - 同じ権限のトークンが期限内で残っているときは、新規作成の代わりに、そのトークンの Regenerate token（再生成。画面にボタンがあれば）でも足ります
2. **ログインする**（VS Code の統合ターミナル。トークンはチャットに貼りません）

   ```bash
   gh auth login --with-token     # 実行して、トークンを貼り付け、Enter を押してから Ctrl+D
   gh auth status                 # Logged in と表示される
   ```

3. **確認する**

   ```bash
   gh run list --limit 3
   gh ruleset list                                 # Administration を付けた場合（読めなければ、ブラウザで確認）
   ```

期限（90 日）が切れると 401 になります。新しいトークンを作って（または再生成して）、手順 2 をやり直してください。
忘れないよう、期限の日をカレンダーに入れておくと確実です。
やめるときは、`gh auth logout` でトークンを消せます（再ビルドでは消えません）。

### 見直す契機

今の形（1 リポジトリ・読み取りだけ・デプロイなし）には、次のどれも必要ありません。状況が変わったときに見直します。

| 状況 | 次の形 |
|---|---|
| デプロイを GitHub Actions に移す | Actions から GCP へは OIDC（Workload Identity Federation）。長期の鍵を置かない |
| 複数リポジトリや Bot 的な自動化が増える | GitHub App と、短期の installation token |
| エージェントに PR の作成まで任せたい | 書き込み専用の短期トークンを、読み取り用とは別に持つ。実行は人間の承認を残す |
| 90 日の更新も負担になった | GitHub App 化を検討する |

## 開発環境のブラウザー確認（Playwright MCP）

開発コンテナには、エージェントが画面を開いて確認するための Playwright MCP が入ります。資料一覧や PDF の表示、チャットの出典、
console と network のエラーを、スクリーンショット付きで確かめるために使います。アプリのコードや本番の環境には関係しません。

| 項目 | 内容 |
|---|---|
| 登録 | リポジトリ直下の `.mcp.json`（プロジェクトのスコープ）。`@playwright/mcp@0.0.83 --headless --browser chromium`。初回は、Claude Code がプロジェクトの MCP を信頼するかを尋ねます |
| Chromium | `.devcontainer/install-playwright.sh` が `~/.cache/ms-playwright` に取得します（約 260 MB）。ここは名前付きボリュームなので、再ビルドしても取得し直しません |
| ライブラリー | 同じスクリプトが `playwright install-deps chromium`（apt）で入れます。Chromium の実行に要るライブラリーと、日本語を含むフォントです。ボリュームには入らないので、再ビルドのたびに入れ直します（作成が数分延びます）。apt のロックを他の導入が持っていて失敗したときは、15 秒あけて最大 8 回試し直します |
| 成功の判定 | 最後に Chromium を実際に起動して（`chrome --version`）確かめます。だめなら理由を stderr に出して終了コード 1 で終わり、作成のログに「Playwright の導入に失敗しました」と出ます（他の導入は先に済んでいます）。直したら `bash .devcontainer/install-playwright.sh` を再実行します |
| スクリーンショット | `.playwright-mcp/` に出ます（保存先は、ツールの制限でリポジトリ内に限られます）。`.gitignore` には入れていないので、使ったあとは未追跡のファイルとして Git に出ます。確認が済んだら削除し、コミットしません |

使うときは、モックを起動してから（`GEMINI_API_KEY=your_api_key_here uvicorn app.main:app`）、エージェントに画面の確認を頼みます。
PDF は、開いてから描画が終わるまで数秒かかります（すぐに撮ると空白になります）。

**注意すること**

- **alpha の依存です。** `@playwright/mcp@0.0.83` は `playwright 1.64.0-alpha-1790635538000` に固定された依存を持ち、MCP の版を上げるときは、
  `install-playwright.sh` の `PLAYWRIGHT_VERSION` も、MCP が依存する版に合わせて変えます（合わないと Chromium のリビジョンが違い、起動できません）。
  安定版の playwright に依存する MCP は 0.0.30 までで、Claude Code のスキーマ検証と合わず動きませんでした。
- **ローカル専用です。外部サイトは開きません**（運用で守ります）。`--allowed-origins http://localhost:8000` を付けると、PDF が空白になります。
  Chromium 内蔵の PDF ビューアーが読む `chrome://` と `chrome-extension://` まで止まるためです（console は `ERR_BLOCKED_BY_CLIENT`）。
  そもそも `--allowed-origins` はセキュリティの境界ではない（MCP の説明にあります。リダイレクトには効きません）ので、外しています。
  画面や PDF の内容は、エージェントにとって信頼できない入力です。厳しくしたいときは、PDF を内蔵ビューアーで開かず、画像にして確かめる方法を検討します。
- **`claude mcp add --scope local` で同じ名前（`playwright`）を登録済みのときは、そちらが優先されます。** 古い登録が
  残らないよう、`claude mcp remove playwright --scope local` で消してください。
- 再ビルドで確かめたのは、Chromium のキャッシュ（ボリューム）が残ること、MCP の登録が有効なことです。ライブラリーの導入は、再ビルド直後に apt のロック競合で失敗したため、試し直しと失敗の検知を足しました。
  試し直しは、ロックを人為的に持たせて確かめました（回復・失敗時の終了コード 1・通常の実行）。足したあとの再ビルドは、まだ行っていません（開発コンテナの中には Docker が無いため）。

## Supabase MCP（読み取り専用の共有）

リポジトリ直下の `.mcp.json` に、`supabase-read`（読み取り専用。2026-10-21 に `supabase` から改名）の定義があります。DB の中身（`documents` / `chunks` など）を、エージェントが読んで確かめるためのものです。

- **読み取り専用です。** URL の `read_only=true` で、書き込み（マイグレーションの適用、書き込みの SQL）を止めます。実際の接続で確かめました（2026-10-18）。
  `apply_migration` のツールは出ず、`CREATE TEMP TABLE` は読み取り専用のトランザクションとして拒否され、`list_tables` などの読み取りは通りました。
- **プロジェクト参照は、リポジトリに書きません。** URL の `${SUPABASE_PROJECT_REF}` は、環境変数から展開します。
  値（Supabase の Project Settings → General の Reference ID）は、自分の `~/.claude/settings.json` の `env` に `"SUPABASE_PROJECT_REF": "<参照ID>"` として書くのが主な手順です。
  このファイルは `~/.claude` のボリュームにあるので、再ビルドしても残り、リポジトリには入りません。設定したあとは `/mcp` で再接続すると反映されます。
  未設定のときは接続に失敗します（接続できません。空にすると、プロジェクトを絞らない接続になりうるので、空にはしません）。
  代わりに、ホスト側の環境変数を渡す方法もあります。`devcontainer.json` の `containerEnv` に `"SUPABASE_PROJECT_REF": "${localEnv:SUPABASE_PROJECT_REF}"` を足します（この方法では確かめていません）。
  macOS で Dock から VS Code を起動すると、シェルの環境変数が届かないことがあります。ターミナルから `code .` で開くか、設定したあとで VS Code を起動し直します。
- **認証は OAuth です。** トークンは設定ファイルに無く、初回は `/mcp` の画面でブラウザーからログインします。トークンは `~/.claude` のボリュームにあり、再ビルドのあとに再認証は要りませんでした（2026-10-18 に 2 回の再ビルドで確認。1 回の環境での結果です）。
- **書き込みは、別名のローカル登録で行います**（共有の `supabase-read` と同じ名前にすると、ローカル登録が優先されて、共有側が隠れます）。
  `claude mcp add --scope local --transport http supabase-write "https://mcp.supabase.com/mcp?project_ref=<参照ID>&features=database,docs"`。
  DDL は `db/migrations/` に SQL を残してから、`apply_migration` で適用します。参照 ID は、リポジトリにも会話の記録にも残しません。
  ローカル登録は作業ディレクトリのパスがキーです。フォルダー名を変えると、設定が残っていても外れます（2026-10-20、chatbot → docunavi で `supabase-write` が一覧から消えた）。
  そのため、`devcontainer.json` の `postStartCommand` が、起動のたびに `.devcontainer/register-supabase-write.sh` を前景で呼び、今のパスに登録し直します（登録だけで、認証はしません。待ちは最大 20 秒。ログは `/tmp/register-supabase-write.log`）。
  バックグラウンドにしないのは、子プロセスが起動処理の終了で止められるためです。`( … &)` と `setsid nohup … &` の 2 通りとも、2026-10-20 の再ビルドでログができず、登録も戻りませんでした（診断用に起動の先頭へ目印のファイルを書いて、`postStartCommand` 自体は動いていると確かめた）。
  前景の書き方は、登録を外した状態からの再ビルドで、自動で登録が戻ることを確かめました（2026-10-20）。パスを変えた直後の最初のセッションだけは、Claude Code が設定を読むほうが登録より早いので、Window の再読み込みが要ります。確かめるときは、先にそのパスの登録を外してから再ビルドします（残っていると、自動登録が失敗しても `/mcp` に出てしまうため）。
- 変数が未設定でも、`.mcp.json` 全体は壊れません（`claude mcp list` に警告が出て、`${…}` の文字のまま使われます。公式の説明で、スクラッチの読み込みでも確認しました）。Playwright は影響を受けません。

### `supabase-write` の破壊的な SQL が `declined` になる

`DELETE` などの破壊的な SQL を流すと、承認の画面が出ないまま `{"status":"declined"}` が返ることがあります（2026-10-21、`delete … where id = 22` で複数回）。

- **実測：** Supabase MCP は、破壊的な SQL の前に確認要求（elicitation。本文は `This SQL includes destructive operations` で始まる）を送ります。`settings.json` の `Elicitation` フック（matcher `supabase-write`、`.claude/hooks/accept-supabase-elicitation.py`）に、その要求が届くことは、ログで確かめました。
- **推測（未確認）：** 要求に誰も答えないために `declined` になり、VS Code 拡張が確認の画面を出せないためと見ています。拡張側の実装は確かめていません。権限のモードや `ask` ルールが原因ではありませんでした。
- **フックの既定：** `ENFORCE = False` で、記録だけして何も答えません。サーバー名・本文の接頭辞・プロジェクト参照・`mode=form` がすべて合うときだけ答える作りで、例外のときは答えません。
- **ログの見方：** `~/.claude/elicitation-hook.log` に 1 要求 1 行（`server`、`match`、`enforce`、時刻。本文は残さない）。`supabase-write` の行が増えて `match: true` なら、要求が届いたことを示します。`enforce` は、そのときのフックの設定です。
