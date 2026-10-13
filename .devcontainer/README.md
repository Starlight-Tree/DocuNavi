# 開発コンテナの gcloud と GitHub CLI（ログインとガード）

この開発コンテナ（Claude Code を動かす環境）に、あなた自身のログインやトークンを置くときの手順と、それを守る層の説明です。
アプリを動かすだけなら読む必要はありません。コマンドはリポジトリのルートで実行します。
エージェントが守ることは [CLAUDE.md](../CLAUDE.md) にあります。

## 開発環境の gcloud（ログインとガード）

開発コンテナには gcloud が入ります（`.devcontainer/install-gcloud.sh`）。GCP の参照（Cloud Build のトリガー、Cloud Run のログなど）は、
**あなた自身のアカウントでログイン**して行います。コンテナにログイン情報があるので、次の層で守っています。

| 層 | 内容 | 効くもの |
|---|---|---|
| 日常の ID | コンテナの環境変数 `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`（`devcontainer.json` の `containerEnv`）で、閲覧専用の `agent-viewer` に固定します。起動した時点から、ターミナルでもエージェントでも閲覧専用で、書き込みは 403 になります | 誤操作、素朴な指示の埋め込み、戻し忘れ |
| PreToolUse フック | `.claude/hooks/guard-cloud-cli.py` が、実行前に gcloud などの呼び出しを検査します。削除・IAM・課金の変更・認証情報の取得・ID の切り替えは拒否し、書き込み系は確認を強制します | 同上、設定の書き換え |
| 権限ルール | `.claude/settings.json`: 読み取り系の gcloud だけ許可し、`~/.config/gcloud` と `.gcloud-token` の読み取りを拒否し、bypass モードを無効にします | 同上 |
| GCP 側 | 予算アラート、プロジェクトの削除防止（lien） | 最後の砦 |

**限界**: これらはセキュリティ境界ではありません。コンテナにはあなたのログイン情報があるので、フックを通らない経路
（スクリプトが認証情報のファイルを直接読むなど）までは防げません。権限ルールもコマンド文字列の照合で、Claude Code の公式文書も
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

   ```bash
   ./scripts/setup-agent-viewer.sh <プロジェクト ID>      # 内容を表示して確認を取ります。何度実行しても構いません
   ```

   実際の操作は次のコマンドと同じです。

   ```bash
   PROJECT_ID=<プロジェクト ID>
   ME=<あなたのアカウント>
   SA=agent-viewer@${PROJECT_ID}.iam.gserviceaccount.com

   gcloud services enable iamcredentials.googleapis.com --project "$PROJECT_ID"
   gcloud iam service-accounts create agent-viewer --display-name="Agent read-only" --project "$PROJECT_ID"
   for role in roles/cloudbuild.builds.viewer roles/run.viewer roles/logging.viewer; do
     gcloud projects add-iam-policy-binding "$PROJECT_ID" \
       --member="serviceAccount:${SA}" --role="$role" --condition=None
   done
   gcloud iam service-accounts add-iam-policy-binding "$SA" \
     --member="user:${ME}" --role=roles/iam.serviceAccountTokenCreator --project "$PROJECT_ID"
   ```

   `logging.viewer` はプロジェクト内のログをすべて読めます。同じプロジェクトに別用途のログがあるなら、外すか絞ってください。

3. **日常の ID は、環境変数で閲覧専用に固定されています**（手順はありません）

   `.devcontainer/devcontainer.json` の `containerEnv` が `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` を設定します。
   コンテナを作り直したあとは、手順 1 のログインだけで、閲覧専用の ID で動きます（設定を打ち直す必要はありません）。

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

   - `WARNING: ... All API calls will be executed as [agent-viewer@...]` が出ていれば、閲覧専用の ID で動いています。
   - `PERMISSION_DENIED: Failed to impersonate` は、作成直後の IAM の反映待ちです（数分で解消します）。
   - `API [...] not enabled on project` は、そのプロジェクトで該当の API が無効、という意味です。なりすまし中の呼び出しは、
     プロジェクトの API の有効化状況に従います。使う API（Cloud Build、Cloud Run、Logging など）を有効にしてから確認してください。
   - 閲覧専用の ID は、付与した 3 つのロールの範囲しか読めません。IAM やサービスの一覧などが拒否されるのは、設計どおりです。

5. **終わったら**、コンテナの統合ターミナルでログアウトします。ログイン情報はボリュームに残しません（再ビルドでも消えます）。
   これはコンテナ内のログインを消す操作で、ホストの gcloud のログインを消す手順ではありません
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
| 読み取り | 何も付けない | `agent-viewer` |

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
また、この方式ではトークンがすでに `agent-viewer` のものなので、環境変数の既定（なりすまし）を外して使います
（`env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud ... --access-token-file=.gcloud-token`）。
外さないと、`agent-viewer` が自分自身になりすまそうとして失敗します。

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
