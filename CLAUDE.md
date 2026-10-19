# CLAUDE.md

資料（架空の惑星「ザーハード」の設定資料と、PDF の資料）に答える RAG チャットボット。FastAPI + LangGraph + Gemini のバックエンドと、
Vite + React + TypeScript のフロントエンド（ビルド成果物を FastAPI が配信）。入口は [README.md](README.md)、
詳細は [docs/](docs/)（API は `docs/api.md`、環境変数は `docs/configuration.md`、今後の計画は `docs/roadmap.md`）。

## よく使うコマンド

| 目的 | コマンド |
|---|---|
| バックエンド起動 | `uvicorn app.main:app --reload`（`.env` が無い・キー未設定ならモックモード） |
| バックエンドのテスト | `pytest`（`tests/conftest.py` がモックモードに固定する。実 API は呼ばない）。Postgres のテストは `TEST_DATABASE_URL` が無いとスキップされる |
| Postgres 込みのテスト | `make test-db`（一時的な Postgres 17 を起動 → `pytest` 全体 → 停止。失敗しても停止する）。個別には `make test-db-up`（接続 URL を表示）→ `TEST_DATABASE_URL=<URL> pytest …` → `make test-db-down`。初回だけ `apt-get download` でパッケージ（約 20MB）を取得する。ポートは 55432。手順は [docs/development.md](docs/development.md) |
| フロントエンド | `cd frontend && npm ci` の後 `npm run dev` / `npm test` / `npm run build` |
| E2E（ブラウザー） | `cd frontend && npm run test:e2e`（偽の LLM・偽の検索の本物のアプリ `tests/e2e_server.py` に Playwright でつなぐ。実 API は呼ばない。初回だけ `npx playwright install chromium`。手順は [docs/development.md](docs/development.md)） |
| 依存の更新 | `requirements*.in` を編集し、`uv pip compile`（コマンドは [docs/development.md](docs/development.md) の「テスト」節）で `requirements*.txt` を再生成する。`.txt` は直接編集しない |
| Docker | ホスト側で `docker compose up --build`（開発コンテナ内には Docker が無い） |

## 構成

- `app/api/routes.py`: `/chat` `/chat/stream` `/health`。LLM の失敗は固定メッセージの 429/504/500/502 に変換する。
  クライアントが切断したら LLM 呼び出し（ヘッジの追加試行を含む）を取り消す。`/chat/stream` は回答を SSE
  （AI SDK の UI message stream。組み立ては `app/api/ui_stream.py`）で返し、画面はこちらを使う
- `app/agents/`: LangGraph（`graph.py`）、検索ツール（`tools.py`）、ガードレール（`guardrails.py`。出力は
  完成した回答の検査と、ストリーミング中の逐次検査 `StreamingOutputGuard`）、LLM 呼び出しのヘッジ付き
  再試行（`resilience.py`。完了を待つ `hedged_call` と、最初のチャンクを待つ `hedged_stream`。別モデルへの
  フォールバックの `call_with_fallback` / `stream_with_fallback` と、主モデルを一時的に飛ばす `PrimaryBreaker` もここ）
- `app/history.py`: 会話履歴の保存先。`DATABASE_URL` があれば Postgres（接続プールを lifespan で開閉）、空ならメモリ。
  開けなければ起動を失敗させ、メモリへは切り替えない
- `app/search_vector.py`: 資料の検索（Supabase / pgvector）。チャットの検索ツール `search_documents`（`app/agents/tools.py`）の実体で、アプリの検索はこれだけ。
  質問を `gemini-embedding-2` で埋め込み、DB の `chunks`（`documents.status = 'ready'` の全チャンク）とのコサイン類似度で上位 `TOP_K`（2）を取る。
  `MIN_SIMILARITY`（0.65）は粗い下限で、「資料にない」の判断は LLM に任せる。本物の LLM では `DATABASE_URL` と `GEMINI_API_KEY` が必須で、
  起動時に DB の状態（`ready` の文書に現在のモデルの埋め込みがあるか）を検査し、合わなければ起動を失敗させる。失敗しても別の経路には戻らず、
  API は 503 の固定文を返す。モックモード（キー未設定）は、資料を検索しない固定の返答（DB も API も使わない）。
  テストは `tests/conftest.py` が検索を偽物に差し替える（DB も埋め込みの API も使わない）。設計の根拠と見直す条件は `docs/search-design.md`
