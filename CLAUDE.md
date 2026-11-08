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
| E2E（ブラウザー） | `cd frontend && npm run test:e2e`（偽の LLM・偽の検索の `tests/e2e_server.py`。実 API は呼ばない。初回だけ `npx playwright install chromium`）。手順は [docs/development.md](docs/development.md) |
| 依存の更新 | `requirements*.in` を編集し、`uv pip compile`（[docs/development.md](docs/development.md) の「テスト」節）で `.txt` を再生成する。`.txt` は直接編集しない |
| Docker | 開発コンテナは Compose ベースで、開くと `api` `frontend` も起動する（`.devcontainer/docker-compose.yml`）。開発コンテナ内には Docker エンジンが無く、起動・停止はホストの `docker compose`（プロジェクト名 `docunavi`）。`api` はポート非公開（`dev` から直接は届かない）。ローカルの `api` は本番の DB につながず、モックモードで動く（実キーでも DB の設定〔`DB_BACKEND=pg` と `DATABASE_URL`〕が無いと unhealthy のまま止まる）。**未確認:** `api` から Gemini への到達、`stopCompose`、ホストのブラウザー（`localhost:3000`）での HMR。構成の詳細・確認の日付・手順は [docs/development.md](docs/development.md) |

## 構成（全体は [docs/architecture.md](docs/architecture.md)）

現在の本番（2026-11-08 から。画面は Cloudflare の Worker `docunavi-web`、API は Cloud Run の FastAPI、DB は Prisma Postgres〔Free・東京。pgvector で検索〕、認証は Worker `docunavi-api` の Better Auth を Hyperdrive 経由で同じ Postgres に載せる。D1・Vectorize・Worker の `/v1` は撤去済み。バックアップは R2 への毎日の `pg_dump`〔`docs/pg-backup.md`〕）と、目標構成と、Go・Django などを採用する条件は [docs/target-architecture.md](docs/target-architecture.md)。
新しいサービス・言語・フレームワーク・DB・インフラを足す前に、それを読んで、ユーザーに確認する。

- `app/api/routes.py`: `/chat` `/chat/stream` `/health`。LLM の失敗は固定メッセージの 429/504/500/502 に変換する。画面は SSE の `/chat/stream`（組み立ては `ui_stream.py`）を使う
- `app/agents/`: LangGraph（`graph.py`）、検索ツール（`tools.py`）、ガードレール（`guardrails.py`。完成した回答の検査と、ストリーミング中の `StreamingOutputGuard`）、
  ヘッジ付き再試行・フォールバック・有料キー切り替え（`resilience.py`）
- `app/search/vector.py`: 資料の検索（Postgres + pgvector）。`search_documents` の実体で、アプリの検索はこれだけ。
  本物の LLM では `DB_BACKEND=pg` + `DATABASE_URL` と `GEMINI_API_KEY` が必須（本番は pg）。起動時に DB の資料と埋め込みの状態を検査し、合わなければ起動を失敗させる。
  失敗しても別の経路には戻らず、API は 503 の固定文を返す。モックモード（キー未設定）は資料を検索しない固定の返答。設計は `docs/search-design.md`
- `app/history.py`: 会話履歴。`DB_BACKEND=pg` なら Postgres、`d1` で `D1_API_URL` があれば D1（データ用 Worker 経由。本番の D1 は撤去済みで、テストの偽 Worker 用に残したコード）、空ならメモリ。開けなければ起動を失敗させ、メモリへは切り替えない。削除コマンドは `app/history_admin.py`（既定は件数の表示だけ、`--apply` で削除。アプリからは使わない）
- `app/auth.py`: `AUTH_MODE=off`（既定）は匿名、`required` は `/chat` と `/chat/stream` に Better Auth（Worker `docunavi-api`。Google ログイン）が発行した JWT（JWKS の ES256/RS256 だけ。`exp` `iss` `aud` `sub` 必須）を要求し、
  履歴のキーを `<sub>:<thread_id>` にする（`sub` 以外から利用者 ID を取らない）。公開鍵が無ければ共有秘密には切り替えず、起動を失敗させる。
  本番は 2026-11-08 から Better Auth。**未確認:** トークン期限切れ後の更新の記録に残る確認（`docs/configuration.md` の「ログイン（認証）」）
