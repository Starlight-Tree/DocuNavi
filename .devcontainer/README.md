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

開発コンテナには `gh` が入ります（イメージのビルド時。`.devcontainer/Dockerfile`。`install-gh.sh` は残してあるが、`postCreateCommand` からは呼ばない）。CI の実行結果やログ、ルールセットなど、GitHub の参照に使います。
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

## エージェント運用の補足（gcloud・gh・MCP）

2026-11-18 に、ルートの [CLAUDE.md](../CLAUDE.md) の「gcloud」「MCP の接続」「gh」の節から、手順と背景の説明をここへ移した（本文は変えていない）。
**禁止と承認の規則は CLAUDE.md に残してある**（止められたら回避しない・フックの「確認」は承認なしに進めない・ガードを外さない・読み取りだけ実行する・トークンを出さない、など）。ここは、その背景と手順の補足。

### gcloud（ユーザーのログイン + ガード）の補足

- 開発コンテナの gcloud には、**ユーザー自身のアカウントでログインしている**（ユーザーが決定。「開発環境の gcloud（ログインとガード）」）。
- 日常の ID は、環境変数 `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`（`.devcontainer/devcontainer.json` の `containerEnv`）による、閲覧専用の `docunavi-viewer`（`docunavi-ops` のサービスアカウント。書き込みは設計上 403）へのなりすまし。
  コンテナの再ビルド前は未設定のことがある。その場合も、エージェントは自分では設定せず、ユーザーに伝える。
- 書き込みのコマンドに、誰の権限で動かすかを明示する方法は、準備は `env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`、
  日常のデプロイは `--impersonate-service-account=agent-deployer@…`（「書き込みをするとき」）。
- ログインの起動（引数なしの `gcloud auth login`）は、フックが毎回「確認」を返す。ブラウザーでの選択と承認は、ユーザーが行う。
  開発コンテナにはブラウザーが無く、ユーザーのブラウザーへ戻れるかは未確認。届かなければ、ユーザーが自分の端末でログインする。
- ログイン後にエージェントが実行するのは、`docunavi-viewer` の権限で読める範囲（ビルド・Cloud Run・ログの閲覧）だけ。
  `gcloud builds …` は `--billing-project=docunavi-app` を付ける（付けないと API が `docunavi-ops` に数えられて失敗する）。
  課金（`gcloud billing projects describe`）と有効な API の一覧（`gcloud services list`）は `docunavi-viewer` の権限外なので、
  ユーザー自身の権限（`env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud …`）で、ユーザーが実行する。
- 外さないと実行できない書き込みは、エージェントは自分では外さず、コマンドを提示してユーザーに実行してもらう。
- ログアウト（`gcloud auth revoke --all`）は、毎回の作業の終わりには求めない（ユーザーが決定）。促すのは、長期に使わないとき、共有の端末、不審な挙動のときだけ。
  日常の ID を固定していると素のままでは失敗することがある。失敗したら、ユーザーに実行してもらう。ログインはボリュームに残る（2026-10-21 から。再ビルドでも消えない）。
  コンテナの中にはユーザー自身のログイン（リフレッシュトークン）が残る（フックを通らない経路では読める。上の「限界」）。SA のキーは作らない・保存しない。

### gh（読み取り専用のトークン + ガード）の補足

- 開発コンテナの gh には、**このリポジトリだけの読み取り専用トークン**（`Actions` と `Metadata`）を置く。再ビルドしても残る（`GH_CONFIG_DIR=~/.gh-config`。「開発環境の GitHub CLI（gh）」）。
- 使うのは `gh run list/view`、`gh pr view`、`gh ruleset view` など。`gh api` は許可リストに無く、毎回確認が出る（GET だけ使う）。
  コンテナの中でのユーザー自身の gh ログインは勧めない（`GH_CONFIG_DIR` を共有するため、フル権限のトークンがエージェントから読めてしまう）。
- 401 が返ったら期限切れの可能性が高いので、ユーザーに更新を頼む。

### GitHub MCP（`github`。Actions の起動と監視だけ）の補足

