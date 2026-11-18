# CLAUDE.md

資料（架空の惑星「ザーハード」の設定資料と、PDF の資料）に答える RAG チャットボット。FastAPI + LangGraph + Gemini のバックエンドと、
Next.js + React + TypeScript のフロントエンド（Cloudflare の Worker `docunavi-web` が配信。FastAPI は配信しない）。入口は [README.md](README.md)、
詳細は [docs/](docs/)（構成は `architecture.md`、API は `api.md`、環境変数と LLM の挙動は `configuration.md`、計画は `roadmap.md`、決定の記録は `decisions/`）。領域ごとの規約は `app/` `frontend/` `tests/` `docs/` `evals/` `ops/django-saas/`（学習用の試作。製品には入っていない）`ops/sse-relay/` の各 `CLAUDE.md`、パス別の規則は `.claude/rules/`。

## よく使うコマンド

| 目的 | コマンド |
|---|---|
| バックエンド起動 | `uvicorn app.main:app --reload`（`.env` が無い・キー未設定ならモックモード） |
| バックエンドのテスト | `pytest`（`tests/conftest.py` がモックモードに固定し、検索も偽物に差し替える。実 API・DB は呼ばない） |
| フロントエンド | `cd frontend && npm ci` の後 `npm run dev` / `npm test` / `npm run build` |
| E2E（ブラウザー） | `cd frontend && npm run test:e2e`（偽の LLM・偽の検索の `tests/e2e_server.py`。実 API は呼ばない。初回だけ `npx playwright install chromium`）。手順は [docs/testing.md](docs/testing.md) |
| 依存の更新 | `requirements*.in` を編集し、`uv pip compile`（[docs/development.md](docs/development.md) の「依存の更新」節）で `.txt` を再生成する。`.txt` は直接編集しない |
| Docker | 開発コンテナは Compose ベースで、開いても `api` `frontend` は起動しない（2026-11-11 から）。コンテナ内に Docker エンジンは無く、起動・停止はホストの `docker compose`（プロジェクト名 `docunavi`）。標準のローカル構成（ローカル Postgres + 認証。画面は 3000 番）は `scripts/dev-local.sh`。**LLM は実 API が既定、モックは `--llm mock` と明示したときだけ。** 本番の DB には接続しない。エージェントは、テストのために通常の 3000 番をモックへ切り替え・再起動しない。未確認の点・構成の詳細・確認の日付は [docs/development.md](docs/development.md) |

## 構成

コードの置き場所・処理の流れ・どこで動くかは [docs/architecture.md](docs/architecture.md)（本番は 2026-11-08 から、画面は Worker `docunavi-web`、API は Cloud Run の FastAPI、DB は Prisma Postgres、認証は Worker `docunavi-api` の Better Auth）。
目標構成と、Go・Django などを採用する条件は [docs/target-architecture.md](docs/target-architecture.md)。**新しいサービス・言語・フレームワーク・DB・インフラを足す前に、それを読んで、ユーザーに確認する。**
`app/` の各モジュールの不変条件（失敗しても別の経路・メモリへ戻らない、`sub` 以外から利用者 ID を取らない、`scope` 必須など）は [.claude/rules/app-api.md](.claude/rules/app-api.md)（`app/**` を触るときに読み込まれる）と `app/CLAUDE.md`。

## 守ること（コードを書く前に）

- **キーは出さない・載せない。** `.env` に実キーがある。値を表示・ログ・コミットしない。`.env` は `.claude/settings.json` の
  `Read(./.env)` の拒否ルールの対象で、Bash でも開かない。拒否されたら回避せず（別の書き方・スクリプト経由で同じことをしない）、
  ユーザーに伝える。実キーの値の照合はせず、`AIza…` 形式のパターンで検査する。
- **エラー本文は固定メッセージ。** 例外の文字列やサーバーのパスを API の応答に含めない。
- **API に画面を配信させない。** 画面は Workers の `docunavi-web`（`frontend`）。`/{path}` のような全捕捉ルートも足さない（API の 405 やリダイレクトが壊れる）。
- 次の 5 点の詳細は [.claude/rules/app-api.md](.claude/rules/app-api.md)（2026-11-18 に移した）。
  - **LLM の呼び出し（ヘッジ・フォールバック・有料キー）は、追加の試行がトークンを増やし、モデルの変更も料金と品質に響くので、変える前に確認する。** フォールバックと有料キーへの切り替えは任意で、既定は無効。
  - **`temperature` は指定しない。**
  - **切断は失敗ではない。** `/chat` の切断は 499・INFO ログで返し、例外（502・ERROR ログ）にしない。`request.is_disconnected()` のポーリングは使わない。
  - **ストリーミングでも出力ガードを外さない。** `StreamingOutputGuard` の窓・保持の文字数を変えるときは `tests/test_streaming_guard.py` で確かめる。
  - キー（有料キーを含む）の値はログに出さない。