- `app/access.py` / `app/scope.py`: ロール別の資料グループ制御（`ACCESS_MODE=off`（既定）/`shadow`/`enforce`）。検索は `scope` が必須（`Scope.ALL` は明示。MCP は運用者専用で全グループ）。本番は 2026-10-26 から `enforce`（`docs/configuration.md` の「資料グループのアクセス制御」）
- `app/api/threads.py`（会話の一覧・発言・削除。`required` では本人の分だけ、`off` は一覧が 404）、`app/api/documents.py`（資料一覧。読み取り専用）、
  `app/api/graph.py`（入寮規程の関係図。`app/relations/` が規則ベースで PDF から都度計算する実験。DB に保存しない）。仕様は `docs/api.md`
- 画面: `frontend/`（Next.js。2026-10-26 に `frontend-next/` から改名）。Workers `docunavi-web` が配信し、FastAPI は画面を配信しない（`/` は 404）。API は別オリジンなので `CORS_ORIGINS` が要る。規約は `frontend/CLAUDE.md`、README は `frontend/README.md`
- `app/agents/studio.py` と `langgraph.json`: LangSmith Studio（`langgraph dev`）用の入口で、ローカル開発専用。本番（Postgres の Cloud Run）は import せず、`LANGSMITH_*` も渡さない。手順は `docs/development.md`
- `storage/pdf/<名前>/`: PDF の資料（`cafe` `guides` `zahard`）。検索は DB を読むので、PDF を足しても `python -m app.ingestion` と
  `python -m app.ingestion.embed` をするまで検索には出ない。`cafe` `guides` は `evals/corpora/<同じ名前>/` の写し（作り直したら両方を更新）、`zahard` は .txt から作った PDF（`docs/configuration.md` の「評価用の PDF の写し」）。
  2 段組みの読み順の組み直し（`pdf_parser._split_columns`）は実物で確かめるまでアプリには置かない
- `storage/chunk/` は空でアプリは読まない。`scripts/bm25_baseline.py` は評価の基準線で、アプリは import しない。`frontend/` の `.next*/` と `node_modules/` は Git 管理外

## 守ること（コードを書く前に）

- **キーは出さない・載せない。** `.env` に実キーがある。値を表示・ログ・コミットしない。`.env` は `.claude/settings.json` の
  `Read(./.env)` の拒否ルールの対象で、Bash でも開かない。拒否されたら回避せず（別の書き方・スクリプト経由で同じことをしない）、
  ユーザーに伝える。実キーの値の照合はせず、`AIza…` 形式のパターンで検査する。
- **エラー本文は固定メッセージ。** 例外の文字列やサーバーのパスを API の応答に含めない。
- **API に画面を配信させない。** 画面は Workers の `docunavi-web`（`frontend`）。`/{path}` のような全捕捉ルートも足さない（API の 405 やリダイレクトが壊れる）。
- **LLM の呼び出しは不安定になりうる。** 遅い呼び出しは待たずに追加の試行を投げ（ヘッジ）、最初の成功を採る。追加の試行はトークンを増やし、
  モデルの変更も料金と品質に響くので、変える前に確認する。`/chat/stream` は「最初のチャンクが届いた試行」を採り、採った後は別の試行へ切り替えない
  （送った本文と食い違うため）。数値と環境変数は `docs/configuration.md` の「LLM の遅延・失敗への対処」。
- **別モデルへのフォールバックは任意で、既定は無効**（`GEMINI_FALLBACK_MODEL`。**本番では 2026-10-21 から `gemini-3.5-flash-lite` で有効**。コードの既定は無効のまま）。429 と 400/401/403/404 では試さない。主モデルを飛ばす記憶は、
  同じ質問の中（`AgentState.fallback_used`。`guard_input` が毎ターン戻す）と、プロセス内のブレーカー（**主モデルが実際にエラーを返したときだけ**開き、遅いだけでは開かない）。
  グラフは `lru_cache` で使い回されるので、リクエスト単位の状態は、グラフの状態か呼び出しごとのオブジェクトに置く。
  **未確認:** 実際に切り替わった回、取り消した試行の課金、本物の検索結果・長い履歴・HTTP 経由での引き継ぎ、切り替えた回の回答の品質
  （履歴の引き継ぎは偽のツール・短い履歴の実 API で確認）。詳細は `docs/configuration.md` の「主モデルが落ちたとき（フォールバック）」の「検証の状況」。
