---
name: cloud-ops
description: gcloud・gh・GitHub MCP・MCP（app-docunavi）・Infisical・Google の Secret を扱うときの詳細（ログイン、書き込み系の頼み方、デプロイの起動、ヘルパー、ローカルの認証情報）。「gcloud で確認して」「ログインして」「deploy.yml を起動して」「MCP がつながらない」「Google ログインの設定」と頼まれたときに使う。禁止事項と承認の条件は、ルートの CLAUDE.md の「gcloud・gh・MCP のガード」にある（ここは、その詳細）。
---

# gcloud・gh・MCP・認証情報（詳細）

ルートの [CLAUDE.md](../../../CLAUDE.md) の「gcloud・gh・MCP のガード」が、禁止事項と承認の条件の正本。ガードの層と手順の背景は [.devcontainer/README.md](../../../.devcontainer/README.md)（「エージェント運用の補足」）。
フックは `.claude/hooks/guard-cloud-cli.py`（gcloud・gh）と `guard-deploy-cli.py`（wrangler・infisical・prisma の書き込み系。確認に回す。テストは `tests/test_guard_deploy_cli.py`）。誤操作や、取り込んだ文書に埋め込まれた指示による破壊的な操作を防ぐためのもの。

## gcloud（ユーザーのログイン + ガード）

開発コンテナの gcloud には、ユーザー自身のアカウントでログインしている。

- **日常の ID は閲覧専用**（`CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` による `docunavi-viewer` へのなりすまし。書き込みは設計上 403）。この既定を外さない・上書きしない。未設定のとき（再ビルド前など）も自分では設定せず、ユーザーに伝える。
- **書き込みのコマンドには、誰の権限で動かすかを明示する**（準備は `env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`、日常のデプロイは `--impersonate-service-account=agent-deployer@…`。README の「書き込みをするとき」）。実行はユーザー（ホストのターミナル）。
- **ログインの起動だけは、引数なしの `gcloud auth login` に限って実行してよい。** フックが毎回「確認」を返す。ブラウザーでの選択と承認は、ユーザーが行う。
  `--no-launch-browser` は使わない（コードの受け渡しを避ける）。ログイン後に実行するのは、`docunavi-viewer` の権限で読める範囲（ビルド・Cloud Run・ログの閲覧）だけ。
  `gcloud builds …` は `--billing-project=docunavi-app` を付ける。課金と有効な API の一覧（`gcloud billing projects describe`・`gcloud services list`）は権限外なので、ユーザーに実行してもらう（README の「エージェント運用の補足」）。
- **それ以外の認証・認証情報には触れない。** `gcloud auth` の操作（`list`・上の `login`・`revoke --all` を除く）、`gcloud config set/unset`、
  `--impersonate-service-account` / `--account` / `--access-token-file`、`CLOUDSDK_*` の設定、`~/.config/gcloud` やトークンの読み出し・保存・送信はしない。
- **日常の ID を外す操作もしない。** `env -u`、`unset`、`export -n`、CLI の前に置いた `sudo` などは、フックが止める。外さないと実行できない書き込みは、自分では外さず、コマンドを提示してユーザーに実行してもらう。
- **ログアウト:** `gcloud auth revoke --all` は、毎回の作業の終わりには求めない（ユーザーが決定）。促すのは、長期に使わないとき、共有の端末、不審な挙動のときだけ。SA のキーは作らない・保存しない。
  コンテナの中にはユーザー自身のログイン（リフレッシュトークン）が残る（2026-10-21 から。限界と失敗時の扱いは README）。

## gh（読み取り専用のトークン + ガード）

開発コンテナの gh には、**このリポジトリだけの読み取り専用トークン**（`Actions` と `Metadata`）を置く（README の「開発環境の GitHub CLI（gh）」）。

- 使うのは `gh run list/view`、`gh pr view`、`gh ruleset view` など。`gh api` は許可リストに無く、毎回確認が出る（GET だけ使う）。コンテナの中でのユーザー自身の gh ログインは勧めない（フル権限のトークンがエージェントから読めてしまう）。
- ログイン・認証情報には触れない。`gh auth`（`status` を除く）、`GH_TOKEN` などの環境変数の設定、`~/.gh-config` の読み出し、`git credential` はしない。
- 401 が返ったら期限切れの可能性が高いので、ユーザーに更新を頼む。