- 変更したら、触れた範囲のテストを実行してから報告する。確認できていないこと（例: Docker ビルド）は確認済みと書かない。

## セキュリティの運用

- **`SECURITY.md` を置いた**（2026-11-18。リポジトリは公開）。窓口は GitHub の Private Vulnerability Reporting で、**設定の有効化はユーザーが行う**（有効化の前は、報告画面が開かない。有効かは `gh api repos/Starlight-Tree/DocuNavi/private-vulnerability-reporting` の GET で読める）。対応はベストエフォートと明記してある。内容を変えるときは確認する。
- **画面に渡してよいのは公開用の値だけ。** `NEXT_PUBLIC_*` はバンドルに埋め込まれ、誰でも読める。入れてよいのは API・中継・認証（Better Auth）の URL など、公開してよい値だけ。
  秘密鍵・`D1_API_TOKEN`・`GEMINI_API_KEY` は入れない。
- **設定は環境変数で渡す。** 認証・DB・LLM の値をコード・文書・コミットに書かない。`.env` と `frontend/.env.local` は Git 管理外。
- **ローカルの認証情報（Google ログインなど）の正本は Infisical `docunavi-local`（ID は環境変数 `INFISICAL_PROJECT_ID_LOCAL`・環境 `dev`・パス `/`）。** `workers/api/.dev.vars` は手で書かず、`scripts/dev-local.sh start|vars` で作る（権限 600）。**実 API の `GEMINI_API_KEY` も同じ保存先**で、API のプロセスの環境変数にだけ渡す（画面・Worker・ログ・引数には出さない）。新しいキーは発行せず、本番のキーは変えない。無料・有料は、変数名では決めず、`gemini-key-info` と `ack-gemini` で確かめて承認を得る。
  **本番（Worker `docunavi-api`。Infisical `docunavi-prod`）と同じ Google Secret を共用**するので、更新は両方の保存先と実行環境をそろえる。必ず先にここを参照し、再利用する。Secret の追加・ローテーション・無効化は自動でせず、理由と影響を示して承認を得る。取れないときは「無い」と即断せず、組織・ID・環境・パス・権限を確認する（手順は `docs/development.md`「Google ログインの設定」）。値は文書・メモリー・Git に書かない。
- **他の人が届く環境（Cloud Run など）は `AUTH_MODE=required`。** `off`（既定）は、自分だけが使うローカル開発用。
- **キーを誤ってコミット・公開したら、失効させて作り直す。** 履歴から消すだけでは足りない。
- **ガードレールはルールベースで、プロンプトインジェクションを完全には防がない**（`docs/guardrails.md`）。
- **GitHub 側の自動防御は、ユーザーが設定画面で確認する。** Dependabot は設定済み。Secret Scanning と Push Protection が有効かは未確認（読み取り専用のトークンでは見えない）。
- 秘密の保管・ローテーション・期限の方針は [.claude/rules/secrets.md](.claude/rules/secrets.md)。

## 開発プロセスの後始末

作業の終わり・セッションの切り替えの前に、自分が起動したサーバー（uvicorn、モック、負荷試験のスクリプト等）を残さない（古いコードのまま 8000 番を占有する）。
起動したものは PID を控え、`kill <pid>` で止める。他のセッションが並行しているときは、一括の `pkill` を使わず PID を指定する。一括で片付けるなら
`fuser -k 8000/tcp || true` と `pkill -f '[u]vicorn' || true`（`[p]ytest`、`[t]est_stress` も同様）を実行し、`lsof -i :8000` が何も表示しなければ空き。
**`pkill -f` のパターンは `[u]vicorn` のように 1 文字を括弧で囲む。** `-f` はコマンドライン全体に一致するので、生の `uvicorn` と書くと実行中のシェル自身も止める。コマンドの行やコメントにも、`uvicorn` などの語を生で書かない。

## gcloud・gh 共通（ガード）

