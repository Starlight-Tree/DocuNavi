# 権限と資格情報の分離

プロジェクト（DocuNavi と、別の個人プロジェクト星庭ジョブ `HoshiniwaJob`）ごとに、資格情報と権限を分ける方針と、DocuNavi 側の現状です。
**状態（2026-11-15。Infisical の `docunavi-local` の行は 2026-11-18 に更新）:** 読み取りで確認した対応関係（GCP は IAM と一覧、Infisical は開発コンテナのマシン ID から見える範囲）。確認していない項目は「未確認」と書きます。
星庭ジョブ側の実装と作成の手順は、星庭ジョブのリポジトリの `docs/access-separation.md` にあります。

## 方針

1. DocuNavi 用のマシン ID・サービスアカウント・資格情報を、他のプロジェクトで再利用しない。
2. Infisical のプロジェクトは、プロジェクトごとに作る。マシン ID も、プロジェクトごとに作る。
3. GCP のサービスアカウントも、プロジェクトごとに作る。キーは作らない。
4. 秘密は、プロジェクトごとに分けて保管する。
5. 権限は、必要な秘密・リソースだけに絞る。
6. コンテナごとに、必要な秘密だけを読めるようにする。
7. 書き込みは、コマンドごとに明示して、承認を得てから行う。

分離の本体は、**プロジェクト専用のボリューム**です。Claude Code のログイン、MCP の OAuth のトークン、MCP の登録は、`~/.claude` のボリュームに入ります。
以前は、DocuNavi と星庭ジョブが同じ `claude-config` ボリュームを共有していて、DocuNavi 用の MCP の登録（user スコープ）が星庭ジョブからも見えていました。
星庭ジョブは専用のボリュームに分けました（星庭ジョブの開発コンテナを再ビルドしたあとに有効）。また、星庭ジョブのフォルダーを DocuNavi の開発コンテナに載せていた bind mount も削除しました。

## 現状の対応関係（DocuNavi）

### GCP

| プロジェクト | 主なサービスアカウント | 備考 |
|---|---|---|
| `docunavi-app` | 実行用 `docunavi-run`、ビルド用 `docunavi-build`、`docunavi-mcp`、`docunavi-ingester`、`docunavi-backup`、`docunavi-admin-run`、`budget-guard`、各スケジューラー、`r2-*` | キーを持つサービスアカウントは、確認した 4 つ（`docunavi-viewer`・`docunavi-run`・`hero-image-job`・`docunavi-build`）では 0 個。Secret は `database-url`・`gemini-api-key*`・`ingest-queue-token`・`r2-*`・`cloudflare-issuer-token` |
| `docunavi-ops` | 閲覧専用 `docunavi-viewer`（開発コンテナの日常の ID。なりすませるのは利用者だけ） | `docunavi-app` で cloudbuild・logging・run・secretmanager の閲覧と serviceUsageConsumer |
| `imagen-app-2026` | `hero-image-job`（`aiplatform.user`）、`budget-guard-disarm` | 画像生成用。Secret は `r2-docunavi-hero-*`・`budget-guard-disarm-token` |
| `docunavi-synth-261104` | `synth-runner` | |
| `wataru-sandbox` | なし | 削除済みのサービスアカウントへの権限が残っている |

### Infisical

| プロジェクト | 内容 | 誰が読むか |
|---|---|---|
| `docunavi-local`（環境 `dev`） | ローカル開発用の値（Gemini のキーなど） | 読み取り: 開発コンテナのマシン ID（Viewer。2026-11-18 にユーザーの承認で追加）とユーザーのログイン。書き込み: ユーザーのログインだけ |
| `docunavi-deploy`（環境 `prod`） | Cloudflare の API トークンと ID（デプロイ用） | GitHub Actions の OIDC のマシン ID `github-actions-deploy`（Viewer）。開発コンテナのマシン ID は入れない |
| `docunavi-prod` | 本番の認証用の値 | 利用者のログイン |
| GitHub App 用のプロジェクト | App の ID と秘密鍵 | 利用者のログインだけ（GitHub MCP のヘルパーが読む） |
| `hero-images`（環境 `dev`） | DB の URL、R2 の 4 キー | 開発コンテナのマシン ID（Viewer） |
| `workers-ai-trial`（環境 `dev`） | Cloudflare の API トークン、無料枠の Gemini のキー | 開発コンテナのマシン ID（Viewer） |
| `playwright-auth`（環境 `dev`） | 実ユーザーのセッション用（ルートは空） | 開発コンテナのマシン ID（Viewer） |