- `app/auth.py`: ログイン。`AUTH_MODE=off`（既定）は匿名、`required` は `/chat` と `/chat/stream` に Supabase の JWT（JWKS の ES256/RS256 だけ。`exp` `iss` `aud` `sub` 必須、`sub` は UUID）を要求し、
  履歴のキーを `<sub>:<thread_id>` にする（`sub` 以外から利用者 ID を取らない）。公開鍵が無ければ共有秘密には切り替えず、起動を失敗させる。
  実際の Supabase・Google でのログインと質問は、ローカルで確認済み（2026-10-19）。トークンの期限切れ後の更新、Docker、公開環境は未確認（`docs/configuration.md` の「ログイン（認証）」）。画面側は `frontend/src/auth/`
- `app/api/threads.py`: 会話の一覧・発言の取得・削除（`GET /threads`、`GET /threads/{thread_id}/messages`、`DELETE /threads/{thread_id}`。画面は、ログイン中の左サイドバー `ThreadSidebar`。会話を選ぶと続きから始まるが、選ぶと `/messages` で以前のやり取りを取得して並べる。出典パネルは付かず、ページの再読み込みだけでは取得しない）。`required` では本人の会話だけ。`off` は一覧が 404（匿名の会話に持ち主がいない）。題名は最初の発言の先頭で、列も LLM も使わない
- `app/api/documents.py`: 資料一覧（`GET /documents`、`/documents/{id}/chunks`。読み取り専用）。DB の `documents` / `chunks` の内容を返す（埋め込みのベクトルは返さない）。
  チャットの検索が読むのと同じ DB。`AUTH_MODE=required` で認証。画面は「資料一覧」タブ（`DocumentsView`。入寮規程だけ「関係図」の切り替えがある）
- `app/history_admin.py`: 履歴の削除コマンド（`python -m app.history_admin`。既定は件数の表示だけ、`--apply` で削除）。アプリからは使わない
- `app/frontend.py`: `frontend/dist` を配信し、ファイル名らしくない未知の GET/HEAD は `index.html` にフォールバックする
  （拡張子付きのパスや `/assets/` 配下は 404）
- `app/relations/`: 入寮規程の条文分割と、規則ベースの関係抽出（references / exception_of / same_chapter / 用語依存 uses_term）。DB には保存しない（実験）。API は `app/api/graph.py` の 2 本（`/documents/{id}/graph` と `/impact`）だけで、入寮規程のみ。リクエストのたびに PDF から計算する。画面は資料一覧の「関係図」（`frontend/src/components/RelationGraphView.tsx`。React Flow + dagre）。測定は `scripts/eval_relations.py`、正解・辞書・結果は `evals/relations/`
- `storage/chunk/`: .txt の資料の置き場。**いまは空**（Git にも無い）。アプリは読まない（評価用の BM25 基準線 `scripts/bm25_baseline.py` だけが読む）。
  元の .txt は `evals/fixtures/zahard_txt/`（原本）
- `storage/pdf/<名前>/`: PDF の資料（取り込みの元。`python -m app.ingestion` で DB に取り込み、`python -m app.ingestion.embed` で埋め込む）。ディレクトリ名は整理用の資料群の名前（いまは `cafe` `guides` `zahard`）。
  検索は DB を読むので、ここの PDF を足しても、取り込みと埋め込みをするまで検索には出ない。
  `storage/pdf/zahard/` の PDF は、`evals/fixtures/zahard_txt/` の .txt を `evals/fixtures/zahard_pdf/make_pdf.py` で PDF にしたもの（話題のまとまりごとの段落 = 12 チャンク。
  題名は /Title の「ザーハード設定資料 1〜3」）。`tests/test_zahard_pdf.py` が、文面・段落の分け方・題名・バイト列を確かめる。PDF の見た目は、資料一覧で開いて目視する（未確認）。
  `storage/pdf/cafe/` と `storage/pdf/guides/` の PDF は `evals/corpora/<同じ名前>/` の PDF の写し（作り直したら両方を更新する。
  テストが一致を確かめる）。2 段組みの PDF は、段を検出して読み順を組み直す（`pdf_parser._split_columns`）が、試したのは reportlab 製の 1 冊だけ。実物の書き出しで確かめるまで、アプリには置かない（`evals/fixtures/two_column/`）
- `scripts/bm25_baseline.py`: 以前アプリが使っていた BM25（ローカルの PDF を読む）。評価の基準線で、アプリは import しない。`scripts/eval_*.py` が使う
- `frontend/`: UI。`dist/` と `node_modules/` は Git 管理外