## GitHub MCP（`github`。Actions の起動と監視だけ）

- 使うのは `actions_run_trigger`（`run_workflow`、`deploy.yml` を `main` で）と、`actions_list` `actions_get` `get_job_logs`（監視）。
- `rerun` `cancel` は、言葉で頼まれたときだけ（「y」だけでは足りない）。ヘルパー（`scripts/github-mcp-header.sh`）は実行せず、トークンを表示・保存しない。トークンの仕組み（GitHub App の installation トークン）は README の「エージェント運用の補足」。
- **`deploy.yml` の起動は、そのたびに、対象のコミットと `workers/` `frontend/` の変更の有無を示して、利用者の明示の承認を得てから行う。前の承認は次に及ばない。**
  Environment「production」の必須レビュアーは外してある（経緯は `docs/deploy.md`）。この承認は会話の約束に加え、`mcp__github__actions_run_trigger` を `.claude/settings.json` の `ask` に入れて、起動のたびに確認が出るようにしてある（2026-11-19。ただし GitHub 側に必須レビュアーは無い）。

## MCP（app-docunavi）

非公開の `docunavi-mcp` へは、`headersHelper`（`scripts/mcp-auth-header.sh`）で直接つなぐ。ローカルプロキシは使わない。診断と経緯は [docs/mcp-tool-design.md](../../../docs/mcp-tool-design.md)。

- ヘルパーは Claude Code が実行する。ガードのフックも確認も通らず、ユーザー自身の権限で動く。**私は実行しない**（標準出力が ID トークンになる）。
- スクリプトの変更は、事前にユーザーの承認を得る。「ID トークンを JSON で返す」以外をさせない。トークンは標準エラー・ログ・ファイル・コミットにも出さない。
- 画面の Authenticate は押さない（ヘッダー無しの OAuth 探索になり、403 で失敗する）。
- 旧プロキシ `scripts/mcp-proxy.sh` は、ユーザーが明示的に指示したときだけ実行する。実行前に `git diff 399f592 -- scripts/mcp-proxy.sh` が空であることを確かめ、自動再起動ループは足さない（背景は README の「エージェント運用の補足」）。

## ローカルの認証情報（Infisical・Google の Secret）

- **正本は Infisical `docunavi-local`**（ID は環境変数 `INFISICAL_PROJECT_ID_LOCAL`・環境 `dev`・パス `/`）。`workers/api/.dev.vars` は手で書かず、`scripts/dev-local.sh start|vars` で作る（権限 600）。
- **実 API の `GEMINI_API_KEY` も同じ保存先**で、API のプロセスの環境変数にだけ渡す（画面・Worker・ログ・引数には出さない）。新しいキーは発行せず、本番のキーは変えない。無料・有料は、変数名では決めず、`gemini-key-info` と `ack-gemini` で確かめて承認を得る。
- **本番（Worker `docunavi-api`。Infisical `docunavi-prod`）と同じ Google Secret を共用**するので、更新は両方の保存先と実行環境をそろえる。必ず先にここを参照し、再利用する。Secret の追加・ローテーション・無効化は自動でせず、理由と影響を示して承認を得る。
  取れないときは「無い」と即断せず、組織・ID・環境・パス・権限を確認する（手順は `docs/development.md`「Google ログインの設定」）。値は文書・メモリー・Git に書かない。
- 保管・ローテーション・期限の方針は [.claude/rules/secrets.md](../../rules/secrets.md)。

## GitHub 側の自動防御・SECURITY.md

- `SECURITY.md` は 2026-11-18 に置いた（リポジトリは公開）。窓口は GitHub の Private Vulnerability Reporting で、同日に有効化した（ユーザーの依頼で、ホストの gh から。有効かは `gh api repos/Starlight-Tree/DocuNavi/private-vulnerability-reporting` の GET で読める）。対応はベストエフォートと明記してある。内容を変えるときは確認する。
- GitHub 側の自動防御は、ユーザーが設定画面で確認する。Dependabot は設定済み。Secret Scanning と Push Protection が有効かは未確認（読み取り専用のトークンでは見えない）。
