# CLAUDE.md

架空の惑星「ザーハード」の設定資料に答える RAG チャットボット。FastAPI + LangGraph + Gemini のバックエンドと、
Vite + React + TypeScript のフロントエンド（ビルド成果物を FastAPI が配信）。詳細な API・環境変数は [README.md](README.md)。

## よく使うコマンド

| 目的 | コマンド |
|---|---|
| バックエンド起動 | `uvicorn app.main:app --reload`（`.env` が無い・キー未設定ならモックモード） |
| バックエンドのテスト | `pytest`（`tests/conftest.py` がモックモードに固定する。実 API は呼ばない） |
| フロントエンド | `cd frontend && npm ci` の後 `npm run dev` / `npm test` / `npm run build` |
| Docker | ホスト側で `docker compose up --build`（開発コンテナ内には Docker が無い） |

## 構成

- `app/api/routes.py`: `/chat` `/health`。LLM の失敗は固定メッセージの 429/504/500/502 に変換する。
  クライアントが切断したら LLM 呼び出し（ヘッジの追加試行を含む）を取り消す
- `app/agents/`: LangGraph（`graph.py`）、検索ツール（`tools.py`）、ガードレール（`guardrails.py`）、
  LLM 呼び出しのヘッジ付き再試行（`resilience.py`）
- `app/frontend.py`: `frontend/dist` を配信し、ファイル名らしくない未知の GET/HEAD は `index.html` にフォールバックする
  （拡張子付きのパスや `/assets/` 配下は 404）
- `storage/chunk/`: 設定資料（RAG のコーパス）
- `frontend/`: UI。`dist/` と `node_modules/` は Git 管理外

## 守ること（コードを書く前に）

- **キーは出さない・載せない。** `.env` に実キーがある。値を表示・ログ・コミットしない。
- **エラー本文は固定メッセージ。** 例外の文字列やサーバーのパスを API の応答に含めない。
- **静的配信は `router.default` に差し込む設計。** `/{path}` のような全捕捉ルートを足さない（API の 405 や
  リダイレクトが壊れる）。`frontend/dist` が無くても API は動き、`/` はビルドを促す 404 を返す。
- **LLM の呼び出しは不安定になりうる。** `gemini-3.1-flash-lite` は速い回と無応答の回が混在する。
  遅い呼び出しは待たずに追加の試行を投げ（ヘッジ、`app/agents/resilience.py`）、最初の成功を採る。
  `LLM_HEDGE_DELAY_SECONDS`（8、0 で無効）/ `LLM_ATTEMPT_TIMEOUT_SECONDS`（30。ヘッジなしは 15）/
  `LLM_MAX_ATTEMPTS`（3）、
  全体の上限は `LLM_TIMEOUT_SECONDS`（60）。追加の試行はトークンを増やす。モデルの変更も料金と品質に
  響くので、変える前に確認する。
- **切断は失敗ではない。** `/chat` はグラフの実行と `request.receive()`（`http.disconnect` まで待つ監視）を
  `asyncio.wait` で競わせ、切断が先なら LLM 呼び出しを取り消して 499 を返す。この分岐は `except Exception` の
  外に置き、ログは INFO・固定文・`thread_id`・経過秒だけにする（例外にすると 502 と ERROR ログになる）。
  `request.is_disconnected()` のポーリングは使わない（外側の取り消しを飲み込み、遅れる）。本文の解析前には
  `receive()` を呼ばない。
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

## gcloud（閲覧専用・短期トークン）

開発コンテナには gcloud が入るが、**ログイン情報は置かない。** GCP の参照は、ホストで発行した閲覧専用サービス
アカウントの短期トークン（`.gcloud-token`、約 1 時間で失効、Git・Docker の対象外）だけで行う。
設定と発行の手順は [README.md](README.md) の「開発環境の gcloud（閲覧専用）」。

- **常に `gcloud --access-token-file=.gcloud-token ...` で呼ぶ。** `gcloud auth login`・`gcloud config` での
  ログイン、`~/.config/gcloud` への認証情報の保存はしない。
- **読み取りだけ。** 書き込み系（デプロイ、IAM の変更、トリガーの作成・削除、API の有効化など）は実行せず、
  コマンドを提示してユーザーに実行してもらう。
- **トークンの中身は表示・ログ・コミットしない。** 失効（認証エラー）したら、ホストでの再発行
  （`./scripts/issue-agent-token.sh <プロジェクト ID>`）を依頼する。

## Git

- **コミットメッセージ:** Conventional Commits（`type(scope): 英語の命令形の要約`）。
  type は `feat` `fix` `docs` `refactor` `test` `chore` `build` `ci` `perf`。件名は 72 文字以内・小文字始まり・
  末尾ピリオドなし。本文に「なぜ」と検証結果（限界も）を書く。1 コミット 1 論理変更。
- **運用:** `main` に直接コミットし、`origin/main` へ通常のプッシュ。コミット・プッシュは**依頼されたときだけ**行う。
  強制プッシュや公開済み履歴の書き換えはしない。プッシュ前に追加行の秘密情報を確認する。
- Cloud Run への自動デプロイ（GitHub Actions）を足したら、`main` へのプッシュ = デプロイになる。
  そのときはこの運用を見直す。

## 作業の進め方

- タスクで指定された範囲だけを編集する。範囲外のファイル（`tests/`・README・`.dockerignore`・
  `docker-compose.yml` など）に必要な変更があれば、勝手に直さず提案する。