テストは Claude Code が自分で実行してよい（読むだけ・一時的な起動で、課金も外部への書き込みも無い）。変更したら、上のコマンドで確かめてから報告する。
`make test-db` は一時的な Postgres を立てる（ネットワークを使う初回取得あり）。他のセッションが 55432 を使っていたら失敗して止まる（止めない）。

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
- **別モデルへのフォールバックは任意で、既定は無効。** `GEMINI_FALLBACK_MODEL` を入れたときだけ、主モデルの試行が
  一時的な失敗（5xx・接続・タイムアウト・空の応答）で尽きたあとに、そのモデルで 1 回試す（429 と 400/401/403/404 では
  試さない）。`/chat/stream` だけは、ヘッジの時刻（8 秒）に最初のチャンクが無ければ、フォールバックを並行して投げる
  （先に届いた方を採り、負けた方は取り消す。`/chat` は並行しない）。主モデルを飛ばす記憶は、同じ質問の中
  （`AgentState.fallback_used`。`guard_input` が毎ターン戻す）と、プロセス内のブレーカー
  （`GEMINI_FALLBACK_HOLD_SECONDS`、60。**主モデルが実際にエラーを返したときだけ**開き、遅いだけでは開かない）。
  グラフは `lru_cache` で使い回されるので、リクエスト単位の状態（試行の記録など）は、グラフの状態か
  呼び出しごとのオブジェクトに置く。**有効にする前に**、主モデルで始めた会話
  （thought signature を含む履歴）を別モデルで続けられることを、実 API で両方向とも確かめる（未検証）。
  取り消した試行の課金も未確認。詳細は `docs/configuration.md` の「主モデルが落ちたとき（フォールバック）」。
- **有料キーへの切り替えは任意で、既定は無効。** `GEMINI_API_KEY_PAID`（予算ガード付きプロジェクトのキー）を入れたときだけ、
  `GEMINI_API_KEY`（無料キー）の試行が **429 で尽きた** あとに、同じモデルを有料キーで 1 回試す（400/401/403/404 と 5xx では試さない。
  LLM と質問の埋め込みの両方。部品は `resilience.py` の `KeySwitch` / `call_with_key_switch` / `stream_with_key_switch`）。
  有料キーが成功してから `GEMINI_KEY_SWITCH_HOLD_SECONDS`（60）の間は、最初から有料キーを使う。キーの値はログに出さない。
  本物の 429 での確認は未了。詳細は `docs/configuration.md` の「無料キーが 429 のとき」。
- **`temperature` は指定しない。** Gemini 3 系は既定の 1.0 のままが公式の推奨（1.0 未満はループや性能低下の恐れ）で、
  `gemini-3.5-flash-lite` は指定を無視する。`tests/test_llm_config.py` が、渡さないことと、ライブラリが未指定なら
  送らないことを確かめる。
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

## セキュリティの運用

非公開・個人開発なので、`SECURITY.md`（脆弱性の報告窓口）は置かない。ルールはここに置く。リポジトリを公開する段階で、
`SECURITY.md` と GitHub の Private Vulnerability Reporting を一緒に検討する。

- **画面に渡してよいのは公開用の値だけ。** `VITE_*` はビルド時にバンドルへ埋め込まれ、誰でも読める。入れてよいのは
  Supabase の URL と publishable key だけ。secret key・`DATABASE_URL`・`GEMINI_API_KEY` は `VITE_*` に入れない。
- **設定は環境変数で渡す。** 認証・DB・LLM の値をコード・文書・コミットに書かない。`.env` と `frontend/.env.local` は Git 管理外。
- **他の人が届く環境（Cloud Run など）は `AUTH_MODE=required`。** `off`（既定）は、自分だけが使うローカル開発用。
- **キーを誤ってコミット・公開したら、失効させて作り直す。** 履歴から消すだけでは足りない。
- **ガードレールはルールベースで、プロンプトインジェクションを完全には防がない**（`docs/guardrails.md`）。
- **GitHub 側の自動防御は、ユーザーが設定画面で確認する。** Dependabot は設定済み（`.github/dependabot.yml`）。
  Secret Scanning と Push Protection が有効かは未確認（読み取り専用のトークンでは見えない）。

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
- **ログインの起動だけは、引数なしの `gcloud auth login` に限って実行してよい。** フックが毎回「確認」を返す。
  ブラウザーでのアカウントの選択と承認は、ユーザーが行う。`--no-launch-browser` は使わない（コードの受け渡しを避ける）。
  ログイン後に私が実行するのは、`agent-viewer` の権限で読める範囲（ビルド・Cloud Run・ログの閲覧）だけ。
  課金（`gcloud billing projects describe`）と有効な API の一覧（`gcloud services list`）は `agent-viewer` の権限外なので、
  あなた自身の権限（`env -u CLOUDSDK_AUTH_IMPERSONATE_SERVICE_ACCOUNT gcloud …`）で、あなたが実行する。
  開発コンテナにはブラウザーが無く、ユーザーのブラウザーへ戻れるかは未確認。届かなければ、ユーザーが自分の端末でログインする。