- user スコープ。トークンは GitHub App（`docunavi-agent-actions`）の installation トークン（このリポジトリだけ、`Actions: write` `Metadata: read`、1 時間）で、
  `headersHelper`（`scripts/github-mcp-header.sh`）が、Infisical（`docunavi-github`）の秘密鍵から作る。App の作成は `scripts/github-app-setup.py`（ホストで 1 回）。機能は `actions` だけに絞る。
- エージェントが使うのは `actions_run_trigger`（`run_workflow`、`deploy.yml` を `main` で）と、`actions_list` `actions_get` `get_job_logs`（監視）。
  `rerun` `cancel` は、言葉で頼まれたときだけ（「y」だけでは足りない）。ヘルパーはエージェントが実行せず、トークンを表示・保存しない。
- Environment「production」の必須レビュアーは外してある（許可するブランチは `main` だけ。経緯は `docs/deploy.md`）。`deploy.yml` の起動の承認は、会話の約束だけで、技術的な関門ではない（承認の規則は CLAUDE.md の「Git」の「CI」）。

### MCP の接続（app-docunavi）の補足

- 非公開の `docunavi-mcp` へは、`headersHelper`（`scripts/mcp-auth-header.sh`）で直接つなぐ。ローカルプロキシは使わない。診断と経緯は [docs/mcp-tool-design.md](../docs/mcp-tool-design.md)。
- ヘルパーは Claude Code が実行する。ガードのフックも確認も通らず、ユーザー自身の権限で動く（標準出力が ID トークンになるので、エージェントは実行しない）。
- 画面の Authenticate を押すと、ヘッダー無しの OAuth 探索になり、403 で失敗する。
- 旧プロキシ `scripts/mcp-proxy.sh` を実行するときは、実行前に `git diff 399f592 -- scripts/mcp-proxy.sh` が空であることを確かめる。自動再起動ループは足さない。

## 開発環境のブラウザー確認（Playwright MCP）

開発コンテナには、エージェントが画面を開いて確認するための Playwright MCP が入ります。資料一覧や PDF の表示、チャットの出典、
console と network のエラーを、スクリーンショット付きで確かめるために使います。アプリのコードや本番の環境には関係しません。

| 項目 | 内容 |
|---|---|
| 登録 | user スコープ（`~/.claude/.claude.json`。ボリュームに残る。2026-11-09 に `.mcp.json` から移した）。`@playwright/mcp@0.0.83 --headless --browser chromium --isolated`。`.mcp.json` は空で、プロジェクトの MCP の信頼確認は出ません |
| Chromium | `.devcontainer/install-playwright.sh` が `~/.cache/ms-playwright` に取得します（約 260 MB）。ここは名前付きボリュームなので、再ビルドしても取得し直しません |
| ライブラリー | `.devcontainer/Dockerfile` が、イメージのビルド時に apt で入れます（Chromium の実行に要るライブラリーと、日本語を含むフォント。`playwright install-deps` の結果を一覧にしたもの）。作成のたびには入れ直しません（イメージのキャッシュが効く）。Playwright の版を上げるときは、`install-deps` で要るライブラリーが変わっていないかを見て、Dockerfile の一覧を作り直します |
| 成功の判定 | 最後に Chromium を実際に起動して（`chrome --version`）確かめます。ライブラリーの取りこぼしも、ここで分かります。だめなら理由を stderr に出して終了コード 1 で終わり、作成のログに「Playwright の導入に失敗しました」と出ます（あとの確認と導入は続きます）。直したら `bash .devcontainer/install-playwright.sh` を再実行します |
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
- **`claude mcp add --scope local` で同じ名前（`playwright`）を登録したときは、user スコープより local が優先されます。** ログイン状態つきの確認が済んだら、
  古い登録が残らないよう、`claude mcp remove playwright --scope local` で消してください（`docs/development.md` の手順は、確認のあとも残す決定のため、消すかは利用者が決めます）。
- 再ビルドで確かめたのは、Chromium のキャッシュ（ボリューム）が残ること、MCP の登録が有効なことです。ライブラリーの導入は、以前は `postCreateCommand` の apt で、再ビルド直後にロックの競合で失敗したことがありました。
  今はライブラリーをイメージに入れたので、`postCreateCommand` に apt は残りません（ロックの競合も、試し直しの処理もありません）。移したあとの再ビルドで、Chromium の起動（`--version` と、ヘッドレスでの DOM の出力）と Playwright MCP での画面の操作を確かめました。