開発コンテナのマシン ID（`docunavi-devcontainer`と推定）は、上の `docunavi-local`（2026-11-18 に追加）と、最後の 3 つだけが見えます。`docunavi-deploy` などは見えません。

### MCP

| 名前 | スコープ | 資格情報 | 状態 | 性質 |
|---|---|---|---|---|
| `playwright` | user | なし | 登録済み | 汎用 |
| `infisical` | user | マシン ID（Viewer。`~/.infisical/identity`） | 資格情報は有効（読み取りでログインできた） | DocuNavi 専用 |
| `github` | user | GitHub App のトークン（ヘルパーが都度作る） | ヘルパーが今動くかは未確認 | DocuNavi 専用（Actions の起動と監視） |
| `cloudflare` | user | OAuth | **認可なし**（トークンなし） | DocuNavi 専用 |
| `langsmith` | user | OAuth | **認可なし** | DocuNavi 専用 |
| `app-docunavi` | local | `gcloud` の ID トークン（利用者本人） | ヘルパーが今動くかは未確認 | DocuNavi 専用 |
| `perplexity` | local | OAuth | **認可なし** | 汎用寄り |
| `plugin:cloudflare`・`plugin:prisma` | プラグイン | OAuth | **認可なし** | DocuNavi 専用 |

**スコープの方針:**
- ボリュームがプロジェクト専用なら、user スコープでも、他のプロジェクトには見えません。そのため、今は user のままにします（local は、パスごとの登録で、フォルダーを移すと失われます。2026-11-09 に Infisical の登録を local から user に変えた理由）。
- ボリュームを他のプロジェクトと共有し直すときは、プロジェクト固有の MCP（`infisical`・`github`・`cloudflare`・`langsmith`・`prisma`）を local に直します。汎用の `playwright` は user のままです。
- OAuth の MCP は、今は認可を持っていません。認可するときは、権限の範囲を最小にします。Cloudflare の認可がアカウント単位でしか絞れないかは未確認です。

## 整理の候補（未実施）

- `cloudflare` が、直接の登録とプラグイン（`plugin:cloudflare`）で二重になっている。LangSmith は直接の登録だけ。どちらかに寄せる。
- 既定の Compute Engine のサービスアカウントと appspot のサービスアカウントに、`editor` が付いている（`docunavi-app`・`imagen-app-2026`・`docunavi-synth-261104`）。外す前に、使っている資源を確認する。
- `wataru-sandbox` の、削除済みサービスアカウントへの権限が残っている。
- 画像生成の基盤 `imagen-app-2026` の台帳が、星庭ジョブのリポジトリにある（DocuNavi 用の試作）。台帳の置き場所を決める。
- 共有ボリュームに残っている星庭ジョブの履歴のコピー（`-workspaces-hoshiniwa-job` と `-workspaces-hoshiniwajob`）。星庭ジョブの開発コンテナで履歴が見えることを確かめたあとで削除する。
- `.devcontainer/README.md` の、Infisical の MCP の記述が古い（実際の起動スクリプトは `list-secrets` だけに絞っている）。

## 未確認

- 星庭ジョブの開発コンテナの再ビルドと、各 MCP の接続。
- `github` と `app-docunavi` のヘルパーが、今も動くか。
- `github-actions-deploy` を含む、Infisical のマシン ID の一覧と、プロジェクトごとの権限（開発コンテナのマシン ID から見えた範囲だけを確認した）。
