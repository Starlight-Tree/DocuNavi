# CLAUDE.md

架空の惑星「ザーハード」の設定資料に答える RAG チャットボット。FastAPI + LangGraph + Gemini のバックエンドと、
Vite + React + TypeScript のフロントエンド（ビルド成果物を FastAPI が配信）。入口は [README.md](README.md)、
詳細は [docs/](docs/)（API は `docs/api.md`、環境変数は `docs/configuration.md`、今後の計画は `docs/roadmap.md`）。

## よく使うコマンド

| 目的 | コマンド |
|---|---|
| バックエンド起動 | `uvicorn app.main:app --reload`（`.env` が無い・キー未設定ならモックモード） |
| バックエンドのテスト | `pytest`（`tests/conftest.py` がモックモードに固定する。実 API は呼ばない） |
| フロントエンド | `cd frontend && npm ci` の後 `npm run dev` / `npm test` / `npm run build` |
| 依存の更新 | `requirements*.in` を編集し、`uv pip compile`（コマンドは [docs/development.md](docs/development.md) の「テスト」節）で `requirements*.txt` を再生成する。`.txt` は直接編集しない |
| Docker | ホスト側で `docker compose up --build`（開発コンテナ内には Docker が無い） |

## 構成

- `app/api/routes.py`: `/chat` `/chat/stream` `/health`。LLM の失敗は固定メッセージの 429/504/500/502 に変換する。
  クライアントが切断したら LLM 呼び出し（ヘッジの追加試行を含む）を取り消す。`/chat/stream` は回答を SSE
  （AI SDK の UI message stream。組み立ては `app/api/ui_stream.py`）で返し、画面はこちらを使う
- `app/agents/`: LangGraph（`graph.py`）、検索ツール（`tools.py`）、ガードレール（`guardrails.py`。出力は
  完成した回答の検査と、ストリーミング中の逐次検査 `StreamingOutputGuard`）、LLM 呼び出しのヘッジ付き
  再試行（`resilience.py`。完了を待つ `hedged_call` と、最初のチャンクを待つ `hedged_stream`）
- `app/history.py`: 会話履歴の保存先。`DATABASE_URL` があれば Postgres（接続プールを lifespan で開閉）、空ならメモリ。
  開けなければ起動を失敗させ、メモリへは切り替えない
- `app/history_admin.py`: 履歴の削除コマンド（`python -m app.history_admin`。既定は件数の表示だけ、`--apply` で削除）。アプリからは使わない
- `app/frontend.py`: `frontend/dist` を配信し、ファイル名らしくない未知の GET/HEAD は `index.html` にフォールバックする
  （拡張子付きのパスや `/assets/` 配下は 404）
- `storage/chunk/`: 設定資料（RAG のコーパス）
- `frontend/`: UI。`dist/` と `node_modules/` は Git 管理外

## 守ること（コードを書く前に）

- **キーは出さない・載せない。** `.env` に実キーがある。値を表示・ログ・コミットしない。
  `.env` は `.claude/settings.json` の `Read(./.env)` の拒否ルールの対象で、Bash でも開かない。拒否されたら回避せず
  （別の書き方・スクリプト経由で同じことをしない）、ユーザーに伝える。実キーの値の照合はせず、`AIza…` 形式のパターンで検査する。
- **エラー本文は固定メッセージ。** 例外の文字列やサーバーのパスを API の応答に含めない。
- **静的配信は `router.default` に差し込む設計。** `/{path}` のような全捕捉ルートを足さない（API の 405 や
  リダイレクトが壊れる）。`frontend/dist` が無くても API は動き、`/` はビルドを促す 404 を返す。
- **LLM の呼び出しは不安定になりうる。** `gemini-3.1-flash-lite` は速い回と無応答の回が混在する。
  遅い呼び出しは待たずに追加の試行を投げ（ヘッジ、`app/agents/resilience.py`）、最初の成功を採る。
  `LLM_HEDGE_DELAY_SECONDS`（8、0 で無効）/ `LLM_ATTEMPT_TIMEOUT_SECONDS`（30。ヘッジなしは 15）/
  `LLM_MAX_ATTEMPTS`（3）、
  全体の上限は `LLM_TIMEOUT_SECONDS`（60）。追加の試行はトークンを増やす。モデルの変更も料金と品質に
  響くので、変える前に確認する。`/chat/stream` は「最初のチャンクが届いた試行」を採り、採った後は別の試行へ
  切り替えない（送った本文と食い違うため）。