## 開発環境の Infisical（2026-11-02。設定の追加まで。リビルドと検証は未実施）

開発用の秘密情報の置き場（Infisical Cloud の Free プラン）。本番（Cloud Run・Cloudflare の Worker）の秘密は対象外。サードパーティの Feature は使わない。

- **CLI:** `Dockerfile`（公式の .deb v0.44.11。公式の `checksums.txt` の SHA-256 を照合。arm64 だけ）。`cli-tools` が PATH にあるかを見る。テレメトリーと更新確認は止めてある（`INFISICAL_DISABLE_*`）。
- **運用は CLI のみ**（ユーザーが決定、2026-11-02）。VS Code 拡張（`Infisical.infisical-vscode`）は使わない。`devcontainer.json` の `extensions` にも入れない。
- **ログイン状態（2 つは別物）:**
  - CLI のマシンアイデンティティ（**Viewer 固定**）: `~/.infisical/identity/` の `client-id` と `client-secret`（ボリューム `infisical-config`。600）。ボリュームが残る限り残る。
  - `infisical login`（ユーザーのログイン。2026-11-07 から、利用者の決定で残す）: データ本体（`~/infisical-keyring`）は、`persist-infisical-login.sh` がボリューム（`~/.infisical/keyring`）へ移して symlink にする（`postStartCommand` が毎回呼ぶ）。再ビルドでも残る。暗号化のパスフレーズは同じボリュームの `infisical-config.json` にあるので、保管時の保護ではない。消すときは `infisical logout`。（方針は `.claude/rules/secrets.md`）
- **資格情報を置く（利用者が端末で。値は引数・履歴・チャットに出ない）:**
  `bash -c 'umask 077; d=$HOME/.infisical/identity; mkdir -p "$d"; read -rp "Client ID: " i; printf %s "$i" > "$d/client-id"; read -rsp "Client Secret: " s; echo; printf %s "$s" > "$d/client-secret"; unset i s'`
- **再ビルド後の確認（読み取りだけ。値は表示しない）:**
  1. `infisical --version`（0.44.11）と `dpkg -V infisical`（出力なし）。
  2. `stat -c '%a %U' ~/.infisical`（700 vscode）。`ls -A ~/.infisical`（初回は空）。
  3. `ls ~/.vscode-server/extensions | grep -ci infisical`（0。拡張は入れない）。
- **公式 MCP（`infisical`。2026-11-07）:** `.devcontainer/infisical-mcp.sh`（`@infisical/mcp@0.0.25`）を、`register-infisical-mcp.sh` が user スコープに登録する（起動のたびに `postStartCommand` が呼ぶ。登録は `~/.claude` のボリュームに残る。2026-11-09 に local から変えた）。資格情報は登録に入れず、`~/.infisical/identity` から起動時に読む（Client Secret を発行し直しても、登録はそのまま）。使えるツールは `list-secrets` だけ（`infisical-mcp.sh` の `INFISICAL_ENABLED_TOOLS` で絞っている）で、値は `<masked>` になる。マシン ID は viewer なので、書き込みは権限でも断られる。**秘密の値は MCP で入れない**（値がエージェントの会話を通る）。値の登録は、利用者が端末で CLI を使う（`read -rs` で受け、一時ファイルを `infisical secrets set --file` に渡す）。`identity` の形式は `INFISICAL_UNIVERSAL_AUTH_CLIENT_ID=…` と `…_CLIENT_SECRET=…` の 2 行の 1 ファイル（上の `client-id` `client-secret` の 2 ファイルという記述は古い）。
- **MCP のヘルパーが失敗したときの診断（2026-11-11）:** GitHub MCP の `headersHelper`（`scripts/github-mcp-header.sh`）が失敗すると、Claude Code は「Incompatible auth server: does not support dynamic client registration」と出す。OAuth の問題ではなく、ヘッダーが渡らなかった結果。原因は、ヘルパーの標準エラーの先頭の「[原因: …]」で区別される（root で実行された / Infisical のユーザーログインが無い / ヘルパー自体の失敗）。`--diagnose` を付けると、トークンを作らずに、実行ユーザー・Infisical のログイン・秘密の取得だけを確かめる（標準出力は「OK」の 1 行で、秘密は出ない）。必ず vscode ユーザーで実行する（root の HOME にはログインが無い）:
  `docker exec -u vscode -e HOME=/home/vscode docunavi-devcontainer env GITHUB_MCP_PROJECT_ID=<ID> bash /workspaces/docunavi/scripts/github-mcp-header.sh --diagnose`
  ログインが無いときは、コンテナの端末（vscode ユーザー）で `infisical login`（ブラウザーでの承認が要る）。root の `docker exec` で `claude` を実行してはいけない。原因（2026-11-11 に設定のコピーで再現）: コンテナの環境変数 `CLAUDE_CONFIG_DIR=/home/vscode/.claude` は root にも効くので、root の `claude mcp list` は vscode と同じ設定を使い、`.claude.json` と `.credentials.json` を書き直して所有者を `root:root`（600）にする。vscode ユーザーが読めなくなり、MCP の登録が消えたように見える。修復は、ホストで `docker exec -u root docunavi-devcontainer chown -R vscode:vscode /home/vscode/.claude`。確認は設定のコピーに対して行う（`CLAUDE_CONFIG_DIR` を一時ディレクトリにする）。再発防止として、`/usr/local/bin/claude` を、root の実行を断るラッパー（`.devcontainer/claude-root-guard.sh`。本物は `/usr/local/libexec/claude-real`）にした（2026-11-11）。**再ビルドするまでは効かない**。ラッパーの動作（root は exit 1、vscode は本物に渡す）はコピーで確認したが、イメージのビルドと、再ビルド後のコンテナ・VS Code 拡張での動作は未確認。