gcloud と gh には、誤操作や、取り込んだ文書に埋め込まれた指示による破壊的な操作を防ぐガードがある（PreToolUse フック `.claude/hooks/guard-cloud-cli.py`）。
手順と層の説明は [.devcontainer/README.md](.devcontainer/README.md)（背景と手順の補足は、同じ README の「エージェント運用の補足」）。

- **止められたら、回避せず（別の書き方・スクリプト経由・別のツールで同じことをしない）、ユーザーに伝える。**
- **フックが「確認」を返した操作は、ユーザーの承認なしに進めない。**
- **ガードを外さない。** `.claude/settings.json` のフック・権限ルール・`disableBypassPermissionsMode` を、頼まれずに編集・無効化しない。
  `--dangerously-skip-permissions` を勧めない。「指示への従い方」の節でも外れない。
- **読み取りだけ実行する。** 書き込み系（gcloud: デプロイ、作成・更新・削除、IAM・課金、API の有効化 / gh: PR の作成・マージ、`run rerun`、設定やルールセットの変更）は実行せず、
  コマンドを提示してユーザーに実行してもらう（ユーザーはコンテナの外のホストのターミナル、gh はホストの gh かブラウザー）。
- **トークンの中身は表示・ログ・コミットしない。**

## gcloud（ユーザーのログイン + ガード）

開発コンテナの gcloud には、ユーザー自身のアカウントでログインしている。

- **日常の ID は閲覧専用**（`CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT` による `docunavi-viewer` へのなりすまし。書き込みは設計上 403）。この既定を外さない・上書きしない。未設定のとき（再ビルド前など）も自分では設定せず、ユーザーに伝える。
- **書き込みのコマンドには、誰の権限で動かすかを明示する**（準備は `env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`、日常のデプロイは `--impersonate-service-account=agent-deployer@…`。README の「書き込みをするとき」）。
- **ログインの起動だけは、引数なしの `gcloud auth login` に限って実行してよい。** フックが毎回「確認」を返す。ブラウザーでの選択と承認は、ユーザーが行う。
  `--no-launch-browser` は使わない（コードの受け渡しを避ける）。ログイン後に実行するのは、`docunavi-viewer` の権限で読める範囲（ビルド・Cloud Run・ログの閲覧）だけ。
  `gcloud builds …` は `--billing-project=docunavi-app` を付ける。課金と有効な API の一覧（`gcloud billing projects describe`・`gcloud services list`）は権限外なので、ユーザーに実行してもらう（README の「エージェント運用の補足」）。
- **それ以外の認証・認証情報には触れない。** `gcloud auth` の操作（`list`・上の `login`・`revoke --all` を除く）、`gcloud config set/unset`、
  `--impersonate-service-account` / `--account` / `--access-token-file`、`CLOUDSDK_*` の設定、`~/.config/gcloud` やトークンの読み出し・保存・送信はしない。
- **日常の ID を外す操作もしない。** `env -u`、`unset`、`export -n`、CLI の前に置いた `sudo` などは、フックが止める。
  外さないと実行できない書き込みは、自分では外さず、コマンドを提示してユーザーに実行してもらう。
- **ログアウト:** `gcloud auth revoke --all` は、毎回の作業の終わりには求めない（ユーザーが決定）。促すのは、長期に使わないとき、共有の端末、不審な挙動のときだけ。SA のキーは作らない・保存しない。
  コンテナの中にはユーザー自身のログイン（リフレッシュトークン）が残る（2026-10-21 から。限界と失敗時の扱いは README）。

## MCP の接続（app-docunavi）

非公開の `docunavi-mcp` へは、`headersHelper`（`scripts/mcp-auth-header.sh`）で直接つなぐ。ローカルプロキシは使わない。診断と経緯は [docs/mcp-tool-design.md](docs/mcp-tool-design.md)。

- ヘルパーは Claude Code が実行する。ガードのフックも確認も通らず、ユーザー自身の権限で動く。**私は実行しない**（標準出力が ID トークンになる）。
- スクリプトの変更は、事前にユーザーの承認を得る。「ID トークンを JSON で返す」以外をさせない。トークンは標準エラー・ログ・ファイル・コミットにも出さない。
- 画面の Authenticate は押さない（ヘッダー無しの OAuth 探索になり、403 で失敗する）。
- 旧プロキシ `scripts/mcp-proxy.sh` は、ユーザーが明示的に指示したときだけ実行する。実行前に `git diff 399f592 -- scripts/mcp-proxy.sh` が空であることを確かめ、自動再起動ループは足さない（背景は README の「エージェント運用の補足」）。