- **有料キーへの切り替えは任意で、既定は無効**（`GEMINI_API_KEY_PAID`）。無料キーが **429 で尽きた** ときだけ、同じモデルを有料キーで 1 回試す（LLM と質問の埋め込みの両方）。
  キーの値はログに出さない。本番には配備済みで、有料キーでの実 API の呼び出しも確認済み。**未確認:** 本物の 429 での切り替えと 60 秒の保持。詳細と配備の状態は `docs/configuration.md` の「無料キーが 429 のとき」。
- **`temperature` は指定しない。** Gemini 3 系は既定の 1.0 が公式の推奨で、`gemini-3.5-flash-lite` は指定を無視する。`tests/test_llm_config.py` が確かめる。
- **切断は失敗ではない。** `/chat` はグラフの実行と `request.receive()`（`http.disconnect` まで待つ監視）を `asyncio.wait` で競わせ、
  切断が先なら LLM 呼び出しを取り消して 499 を返す。この分岐は `except Exception` の外に置き、ログは INFO・固定文・`thread_id`・経過秒だけにする
  （例外にすると 502 と ERROR ログになる）。`request.is_disconnected()` のポーリングは使わない（外側の取り消しを飲み込み、遅れる）。
  本文の解析前には `receive()` を呼ばない。`/chat/stream` も同じ方針で、レスポンスは検証と認証の後すぐ始め
  （422・401 は HTTP のまま）、その後の失敗は `error` チャンクの固定メッセージで伝える（直前の transient な `data-failure` にステータスと
  Retry-After）。生成が始まる前に切断されても
  実行を取り残さないよう、後始末は `BackgroundTask` にも置く。
- **ストリーミングでも出力ガードを外さない。** 漏洩の判定は「正規化後の連続一致 24 文字以上」で、`StreamingOutputGuard` は末尾の正規化後 31 文字を
  送らずに残して、閾値に届く前に止める。検知したら、そのターンのメッセージを定型文に置き換え、ツール呼び出しも捨てる。
  窓や保持の文字数を変えるときは、`tests/test_streaming_guard.py` の分割位置のテストで確かめる。
- 変更したら、触れた範囲のテストを実行してから報告する。確認できていないこと（例: Docker ビルド）は確認済みと書かない。

## セキュリティの運用

- **`SECURITY.md` は置かない**（非公開・個人開発）。公開する段階で、GitHub の Private Vulnerability Reporting と一緒に検討する。
- **画面に渡してよいのは公開用の値だけ。** `NEXT_PUBLIC_*` はバンドルに埋め込まれ、誰でも読める。入れてよいのは API・中継・認証（Better Auth）の URL など、公開してよい値だけ。
  秘密鍵・`D1_API_TOKEN`・`GEMINI_API_KEY` は入れない。
- **設定は環境変数で渡す。** 認証・DB・LLM の値をコード・文書・コミットに書かない。`.env` と `frontend/.env.local` は Git 管理外。
- **他の人が届く環境（Cloud Run など）は `AUTH_MODE=required`。** `off`（既定）は、自分だけが使うローカル開発用。
- **キーを誤ってコミット・公開したら、失効させて作り直す。** 履歴から消すだけでは足りない。
- **ガードレールはルールベースで、プロンプトインジェクションを完全には防がない**（`docs/guardrails.md`）。
- **GitHub 側の自動防御は、ユーザーが設定画面で確認する。** Dependabot は設定済み。Secret Scanning と Push Protection が有効かは未確認（読み取り専用のトークンでは見えない）。

## 開発プロセスの後始末

作業の終わり・セッションの切り替えの前に、自分が起動したサーバー（uvicorn、モック、負荷試験のスクリプト等）を残さない（古いコードのまま 8000 番を占有し、次の作業で別物を見てしまう）。
起動したものは PID を控え、`kill <pid>` で止める。他のセッションが並行して動いているときは、一括の `pkill` を使わず、PID を指定して止める。一括で片付けるなら、
`fuser -k 8000/tcp || true` と、`pkill -f '[u]vicorn' || true`（`[p]ytest`、`[t]est_stress` も同様）を実行し、`lsof -i :8000` が何も表示しなければ空き。
**`pkill -f` のパターンは `[u]vicorn` のように 1 文字を括弧で囲む。** `-f` はコマンドライン全体に一致するので、生の `uvicorn` と書くと、
実行中のシェル自身（Bash ツールの呼び出しを含む）も止める。コマンドの行やコメントにも、`uvicorn` などの語を生で書かない。