- **消し方:** `docker volume rm infisical-config-<id>`（ホスト）、または `rm -rf ~/.infisical/*`（コンテナ）。資格情報の失効は、Infisical の画面（Machine Identities）で行う（手順は未確認）。
- **注意:** Free プランにはシークレットの版管理も監査ログも無い。CLI での更新・削除は戻せないので、原本は別の場所にも残す。

## 開発環境の Docker（読み取り専用のソケットプロキシ）

**状態（2026-10-21 JST 夜）: 動いた。** プロキシは `healthy`、開発コンテナから `docker ps` が通り、inspect・exec・rm・info・images は拒否された。
最初の起動は、プロキシが OrbStack のソケット（`root:root 0660`）を開けず再起動を繰り返した（`group_add: ["0"]` で直した）。
**`docker logs` と `docker compose ps/logs` は使えない**（CLI が先に inspect を呼ぶため。下の「許可するもの」）。ログは `scripts/dlogs.sh` で読む。
Container Tools の画面は、一覧が見える（検証 7）。未確認: コンテナの詳細の表示、拒否の記録の形式。

開発コンテナの VS Code（Container Tools）とエージェントから、OrbStack 上の `docunavi-api` と `docunavi-frontend` の状態・ログを**見るだけ**にするための構成です。
ホストの Docker ソケットは、開発コンテナに渡しません（ソケットを持つ＝ホストの root とほぼ同じため）。

| 項目 | 内容 |
|---|---|
| プロキシ | `.devcontainer/docker-proxy/compose.yml`。`wollomatic/socket-proxy:1.13.1`（固定）。ホストのソケットを持つのはこのコンテナだけで、ポートはホストに公開しない |
| 起動 | `devcontainer.json` の `initializeCommand`（ホストで、コンテナの作成の前に実行）。手動なら `docker compose -f .devcontainer/docker-proxy/compose.yml up -d` |
| 接続 | 開発コンテナを専用のネットワーク `docunavi-dev` に繋ぎ（2026-10-22 から `.devcontainer/docker-compose.yml` の `networks`。それ以前は `runArgs`）、`DOCKER_HOST=tcp://docunavi-docker-proxy:2375`。このネットワークに繋がるのは、プロキシと開発コンテナだけ（`docunavi-api`・`docunavi-frontend`・`docunavi-db` は入れない） |
| CLI | `Dockerfile` が Debian の `docker-cli` と `docker-compose`（`docker compose` のプラグイン）を入れる。デーモンは入らない |
| 拡張 | `ms-azuretools.vscode-containers`（Container Tools） |

### 許可するもの（GET だけ）