- **切断は失敗ではない。** `/chat` はグラフの実行と `request.receive()`（`http.disconnect` まで待つ監視）を
  `asyncio.wait` で競わせ、切断が先なら LLM 呼び出しを取り消して 499 を返す。この分岐は `except Exception` の
  外に置き、ログは INFO・固定文・`thread_id`・経過秒だけにする（例外にすると 502 と ERROR ログになる）。
  `request.is_disconnected()` のポーリングは使わない（外側の取り消しを飲み込み、遅れる）。本文の解析前には
  `receive()` を呼ばない。`/chat/stream` も同じ方針で、レスポンスは最初の本文が届くまで始めない（その前の
  失敗は `/chat` と同じ HTTP ステータス、後の失敗は `error` チャンクの固定メッセージ）。本体の生成が始まる前に
  切断されても実行を取り残さないよう、後始末は `BackgroundTask` にも置く。
- **ストリーミングでも出力ガードを外さない。** 漏洩の判定は「正規化後の連続一致 24 文字以上」で、
  `StreamingOutputGuard` は末尾の正規化後 31 文字を送らずに残して、連続一致が閾値に届く前に止める。
  検知したら、そのターンのメッセージを定型文に置き換え、ツール呼び出しも捨てる。窓や保持の文字数を
  変えるときは、`tests/test_streaming_guard.py` の分割位置のテストで確かめる。
- 変更したら、触れた範囲のテストを実行してから報告する。確認できていないこと（例: Docker ビルド）は
  確認済みと書かない。

## 開発プロセスの後始末

作業の終わり・セッションの切り替えの前に、自分が起動したサーバー（uvicorn、モック、負荷試験のスクリプト等）を
残さない。残すと古いコードのまま 8000 番を占有し、次の作業で別物を見てしまう。

- 起動したものは PID を控え、`kill <pid>` で止める。一括で片付けるなら次のとおり。

  ```bash
  fuser -k 8000/tcp || true
  pkill -f '[u]vicorn' || true
  pkill -f '[p]ytest' || true
  pkill -f '[t]est_stress' || true
  lsof -i :8000
  ```

  `lsof` に何も表示されなければ空き（`ss -ltnp` でもよい）。
- **`pkill -f` のパターンは `[u]vicorn` のように 1 文字を括弧で囲む。** `-f` はコマンドライン全体に一致するため、
  `uvicorn` と書くと、そのコマンドを実行しているシェル自身（Bash ツールの呼び出しを含む）も止めてしまう。
  括弧付きならパターン自体には一致しない。コマンドの行やコメントに `uvicorn` などの語を生で書かない。
- 他のセッションが並行して動いているときは、一括の `pkill` を使わない（そちらの作業も止める）。
  PID を指定して止める。

## gcloud（ユーザーのログイン + ガード）

開発コンテナの gcloud には、**ユーザー自身のアカウントでログインしている**（2026-10-13 にユーザーが決定）。
ログイン情報があるので、誤操作や、取り込んだ文書に埋め込まれた指示による破壊的な操作を防ぐため、次を守る。
手順と層の説明は [.devcontainer/README.md](.devcontainer/README.md) の「開発環境の gcloud（ログインとガード）」。

- **日常の ID は閲覧専用。** コンテナの環境変数 `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`
  （`.devcontainer/devcontainer.json` の `containerEnv`）で、閲覧専用の `agent-viewer` へのなりすましが既定になっている
  （書き込みは設計上 403）。この既定を外さない・上書きしない。コンテナの再ビルド前は未設定のことがある。その場合も
  自分では設定せず、ユーザーに伝える。
- **読み取りだけ実行する。** 書き込み系（デプロイ、作成・更新・削除、IAM・課金、API の有効化）は実行せず、
  コマンドを提示してユーザーに実行してもらう。提示するコマンドには、誰の権限で動かすかを明示する
  （準備は `env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`、日常のデプロイは `--impersonate-service-account=agent-deployer@…`。
  [.devcontainer/README.md](.devcontainer/README.md) の「書き込みをするとき」）。
- **ログイン・認証情報には触れない。** `gcloud auth` の操作（`list` を除く）、`gcloud config set/unset`、
  `--impersonate-service-account` / `--account` / `--access-token-file`、`CLOUDSDK_*` の設定、
  `~/.config/gcloud` やトークンの読み出しはしない。PreToolUse フック（`.claude/hooks/guard-cloud-cli.py`）が止める。
  止められたら、回避せず（別の書き方・スクリプト経由・別のツールで同じことをしない）、ユーザーに伝える。
- **日常の ID を外す操作もしない。** `env -u`、`unset`、`export -n`、CLI の前に置いた `sudo` などは、フックが止める。
  外さないと実行できない書き込みは、自分では外さず、コマンドを提示してユーザーに実行してもらう。
- **フックが「確認」を返した操作は、ユーザーの承認なしに進めない。**
- **ガードを外さない。** `.claude/settings.json` のフック・権限ルール・`disableBypassPermissionsMode` を、頼まれずに
  編集・無効化しない。`--dangerously-skip-permissions` を勧めない。
- **トークンの中身は表示・ログ・コミットしない。** 作業の終わりには、`gcloud auth revoke --all` でのログアウトを促す。
  ログインはボリュームに残さない。