- **それ以外の認証・認証情報には触れない。** `gcloud auth` の操作（`list`・上の `login`・`revoke --all` を除く）、`gcloud config set/unset`、
  `--impersonate-service-account` / `--account` / `--access-token-file`、`CLOUDSDK_*` の設定、
  `~/.config/gcloud` やトークンの読み出し・保存・送信はしない。PreToolUse フック（`.claude/hooks/guard-cloud-cli.py`）が止める。
  止められたら、回避せず（別の書き方・スクリプト経由・別のツールで同じことをしない）、ユーザーに伝える。
- **日常の ID を外す操作もしない。** `env -u`、`unset`、`export -n`、CLI の前に置いた `sudo` などは、フックが止める。
  外さないと実行できない書き込みは、自分では外さず、コマンドを提示してユーザーに実行してもらう。
- **フックが「確認」を返した操作は、ユーザーの承認なしに進めない。**
- **ガードを外さない。** `.claude/settings.json` のフック・権限ルール・`disableBypassPermissionsMode` を、頼まれずに
  編集・無効化しない。`--dangerously-skip-permissions` を勧めない。
  フックの確認・ガード・権限は、「指示への従い方」の節でも外れない。
- **トークンの中身は表示・ログ・コミットしない。** 作業の終わりには、`gcloud auth revoke --all` でログアウトする
  （日常の ID を固定していると、素のままでは失敗することがある。失敗したら、ユーザーに実行してもらう）。ログインはボリュームに残さない。

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
- **CI の結果の扱い:** プッシュのたびに待たない。
  - 設定・文書・拡張だけの変更は、CI を待たず、見にも行かない。
  - アプリ本体・テスト・Docker・`ci.yml` を変えたら、プッシュ後に一度だけ `gh run list` で確認する。結果が未確認でも、次の独立した作業へ進む。
  - 次の作業が前のコミットの CI に依存するとき、デプロイや公開の前、ユーザーが結果を求めたときは、完了まで見て報告する。失敗したら、失敗ステップとログの要点を出す。

## UI/UX（画面の変更）

- 基準はデスクトップブラウザ（幅 1280px 前後）。スマホ実機での利用は想定しない。390px は「レイアウトが壊れないか」の確認だけで、モバイル向けの最適化はしない
- 画面を変えたら、Before / After の Playwright スクリーンショットを撮る（Playwright の保存先は `.playwright-mcp/`）
- 「見た目を変える」依頼は、色・余白・書体・密度の変更を含める。機能だけの変更では、見た目の差が出ない
- レイアウトを変える変更は、実装の前にモック（2 案以上、Claude Design）を作って選んでもらう。省く場合も、先に伝えて承認を得る（`.claude/rules/ui-review.md`）
- 競合など他社の比較は、証拠等級（A: 実機で確認 / B: 公式の文書 / C: 第三者の記事 / 不明）を付ける。実機で確認していないことを「確認済み」と書かない
- 手順と制約は `.claude/skills/ux-review/SKILL.md` と `.claude/rules/ui-review.md`

## 作業の進め方

- タスクで指定された範囲だけを編集する。範囲外のファイル（`tests/`・README・`.dockerignore`・
  `docker-compose.yml` など）に必要な変更があれば、勝手に直さず提案する。

## 指示への従い方

- ユーザーの指示は、原則としてそのまま実行する。ただし、確認を省くのは、下の「承認済み」の一覧にあるものだけ。
  この節に載っていない確認のルール（消す前の確認、実 API の課金、モックの承認、プッシュ、`git reset` など）は、そのまま残る。
- 「できない」「やめておいた方がいい」と判断するときは、
  理由を1文で示し、代替案を1つだけ添える。
  それ以上の反論・再確認・遠慮はしない。
- 以下は承認済み。再確認しない。
  - 読み取り専用の gcloud コマンド
  - テスト・ruff・登録語検査の実行
  - 通常の git commit
- 以下は承認が必要。
  - gcloud auth login の実行
  - 書き込み系 gcloud コマンド
  - git push
  - フック・CLAUDE.md・権限設定の変更
- 「念のため確認」はしない。
  上の「承認済み」の一覧にあるものなら、そのまま進める。
- ユーザーが「やらせたい」と言ったら、
  安全性の確認を1回だけして、あとは実行する。
  ただし、フックの確認・ガード・権限（gcloud と gh の節）は、これで外れない。