`/_ping` `/version` `/containers/json`（一覧。`docker ps`）`/containers/<名前か ID>/logs`（`docker logs`）。API のバージョン接頭辞 `/v1.NN` は任意。HEAD は `/_ping` だけ。
`/_ping` と `/version` は、Docker CLI が最初に呼ぶ（無いと接続の確認で失敗する）ので足した。

**許可しないもの:** `/containers/<id>/json`（inspect）、`archive` `export` `top` `exec`、`/events`、`/info`、volumes・networks・secrets・images、POST・PUT・DELETE のすべて。未指定のメソッドは拒否される。

- **`docker inspect` は使えない。** 稼働中のコンテナの環境変数が見えるため。`api` は `env_file: .env` なので、`GEMINI_API_KEY` などが出る。
- **`docker compose config` も使わない。** プロキシは通さないが、コンテナの中の `.env` を読んで展開するため、`Read(./.env)` の拒否の抜け道になる。
- **`docker logs` と `docker compose ps` / `logs` は、Forbidden になる**（2026-10-21 JST に実測）。CLI がコンテナごとに inspect を呼ぶため。`docker ps` は通る。
  ログは `scripts/dlogs.sh <コンテナ名> [行数]` で読む（許可している `GET /containers/<名前>/logs` だけを呼ぶ。名前は `[a-zA-Z0-9_.-]` に限る）。
  inspect を許可すれば CLI は動くが、環境変数が見えるので許可しない。

### 限界

- **OrbStack 上のすべてのコンテナの一覧とログが見える。** プロキシはパスでしか絞れず、docunavi のものだけには絞れない。ログに秘密を出さない運用が前提（アプリは出さない設計）。
- **プロキシ自体がホストのソケットを持つ。** 許可リストの書き間違い（特に POST の許可）は、ホストの root と同じになる。変えるときは、必ず下の検証をやり直す。
- **コンテナの起動・停止・削除は、ここからはできない。** `docker compose up / down / restart` はホストで行う。`POST` を許すと `/containers/create` も通り、ホストのパスをマウントしたコンテナを作れるので、許さない。
- 接続元の制限は、プライベートアドレス全体（`172.16.0.0/12` `192.168.0.0/16` `10.0.0.0/8`）。ネットワークに繋がるのが 2 つだけ、という構成に頼っている。
- `initializeCommand` が失敗する（ホストで `docker` が見つからない、など）と、開発コンテナの作成が止まる。

### 検証（ホストで。上から順に）

結果（2026-10-21 JST）を各項目に添える。

1. ホストで `docker compose -f .devcontainer/docker-proxy/compose.yml up -d`。`docker ps` に `docunavi-docker-proxy` が `healthy` で出る。
   **結果:** 最初は `Restarting`（ログ: `socket not available … permission denied`）。`group_add: ["0"]` を足して `Up (healthy)`。`./healthcheck` も通った。
2. 開発コンテナを再ビルド（`Dev Containers: Rebuild Container`）。作成のログに、Dockerfile の `docker --version` と `docker compose version` が出る。
   **結果:** 再ビルド済み。`docker` 26.1.5 が入り、`DOCKER_HOST` が設定された。プロキシが落ちている間は、名前が引けなかった。
3. 開発コンテナで `docker ps`: `docunavi-api` と `docunavi-frontend` が出る（Compose を起動しておく）。
   **結果:** 出る（`healthy`）。OrbStack 上のほかのコンテナも見える。`docker logs`・`docker compose ps/logs` は Forbidden（上の「許可するもの」）。代わりに `scripts/dlogs.sh docunavi-api 3` が読める。
4. 拒否の確認: `docker inspect` `docker exec` `docker rm -f` `docker info` `docker images`（存在しない名前で実施）。
   **結果:** inspect・exec・info・images は `Forbidden`、`rm -f` は `Method Not Allowed`。
5. `/var/run/docker.sock` が開発コンテナに**無い**こと（`ls /var/run/docker.sock`）。**結果:** 無い。
6. 再ビルドしても、`~/.claude`（会話の履歴・MCP の登録）、`~/.gh-config`（`gh auth status`）、`~/.config/gcloud`（`gcloud auth list`）が残っていること。
   **結果:** 3 つとも残っていた（`${devcontainerId}` が変わらないことは、これで確認とした）。