## gh（読み取り専用のトークン + ガード）

開発コンテナの gh には、**このリポジトリだけの読み取り専用トークン**（`Actions` と `Metadata`）を置く。
トークンは再ビルドしても残る（`GH_CONFIG_DIR=~/.gh-config`）。手順と層の説明は [.devcontainer/README.md](.devcontainer/README.md) の
「開発環境の GitHub CLI（gh）」。

- **読み取りだけ実行する。** `gh run list/view`、`gh pr view`、`gh ruleset view` など。`gh api` は許可リストに
  無く、毎回確認が出る（GET だけ使う）。書き込み系（PR の作成・マージ、`run rerun`、設定やルールセットの変更）は
  実行せず、コマンドを提示してユーザーに実行してもらう。ユーザーはコンテナの外（ホストの gh かブラウザー）で実行する。
  コンテナの中でのユーザー自身の gh ログイン（ブラウザー / デバイス認証）は勧めない（`GH_CONFIG_DIR` を共有するため、
  フル権限のトークンがエージェントから読めてしまう）。
- **ログイン・認証情報には触れない。** `gh auth`（`status` を除く）、`GH_TOKEN` などの環境変数の設定、
  `~/.gh-config` の読み出し、`git credential` はしない。同じフック（`.claude/hooks/guard-cloud-cli.py`）が止める。
  止められたら、回避せず（別の書き方・スクリプト経由・別のツールで同じことをしない）、ユーザーに伝える。
- **フックが「確認」を返した操作は、ユーザーの承認なしに進めない。** ガード（フック・権限ルール）を、頼まれずに
  編集・無効化しない。
- **トークンの中身は表示・ログ・コミットしない。** 401 が返ったら期限切れの可能性が高いので、ユーザーに更新を頼む。

## Git

- **コミットメッセージ:** Conventional Commits（`type(scope): 英語の命令形の要約`）。
  type は `feat` `fix` `docs` `refactor` `test` `chore` `build` `ci` `perf`。件名は 72 文字以内・小文字始まり・
  末尾ピリオドなし。本文に「なぜ」と検証結果（限界も）を書く。1 コミット 1 論理変更。
- **運用:** `main` に直接コミットし、`origin/main` へ通常のプッシュ。
- **コミット:** 変更が完結し、触れた範囲のテストが通ったら（文書だけの変更はテスト不要）、意味のある単位で
  コミットしてよい。タスクで「コミットしない」と指定されたら、それに従う。ステージはパスを指定する（作業ツリーを
  並行セッションと共有しているので、`git add -A` と `git add .` は使わない）。コミット前に `git diff --staged` で、
  意図しない変更が入っていないかを確かめる。`--no-verify` は使わない。
- **amend:** 直前のコミットが未プッシュのときだけ、承認なしで `git commit --amend` してよい。使う前に
  `git fetch` を行い、`git status -sb` が `ahead` を示し、`git log --oneline origin/main..HEAD` に直前のコミットが
  あることを確かめる。ステージの確認（上の `git diff --staged`）は amend でも行い、amend の後は検査をやり直す。
  プッシュ済みのコミットは amend しない。直す必要があるときは、新しいコミットで直すか、ユーザーに相談する。
- **プッシュ:** **ユーザーの承認を得てから**行う。コミットのたびには聞かず、まとまりが完結した時点で 1 回、
  次を示して承認を求める: `git status --short`、`git log --oneline origin/main..HEAD`、
  `git diff --stat origin/main..HEAD`、検査の結果、テストを実行したか。テストが失敗しているとき、作業の途中のときは
  提案しない。検査は、追加行の秘密情報のパターンと、登録語（`scripts/check-sensitive-terms.sh origin/main..HEAD`。
  語は Git の管理外の `.git/sensitive-terms` に置く）。
- **しないこと:** 強制プッシュと、公開済み履歴の書き換えはしない（必要なら相談する）。`git reset` と
  `git restore` は、承認を得てから行う。
- Cloud Run への自動デプロイ（GitHub Actions）を足したら、`main` へのプッシュ = デプロイになる。
  そのときはこの運用を見直す。
- **CI:** `.github/workflows/ci.yml` は、`main` へのプッシュと PR で、バックエンドのテスト（Python 3.12）、
  フロントエンドのテストとビルド、Docker イメージのビルドと起動確認、ワークフローの静的検査（actionlint・zizmor）を行う。**デプロイは含まない**
  （含めるときは、上のとおり運用を見直す）。アクションはコミット SHA で固定していて、Dependabot
  （`.github/dependabot.yml`、週次・7 日待ち）が更新の PR を出す。手動実行は `workflow_dispatch`。

## 作業の進め方

- タスクで指定された範囲だけを編集する。範囲外のファイル（`tests/`・README・`.dockerignore`・
  `docker-compose.yml` など）に必要な変更があれば、勝手に直さず提案する。