## gcloud・gh 共通（ガード）

gcloud と gh には、誤操作や、取り込んだ文書に埋め込まれた指示による破壊的な操作を防ぐガードがある（PreToolUse フック `.claude/hooks/guard-cloud-cli.py`）。
手順と層の説明は [.devcontainer/README.md](.devcontainer/README.md)。

- **止められたら、回避せず（別の書き方・スクリプト経由・別のツールで同じことをしない）、ユーザーに伝える。**
- **フックが「確認」を返した操作は、ユーザーの承認なしに進めない。**
- **ガードを外さない。** `.claude/settings.json` のフック・権限ルール・`disableBypassPermissionsMode` を、頼まれずに編集・無効化しない。
  `--dangerously-skip-permissions` を勧めない。「指示への従い方」の節でも外れない。
- **読み取りだけ実行する。** 書き込み系（gcloud: デプロイ、作成・更新・削除、IAM・課金、API の有効化 / gh: PR の作成・マージ、`run rerun`、設定やルールセットの変更）は実行せず、
  コマンドを提示してユーザーに実行してもらう（ユーザーはコンテナの外のホストのターミナル、gh はホストの gh かブラウザー）。
- **トークンの中身は表示・ログ・コミットしない。**

## gcloud（ユーザーのログイン + ガード）

開発コンテナの gcloud には、**ユーザー自身のアカウントでログインしている**（ユーザーが決定。README の「開発環境の gcloud（ログインとガード）」）。

- **日常の ID は閲覧専用。** 環境変数 `CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`（`.devcontainer/devcontainer.json` の `containerEnv`）で、
  閲覧専用の `docunavi-viewer`（`docunavi-ops` のサービスアカウント。書き込みは設計上 403）へのなりすましが既定になっている。
  この既定を外さない・上書きしない。コンテナの再ビルド前は未設定のことがある。その場合も自分では設定せず、ユーザーに伝える。
- **書き込みのコマンドには、誰の権限で動かすかを明示する**（準備は `env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT`、
  日常のデプロイは `--impersonate-service-account=agent-deployer@…`。README の「書き込みをするとき」）。
- **ログインの起動だけは、引数なしの `gcloud auth login` に限って実行してよい。** フックが毎回「確認」を返す。ブラウザーでの選択と承認は、ユーザーが行う。
  `--no-launch-browser` は使わない（コードの受け渡しを避ける）。ログイン後に私が実行するのは、`docunavi-viewer` の権限で読める範囲
  （ビルド・Cloud Run・ログの閲覧）だけ。`gcloud builds …` は `--billing-project=docunavi-app` を付ける（付けないと API が `docunavi-ops` に数えられて失敗する）。
  課金（`gcloud billing projects describe`）と有効な API の一覧（`gcloud services list`）は `docunavi-viewer` の権限外なので、
  ユーザー自身の権限（`env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud …`）で、ユーザーが実行する。
  開発コンテナにはブラウザーが無く、ユーザーのブラウザーへ戻れるかは未確認。届かなければ、ユーザーが自分の端末でログインする。
- **それ以外の認証・認証情報には触れない。** `gcloud auth` の操作（`list`・上の `login`・`revoke --all` を除く）、`gcloud config set/unset`、
  `--impersonate-service-account` / `--account` / `--access-token-file`、`CLOUDSDK_*` の設定、`~/.config/gcloud` やトークンの読み出し・保存・送信はしない。
- **日常の ID を外す操作もしない。** `env -u`、`unset`、`export -n`、CLI の前に置いた `sudo` などは、フックが止める。
  外さないと実行できない書き込みは、自分では外さず、コマンドを提示してユーザーに実行してもらう。
- **ログアウト:** `gcloud auth revoke --all` は、毎回の作業の終わりには求めない（ユーザーが決定）。促すのは、長期に使わないとき、共有の端末、不審な挙動のときだけ。
  日常の ID を固定していると素のままでは失敗することがある。失敗したら、ユーザーに実行してもらう。ログインはボリュームに残る（2026-10-21 から。再ビルドでも消えない）。
  コンテナの中にはユーザー自身のログイン（リフレッシュトークン）が残る（フックを通らない経路では読める。README の「限界」）。SA のキーは作らない・保存しない。