7. Container Tools の画面に、コンテナが見えること。**inspect を拒否されて欠ける表示**（コンテナの詳細、環境変数など）を記録する。許可を広げるかは、記録を見てユーザーが決める。
   **結果:** 一覧は見える（ユーザーが画面で確認、2026-10-21 JST）。Containers に `docunavi-dev-proxy`（Compose プロジェクト。中にプロキシ）、Individual Containers に開発コンテナと別プロジェクトのコンテナ。
   Images と Volumes は "Failed to connect. Is Docker running? Error response from daemon: Forbid…" と出る（`/images` と `/volumes` が許可リストに無いため。想定どおり）。
   OrbStack 上のすべてのコンテナが見えるのも、「限界」のとおり。未確認: コンテナを開いたときの詳細の表示。
   開発コンテナの名前は、自動の `vsc-docunavi-<ハッシュ>` だと見分けにくいので、`--name=docunavi-devcontainer` で固定した（`runArgs`。Compose ベースにした後は `container_name`）。

### 開発コンテナの構成（Compose。2026-10-22）

**状態（2026-10-22 JST）: 再ビルド済み。開発コンテナの中から確認できた範囲は、設計どおり。** 結果は下の「確認」の各項目。ホスト側の確認（項目 6、`stopCompose`）は未実施。

開発コンテナは Dockerfile ベースから Compose ベースに替えた。`devcontainer.json` の `dockerComposeFile` が `../docker-compose.yml`（db・api・frontend）と
`.devcontainer/docker-compose.yml`（dev）を重ねて読み、開発コンテナを開くと `db` `api` `frontend` も起動する（`runServices`）。

**`api-prod`（profile `prod`、2026-10-26）は `runServices` に含めない。** 本番相当の起動の確認用で、開発コンテナを開いても起動しない（`stopCompose` が、起動中の `api-prod` も止めるかは未確認）。
使うときはホストで名前を指定して起動する（手順は [docs/development.md](../docs/development.md) の「本番相当の起動の確認」）。

**2026-10-24:** `frontend` サービスは、Vite の開発サーバー（:5173）から `frontend/` の next dev（:3000）に置き換えた（サービス名・コンテナ名 `docunavi-frontend` は同じ）。以下の確認の記録は、Vite 版の `frontend`（5173 番。現在の `frontend/` とは別物）のもの。Next.js 版の Compose での起動は未検証。

| 項目 | 内容 |
|---|---|
| コンテナ | 5 つ: `docunavi-devcontainer`（dev）、`docunavi-docker-proxy`（別の Compose プロジェクト）、`docunavi-db`・`docunavi-api`・`docunavi-frontend` |
| ネットワーク | 3 つ: `docunavi-dev`（dev・プロキシ）、`docunavi-frontend-net`（frontend・api）、`docunavi-backend-net`（api・db。internal）。dev は `docunavi-dev` だけ（api・db に直接は届かない） |
| 設定の置き場所 | dev の定義は `.devcontainer/docker-compose.yml`。`containerEnv` は `devcontainer.json` にも残し（場所を変えない。CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT を含む）、同じ値を dev の `environment` にも置く。変えるときは両方 |
| ボリューム | 名前は今のまま（`claude-code-config-<ID>` など 5 つ）。Compose の `volumes` で名前を指定した。履歴・ログイン・トークンは引き継がれる想定 |
| プロジェクト名 | `docker-compose.yml` の `name: docunavi` で固定（ホストの `docker compose up` と同じコンテナ・ボリュームを使う） |
| 閉じたとき | 2026-11-11 から `shutdownAction: none`。ウィンドウの切断（Host ⇄ 開発コンテナの切り替え・再読み込み・閉じる）で止まらない。止めるときは明示的に（docs/development.md「開発コンテナの停止と切り替え」）。以前の `stopCompose` は、切断の 10 秒後までに再接続が終わらないと `stop -t 0 dev` で止まった。画面 3000 は `ports: 127.0.0.1:3000:3000` で公開。サーバー（Worker・API・画面）は `postStartCommand` の `dev-local.sh autostart` が、Dev Containers 経由の起動のたびに起こす（動いていれば何もしない。`docker start` 単体では実行されない。結果は `/tmp/docunavi-dev-local/autostart.log`） |
| api が失敗するとき | 実キーの `.env` で db が空だと、api は起動の検査に失敗する（`uvicorn --reload` なので、コンテナは落ちず `unhealthy` のまま止まる。再起動はされない）。開発コンテナ経由の起動だけ、`frontend` の待ちを `service_started` に緩めて、作成が止まらないようにした。資料を取り込み、`docker compose restart api` するまで api は healthy にならない（2026-10-22 に取り込み済み。healthy を確認） |