## gh（読み取り専用のトークン + ガード）と GitHub MCP（Actions だけ）

開発コンテナの gh には、**このリポジトリだけの読み取り専用トークン**（`Actions` と `Metadata`）を置く（README の「開発環境の GitHub CLI（gh）」）。

- **使うのは** `gh run list/view`、`gh pr view`、`gh ruleset view` など。`gh api` は許可リストに無く、毎回確認が出る（GET だけ使う）。
  コンテナの中でのユーザー自身の gh ログインは勧めない（フル権限のトークンがエージェントから読めてしまう）。
- **ログイン・認証情報には触れない。** `gh auth`（`status` を除く）、`GH_TOKEN` などの環境変数の設定、`~/.gh-config` の読み出し、`git credential` はしない。
- 401 が返ったら期限切れの可能性が高いので、ユーザーに更新を頼む。
- **GitHub MCP（`github`。Actions の起動と監視だけ）:** 使うのは `actions_run_trigger`（`run_workflow`、`deploy.yml` を `main` で）と、`actions_list` `actions_get` `get_job_logs`（監視）。
  `rerun` `cancel` は、言葉で頼まれたときだけ（「y」だけでは足りない）。ヘルパー（`scripts/github-mcp-header.sh`）は実行せず、トークンを表示・保存しない。トークンの仕組み（GitHub App の installation トークン）は README の「エージェント運用の補足」。
  **`deploy.yml` の起動は、そのたびに、対象のコミットと `workers/` `frontend/` の変更の有無を示して、利用者の明示の承認を得てから行う。前の承認は次に及ばない。** Environment「production」の必須レビュアーは外してある（経緯は `docs/deploy.md`）。この承認は会話の約束だけで、技術的な関門ではない。

## Git

- **作者名:** `git config user.name` は `Starlight-Tree`（GitHub の登録名。メールは変えない）。2026-11-14 に、全履歴の作者・コミッターを `Starlight-Tree` に書き換えた（[docs/history-rewrite.md](docs/history-rewrite.md)）。
- **コミットメッセージ:** Conventional Commits（`type(scope): 英語の命令形の要約`）。type は `feat` `fix` `docs` `refactor` `test` `chore` `build` `ci` `perf`。
  件名は英語（日本語は本文に）・72 文字以内・小文字始まり・末尾ピリオドなし。本文に「なぜ」と検証結果（限界も）を書く。1 コミット 1 論理変更。`scripts/check_commit_messages.py` が検査し、CI（`commit-messages`）と Claude のフック（`guard-git-write.py`）が使う（過去の履歴は対象外）。
- **運用:** `main` に直接コミットし、`origin/main` へ通常のプッシュ。
- **コミット:** 変更が完結し、触れた範囲のテストが通ったら（文書だけの変更はテスト不要）、意味のある単位でコミットしてよい。
  タスクで「コミットしない」と指定されたら、従う。ステージはパスを指定する（作業ツリーを並行セッションと共有しているので、`git add -A` と `git add .` は使わない）。
  コミット前に `git diff --staged` で意図しない変更が無いかを確かめる。`--no-verify` は使わない。
- **amend:** 直前のコミットが未プッシュのときだけ、承認なしで `git commit --amend` してよい。使う前に `git fetch` を行い、`git status -sb` が `ahead` を示し、
  `git log --oneline origin/main..HEAD` に直前のコミットがあることを確かめる。ステージの確認は amend でも行い、amend の後は検査をやり直す。
  プッシュ済みのコミットは amend しない。直す必要があるときは、新しいコミットで直すか、ユーザーに相談する。
- **プッシュ:** **ユーザーの承認を得てから**行う。コミットのたびには聞かず、まとまりが完結した時点で 1 回、次を示して承認を求める:
  `git status --short`、`git log --oneline origin/main..HEAD`、`git diff --stat origin/main..HEAD`、検査の結果、テストを実行したか。
  テストが失敗しているとき、作業の途中のときは提案しない。検査は、追加行の秘密情報のパターンと、登録語
  （`scripts/check-sensitive-terms.sh origin/main..HEAD`。語は Git の管理外の `.git/sensitive-terms` に置く）。