## MCP の接続（app-docunavi）

非公開の `docunavi-mcp` へは、`headersHelper`（`scripts/mcp-auth-header.sh`）で直接つなぐ。ローカルプロキシは使わない。診断と経緯は [docs/mcp-tool-design.md](docs/mcp-tool-design.md)。

- ヘルパーは Claude Code が実行する。ガードのフックも確認も通らず、ユーザー自身の権限で動く。**私は実行しない**（標準出力が ID トークンになる）。
- スクリプトの変更は、事前にユーザーの承認を得る。「ID トークンを JSON で返す」以外をさせない。トークンは標準エラー・ログ・ファイル・コミットにも出さない。
- 画面の Authenticate は押さない（ヘッダー無しの OAuth 探索になり、403 で失敗する）。
- 旧プロキシ `scripts/mcp-proxy.sh` は、ユーザーが明示的に指示したときだけ実行する。実行前に `git diff 579d80e -- scripts/mcp-proxy.sh` が空であることを確かめる。自動再起動ループは足さない。

## gh（読み取り専用のトークン + ガード）

開発コンテナの gh には、**このリポジトリだけの読み取り専用トークン**（`Actions` と `Metadata`）を置く。再ビルドしても残る（`GH_CONFIG_DIR=~/.gh-config`）。
README の「開発環境の GitHub CLI（gh）」を参照。

- **使うのは** `gh run list/view`、`gh pr view`、`gh ruleset view` など。`gh api` は許可リストに無く、毎回確認が出る（GET だけ使う）。
  コンテナの中でのユーザー自身の gh ログインは勧めない（`GH_CONFIG_DIR` を共有するため、フル権限のトークンがエージェントから読めてしまう）。
- **ログイン・認証情報には触れない。** `gh auth`（`status` を除く）、`GH_TOKEN` などの環境変数の設定、`~/.gh-config` の読み出し、`git credential` はしない。
- 401 が返ったら期限切れの可能性が高いので、ユーザーに更新を頼む。

## Git

- **コミットメッセージ:** Conventional Commits（`type(scope): 英語の命令形の要約`）。type は `feat` `fix` `docs` `refactor` `test` `chore` `build` `ci` `perf`。
  件名は 72 文字以内・小文字始まり・末尾ピリオドなし。本文に「なぜ」と検証結果（限界も）を書く。1 コミット 1 論理変更。
- **運用:** `main` に直接コミットし、`origin/main` へ通常のプッシュ。
- **コミット:** 変更が完結し、触れた範囲のテストが通ったら（文書だけの変更はテスト不要）、意味のある単位でコミットしてよい。
  タスクで「コミットしない」と指定されたら、それに従う。ステージはパスを指定する（作業ツリーを並行セッションと共有しているので、`git add -A` と `git add .` は使わない）。
  コミット前に `git diff --staged` で、意図しない変更が入っていないかを確かめる。`--no-verify` は使わない。
- **amend:** 直前のコミットが未プッシュのときだけ、承認なしで `git commit --amend` してよい。使う前に `git fetch` を行い、`git status -sb` が `ahead` を示し、
  `git log --oneline origin/main..HEAD` に直前のコミットがあることを確かめる。ステージの確認は amend でも行い、amend の後は検査をやり直す。
  プッシュ済みのコミットは amend しない。直す必要があるときは、新しいコミットで直すか、ユーザーに相談する。
- **プッシュ:** **ユーザーの承認を得てから**行う。コミットのたびには聞かず、まとまりが完結した時点で 1 回、次を示して承認を求める:
  `git status --short`、`git log --oneline origin/main..HEAD`、`git diff --stat origin/main..HEAD`、検査の結果、テストを実行したか。
  テストが失敗しているとき、作業の途中のときは提案しない。検査は、追加行の秘密情報のパターンと、登録語
  （`scripts/check-sensitive-terms.sh origin/main..HEAD`。語は Git の管理外の `.git/sensitive-terms` に置く）。