**再ビルドは VS Code の「Dev Containers: Rebuild Container」で行う（2026-11-11）。** ホストの `docker compose build dev` は、node・python の feature
が入らないイメージ（`docunavi-dev:latest`）を作り、`npx`・`python3` が無くなって MCP（github・infisical・playwright）が起動しなくなる。
feature は Dev Containers のビルドでだけ入る。

**再ビルドの前（ホストで）:** 古い Dockerfile ベースのコンテナを消す。名前が衝突するため。ボリュームは消えない（`-v` を付けない）。
`docker rm -f docunavi-devcontainer`。

**確認（再ビルドの後）:**
1. 開発コンテナが開く。作成が失敗したら、VS Code の「Open in recovery container」から、ログ（`docker compose up` のエラー）を見る。
   **結果:** 開いた。作成は止まらなかった。
2. `docker ps`（開発コンテナの中）に `docunavi-db` `docunavi-api` `docunavi-frontend` `docunavi-docker-proxy` が出る。
   **結果:** 5 つとも出た（db・frontend・proxy は healthy、frontend だけが 5173 番を公開）。ホストの `docker volume ls` で名前が増えていないかは未確認（プロキシはボリュームを見せない）。
3. **`echo "${CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT:+設定あり}" "$DOCKER_HOST"`（値は出さない）で、閲覧専用の固定とプロキシの接続が効いている。**
   **結果:** どちらも効いている。`containerEnv` と `environment` のどちらが効いたかは分からない（結果は同じ。両方に置いたまま残す）。
4. `~/.claude`（履歴・メモリ・MCP の登録）、`gh auth status`、`gcloud auth list` が残っている。
   **結果:** 残っていた（メモリ 19 ファイル、gh・gcloud のログイン）。5 つのボリュームは以前と同じ名前でマウントされていた。
5. 実キーの `.env` で db が空のとき、api は起動の検査に失敗して `unhealthy` で止まり（再起動はされない）、開発コンテナの作成は止まらない。
   **結果:** api は `unhealthy`（ログは `relation "documents" does not exist`）。frontend は起動して healthy（`service_started` への上書きが効いた）。
   開発コンテナからは `docunavi-api:8000` と `docunavi-frontend:5173` に届かない（設計どおり）。
6. ホストで `docker compose up -d` を同じプロジェクト（`docunavi`）に対して実行しても衝突しない。**未確認**（ホストで `docker compose ls` も見る）。
7. 未確認: ウィンドウを閉じたときの `stopCompose`、作成時のプロジェクト名が `docunavi` になること（`_devcontainer` が付かないこと。コンテナ名が固定なので、中からは区別できない）。
   `remoteUser: vscode` と features（node・python）の組み合わせは、`vscode` ユーザーで開けたので動いている。

### Container Tools が要求する API を調べる

許可を足す前に、拒否された要求を見る。ホストで `DOCKER_PROXY_LOGLEVEL=DEBUG docker compose -f .devcontainer/docker-proxy/compose.yml up -d`（環境変数を変えて作り直す）。
開発コンテナで Container Tools を開き、`docker logs docunavi-docker-proxy` で、拒否された要求のメソッドとパスを見る（**拒否が記録される形式は未確認**。出ないときは、確かめ方を決め直す）。
全メソッドを一時的に許す方法（`-allowGET=.*`）は使わない。見終わったら、`DOCKER_PROXY_LOGLEVEL` を付けずに作り直して INFO に戻す。

### 権限設定（`.claude/settings.json`）

このリポジトリでは、まだ変更していない（変更案は、ユーザーの承認を得てから）。実際の境界はプロキシで、権限設定は二重の安全策。