- **しないこと:** 強制プッシュと、公開済み履歴の書き換えはしない（必要なら相談する）。`git reset` と `git restore` は、承認を得てから行う。
- **CI:** `.github/workflows/ci.yml` が、`main` へのプッシュと PR でテスト・ビルド・静的検査を行う（内訳は [docs/testing.md](docs/testing.md)）。**CI 自体にデプロイは含まない。**
  `.github/workflows/deploy.yml` は**手動の実行だけ**（`workflow_dispatch`。自動では起動しない）。`main` の CI の成功を確かめたうえで、利用者の明示の承認を得て実行し（GitHub MCP で私が起動してもよい。承認は会話で取る）、Cloudflare の Worker（`workers/api` = `docunavi-api`、`frontend` = `docunavi-web`）を本番へデプロイする。
  Cloudflare の資格情報は GitHub に置かず、OIDC で Infisical（`docunavi-deploy`）から取る（詳細は [docs/deploy.md](docs/deploy.md)、方針は [.claude/rules/secrets.md](.claude/rules/secrets.md)）。
  プッシュの承認を求めるときは、`workers/` `frontend/` の変更が含まれるか（＝ユーザーが `deploy.yml` を実行したときに本番へ出る）を添える。プッシュだけでは Worker は本番に出ない。Worker の秘密（`wrangler secret put`）はこの経路に含めない。Cloud Run（FastAPI・中継）は自動デプロイしない。
- **CI の結果の扱い:** プッシュのたびに待たない。設定・文書・拡張だけの変更は、見にも行かない。アプリ本体・テスト・Docker・`ci.yml`・`frontend/` を変えたら、
  プッシュ後に一度だけ `gh run list` で確認し、未確認でも次の独立した作業へ進む。次の作業が前のコミットの CI に依存するとき、デプロイや公開の前、
  ユーザーが結果を求めたときは、完了まで見て報告する。失敗したら、失敗ステップとログの要点を出す。

## UI/UX（画面の変更）

基準（デスクトップ 1280px 前後、スマホ実機は想定しない）、Before / After のスクリーンショット、モックの作成、比較リサーチの手順は、`.claude/rules/ui-review.md`（`frontend/**` を触るときに読み込まれる）と `.claude/skills/ux-review/SKILL.md`。
**レイアウトを変える変更は、実装の前にモックを作って承認を得る。省くときも、省くことと理由を先に伝えて承認を得る。**

## 作業の進め方

- 手順を案内するときは、まず CLI・API・MCP でできるかを確かめ、できるならそのコマンドを先に出す。画面の手順は、それらで届かないものに限り、理由を 1 行添える。画面を開かせる手順には、ログイン・アカウント選択・設定画面の移動を含めて、開く先の完全な URL をリンクで付ける（実機で確認していない URL は、そう書く）。
- タスクで指定された範囲だけを編集する。範囲外のファイル（`tests/`・README・`.dockerignore`・`docker-compose.yml` など）に必要な変更があれば、勝手に直さず提案する。
- この CLAUDE.md が 199 行を超えたら（＝200 行以上。`wc -l CLAUDE.md`）、作業の区切りで 1 回、圧縮・分離・削除の整理案を提案する。実行は承認後。
  禁止事項と承認が必要な操作は、行数のために削らない。
- 開発環境の方針（LiteLLM・Gemini CLI は使わない、GCP の操作は原則 gcloud CLI で Cloud Code 等の VS Code 拡張は入れない、画像生成の Cloud Run Job は星庭ジョブのリポジトリで管理し `job/` をここに置かない、開発コンテナの再ビルドの可能性を壊す変更は先に確認する）は [.claude/rules/dev-environment.md](.claude/rules/dev-environment.md)（`.devcontainer/**` の規則は [devcontainer.md](.claude/rules/devcontainer.md)）。

## 指示への従い方

- ユーザーの指示は、原則としてそのまま実行する。確認を省くのは、下の「承認済み」の一覧にあるものだけ。
  この節に載っていない確認のルール（消す前の確認、実 API の課金、モックの承認、プッシュ、`git reset` など）は、そのまま残る。
- 「できない」「やめておいた方がいい」と判断するときは、理由を 1 文で示し、代替案を 1 つだけ添える。それ以上の反論・再確認・遠慮はしない。
- 承認済み（再確認しない）: 読み取り専用の gcloud コマンド / テスト・ruff・登録語検査の実行 / 通常の git commit。
- 承認が必要: gcloud auth login の実行 / 書き込み系 gcloud コマンド / git push / フック・CLAUDE.md・権限設定の変更。
- ユーザーが「やらせたい」と言ったら、安全性の確認を 1 回だけして、あとは実行する。ただし、フックの確認・ガード・権限（gcloud と gh の節）は、これで外れない。