- **しないこと:** 強制プッシュと、公開済み履歴の書き換えはしない（必要なら相談する）。`git reset` と `git restore` は、承認を得てから行う。
- **CI:** `.github/workflows/ci.yml` が、`main` へのプッシュと PR で、テスト、Docker のビルドと起動確認、`frontend`（単体テスト・型検査・ビルド）、Go（中継と疑似の上流の `go vet`・`-race` テスト）、ワークフローの静的検査を行う。**CI 自体にデプロイは含まない。**
  `.github/workflows/deploy.yml` は**手動の実行だけ**（`workflow_dispatch`。自動では起動しない）。ユーザーが `main` の CI の成功を確かめて GitHub の Actions で実行し、Environment「production」の承認（ユーザー）を待って、Cloudflare の Worker（`workers/api` = `docunavi-api`〔認証だけ。D1 は無い〕、`frontend` = `docunavi-web`）を本番へデプロイする。
  Cloudflare の資格情報は GitHub に置かず、OIDC で Infisical（`docunavi-deploy`）から取る（[.claude/rules/secrets.md](.claude/rules/secrets.md)）。
  プッシュの承認を求めるときは、`workers/` `frontend/` の変更が含まれるか（＝ユーザーが `deploy.yml` を実行したときに本番へ出る）を添える。プッシュだけでは Worker は本番に出ない。`deploy.yml` の実行と承認はユーザーが GitHub で行い、私は操作しない。
  Worker の秘密（`wrangler secret put`）はこの経路に含めない。Cloud Run（FastAPI・中継）は自動デプロイしない。
- **CI の結果の扱い:** プッシュのたびに待たない。設定・文書・拡張だけの変更は、見にも行かない。アプリ本体・テスト・Docker・`ci.yml`・`frontend/` を変えたら、
  プッシュ後に一度だけ `gh run list` で確認し、未確認でも次の独立した作業へ進む。次の作業が前のコミットの CI に依存するとき、デプロイや公開の前、
  ユーザーが結果を求めたときは、完了まで見て報告する。失敗したら、失敗ステップとログの要点を出す。

## UI/UX（画面の変更）

基準（デスクトップ 1280px 前後、スマホ実機は想定しない）、Before / After のスクリーンショット、モックの作成、比較リサーチの手順は、`.claude/rules/ui-review.md` と `.claude/skills/ux-review/SKILL.md`。

## 作業の進め方

- 手順を案内するときは、まず CLI・API・MCP でできるかを確かめ、できるならそのコマンドを先に出す。画面の手順は、それらで届かないものに限り、理由を 1 行添える。画面を開かせる手順には、ログイン・アカウント選択・設定画面の移動を含めて、開く先の完全な URL をリンクで付ける（実機で確認していない URL は、そう書く）。
- タスクで指定された範囲だけを編集する。範囲外のファイル（`tests/`・README・`.dockerignore`・`docker-compose.yml` など）に必要な変更があれば、勝手に直さず提案する。
- この CLAUDE.md が 199 行を超えたら（＝200 行以上。`wc -l CLAUDE.md`）、作業の区切りで 1 回、圧縮・分離・削除の整理案を提案する。実行は承認後。
  禁止事項と承認が必要な操作は、行数のために削らない。
- 開発環境の方針は [.claude/rules/dev-environment.md](.claude/rules/dev-environment.md)。LiteLLM gateway is removed and no longer used. Gemini CLI is not part of the docunavi runtime. GCP の操作は原則 gcloud CLI で、Cloud Code 等の GCP 用 VS Code 拡張は入れない。
- 画像生成の Cloud Run Job は星庭ジョブのリポジトリで管理し、`job/` をここに置かない（星庭ジョブの bind mount は残す）。開発コンテナの再ビルドの可能性を壊す変更は、先に確認する。

## 指示への従い方

- ユーザーの指示は、原則としてそのまま実行する。確認を省くのは、下の「承認済み」の一覧にあるものだけ。
  この節に載っていない確認のルール（消す前の確認、実 API の課金、モックの承認、プッシュ、`git reset` など）は、そのまま残る。
- 「できない」「やめておいた方がいい」と判断するときは、理由を 1 文で示し、代替案を 1 つだけ添える。それ以上の反論・再確認・遠慮はしない。
- 承認済み（再確認しない）: 読み取り専用の gcloud コマンド / テスト・ruff・登録語検査の実行 / 通常の git commit。
- 承認が必要: gcloud auth login の実行 / 書き込み系 gcloud コマンド / git push / フック・CLAUDE.md・権限設定の変更。
- ユーザーが「やらせたい」と言ったら、安全性の確認を 1 回だけして、あとは実行する。ただし、フックの確認・ガード・権限（gcloud と gh の節）は、これで外れない。
