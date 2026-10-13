# 開発コンテナの gcloud と GitHub CLI（ログインとガード）

この開発コンテナ（Claude Code を動かす環境）に、あなた自身のログインやトークンを置くときの手順と、それを守る層の説明です。
アプリを動かすだけなら読む必要はありません。コマンドはリポジトリのルートで実行します。
エージェントが守ることは [CLAUDE.md](../CLAUDE.md) にあります。

## 開発環境の gcloud（ログインとガード）

開発コンテナには gcloud が入ります（`.devcontainer/install-gcloud.sh`）。GCP の参照（Cloud Build のトリガー、Cloud Run のログなど）は、
**あなた自身のアカウントでログイン**して行います。コンテナにログイン情報があるので、次の層で守っています。

| 層 | 内容 | 効くもの |
|---|---|---|
| 日常の ID | ログイン後、`auth/impersonate_service_account` で閲覧専用の `agent-viewer` に固定します。書き込みは 403 になります | 誤操作、素朴な指示の埋め込み |
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

3. **日常の ID を閲覧専用に固定する**（統合ターミナルで実行します）

   ```bash
   gcloud config set auth/impersonate_service_account agent-viewer@<プロジェクト ID>.iam.gserviceaccount.com
   ```

   以降の gcloud は、このサービスアカウントとして動きます。書き込み系を自分で実行するときは、ホスト側で実行するのが確実です
   （コンテナでは `gcloud config unset auth/impersonate_service_account` で外せますが、戻すのを忘れないでください）。

4. **確認する**

   ```bash
   gcloud auth list                                   # ACTIVE があなたのアカウント
   gcloud config list                                 # [auth] impersonate_service_account = agent-viewer@...
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

## 開発環境の GitHub CLI（gh）

開発コンテナには `gh` が入ります（`.devcontainer/install-gh.sh`）。CI の実行結果やログ、ルールセットなど、GitHub の参照に使います。
gcloud と同じ考え方で、エージェントには**読み取り専用**だけを持たせ、書き込み（PR の作成、設定の変更）はあなたが行います。

| 層 | 内容 |
|---|---|
| トークン | **このリポジトリだけ**の細粒度トークン（fine-grained PAT）。`Actions` と `Metadata` の読み取り専用で、期限は 30 日。書き込みの権限は付けません。これが主な制御です |
| PreToolUse フック | `.claude/hooks/guard-cloud-cli.py` が gh の呼び出しも検査します（Bash と Monitor）。認証・トークンの取得、削除、秘密と設定の変更、拡張とエイリアス（コードの実行）、書き込みの GraphQL、URL 指定は拒否し、書き込み系は確認を強制します。`git credential`（保存された認証情報の取り出し）も拒否します。コマンドは、改行・`;`・パイプ・引用符・コマンド置換・ヒアドキュメントを読んで 1 つずつに分け、`bash -c`・`xargs`・`sudo`・`find -exec` などの中身も調べます |
| 権限ルール | `.claude/settings.json`: 読み取り系の gh（`run` / `pr` / `issue` / `workflow` / `ruleset` / `release` / `repo view`）だけ許可し、gh の設定ディレクトリの読み取りを拒否します。**`gh api` は許可リストに入れていません**（毎回確認します。フックの見落としが、そのまま実行にならないようにするためです） |

**gcloud との違い**: gh の設定（トークン）は、再ビルドしても残ります。名前付きボリュームを `~/.gh-config` にマウントし、
`GH_CONFIG_DIR` で gh にそこを使わせています（`~/.config/gh` に直接マウントすると、`~/.config` が root 所有で作られ、
gcloud などが書けなくなるおそれがあるためです）。読み取り専用で期限付きのトークンだけを置く前提なので、残しています。
コンテナにはキーリングが無く、トークンは平文のファイルとして保存されます。フックは境界ではありません（gcloud と同じ限界です）。
コマンド文字列の検査なので、変数やファイルに隠した操作（`$CMD`、書いてから実行するスクリプト）、パスの難読化
（`cd` した後の相対パスなど）、`ssh` や `docker exec` の先は追えません。主な制御は、トークンの権限（読み取り専用）です。

### 初回の設定

1. **トークンを作る**（GitHub のブラウザ操作）。Settings → Developer settings → Personal access tokens →
   Fine-grained tokens → Generate new token。
   - Repository access: Only select repositories → このリポジトリ
   - Repository permissions: `Actions` を Read-only（`Metadata` は自動で Read-only になります）。
     ルールセットを読みたいときは、`Administration` を Read-only にします（まず付けずに試し、読めなければ足します）
   - Expiration: 30 days
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

期限（30 日）が切れると 401 になります。新しいトークンを作って、手順 2 をやり直してください。
やめるときは、`gh auth logout` でトークンを消せます（再ビルドでは消えません）。
