# ドキュナビ（DocuNavi）

資料を聞く・探す、RAG チャットボット（FastAPI × LangGraph × Gemini）です。資料を根拠に答えます。検索する資料は、`storage/pdf/<資料群の名前>/*.pdf` の PDF です。いまは、
架空の惑星「ザーハード」の設定資料（`zahard/`）、架空のカフェチェーン「みなとコーヒー」の店舗案内（`cafe/`）、
架空の洗濯乾燥機の取扱説明書・学生寮の入寮規程（`guides/`）があります。

チャット（`/chat`・`/chat/stream`）は、DB（Supabase / pgvector）に取り込んだ資料を、質問の埋め込みとのコサイン類似度で検索します
（検索の設計と、見直す条件は [docs/search-design.md](docs/search-design.md)）。本物の LLM には `DATABASE_URL` と `GEMINI_API_KEY` が要ります
（[docs/configuration.md](docs/configuration.md)）。
バックエンド（FastAPI）は API だけを持ちます。チャット画面（Next.js + React + TypeScript、`frontend/`）は Vercel が配信します。

- **FastAPI**: HTTP API（`/chat`, `/chat/stream`, `/health`）
- **LangGraph**: 入力ガード → エージェント（ツール呼び出しのループ）→ 出力ガード
- **Gemini**（主モデルはコード既定の `gemini-3.1-flash-lite`、フォールバックは `gemini-3.5-flash-lite`）: 回答生成。Gemini API キー、または `GOOGLE_GENAI_USE_VERTEXAI=true` のとき Vertex AI（ADC）で認証し、どちらも未設定ならモックモードで動く。
  本番の Cloud Run（`docunavi-00025-qey`）には `GEMINI_MODEL` と `GOOGLE_GENAI_USE_VERTEXAI` の設定が無く、`GEMINI_FALLBACK_MODEL=gemini-3.5-flash-lite` がある（2026-10-31 に `gcloud run services describe` で確認）。本番のキーの渡し方は**要確認**
- **RAG**: PDF を、ページの文字の位置から見出し・段落・表を組み立ててチャンクにし、DB に取り込んで埋め込む（`python -m app.ingestion`）。質問ごとに埋め込みを作り、類似度の高いチャンクを LLM に渡す
- **画面**: `frontend/`（Next.js + React + TypeScript）。Vercel が配信し、ブラウザーが API（Cloud Run の FastAPI）を直接呼ぶ。FastAPI は画面を配信しない

## 起動

### Python で起動

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # GEMINI_API_KEY を設定（未設定ならモックモード）
uvicorn app.main:app --reload
```

### Docker Compose

Docker Compose を使う場合は、ホスト側で `docker compose up --build` を実行します（`.env` が無くても起動し、その場合はモックモードです）。
画面の開発サーバー（Next.js のホットリロード）は別のコンテナ `frontend` で動き、<http://localhost:3000> で開きます（`api` が healthy になってから起動し、初回は `npm install` で時間がかかります）。
`api` はポートを公開せず、`frontend` のプロキシ（`next.config.ts` の rewrites）経由で使います。検索の DB は compose の `db`（ローカルの Postgres。空で始まる）で、`.env` の `DATABASE_URL` は使いません。
実キーで動かすには資料の取り込みが要ります（[docs/development.md](docs/development.md) の「ローカルの db」）。
`curl` で API を直接確かめるときは、`docker compose -f docker-compose.yml -f docker-compose.api-port.yml up -d` で 8000 番を `127.0.0.1` に公開します（`/` は 404 です。画面は配信しません）。
片方だけ再起動するには `docker compose restart frontend`（または `api`）。ログは `docker compose logs -f frontend`。変更通知が届かずホットリロードが効かないときは、`docker-compose.yml` の `WATCHPACK_POLLING` のコメントを外します。

### 画面を手元で開発する

```bash
cd frontend
npm ci
cp .env.example .env.local   # 初回だけ
npm run dev                  # 開発サーバー（http://localhost:3000）。/chat（/chat/stream を含む）・/health・/documents・/threads は localhost:8000 へ転送
```

画面の詳細は [frontend/README.md](frontend/README.md) を参照してください。

## 開発環境の方針

- **LiteLLM gateway is removed and no longer used.** LiteLLM Proxy・Bifrost・claude-code-router は採用しません（`.devcontainer/litellm_proxy` は削除済み。経緯は [docs/target-architecture.md](docs/target-architecture.md)）。
  LLM の遅延・失敗への対処は、アプリの内側（`app/agents/resilience.py`）にあります。
- **Gemini CLI is not part of the docunavi runtime.** 開発コンテナにも入れません（`devcontainer.json` と `docker-compose.yml` から、導入・ボリューム・`GOOGLE_CLOUD_*`・`GEMINI_MODEL` を外した）。開発時の AI 支援は Claude Code（VS Code 拡張）です。
- **GCP の操作は、原則 gcloud CLI で行います。** Cloud Code・Gemini Code Assist・Data Agent Kit・Jupyter 系の VS Code 拡張は導入しません（`devcontainer.json` の `extensions` に足さない）。ガードの手順は [.devcontainer/README.md](.devcontainer/README.md)。
- **画像生成の Cloud Run Job は、星庭ジョブ（`hoshiniwa-job`）のリポジトリで管理します。** このリポジトリに `job/` は置きません。
- **星庭ジョブのリポジトリは、開発コンテナに bind mount で載せています**（作業ツリーは読み取り専用、`.git` だけ書き込み可。`.devcontainer/docker-compose.yml`）。
  このマウントは、ホストの `/Users/wataru/projects/hoshiniwa-job` があることが前提です。無い環境では、そのマウント 4 件を外すと、このリポジトリだけで開発コンテナを作れます。
  それ以外の開発コンテナの定義は、このリポジトリのファイルだけで完結します。

## 設定

`GEMINI_API_KEY`（または Vertex AI）が未設定ならモックモードで動きます（資料を検索せず、固定の返答を返す。画面と API の動作確認用）。
本物の LLM では、資料の検索に `DATABASE_URL` も要ります（DB に資料を取り込み・埋め込み済みであること。[docs/configuration.md](docs/configuration.md) の「資料の検索」）。

| 環境変数 | 説明 |
|---|---|
| `GEMINI_API_KEY` | Gemini API キー。空または `your_api_key_here` のときはモックモード |
| `GOOGLE_GENAI_USE_VERTEXAI` | `true` で Vertex AI を使う。キー不要で ADC（Cloud Run ならサービスアカウント）で認証する |
| `DATABASE_URL` | Postgres の接続 URL。本物の LLM では必須（資料の検索）で、会話履歴の保存先も兼ねる（モックモードで空ならプロセス内メモリ）。パスワードを含むのでコミットしない。詳細は [docs/configuration.md](docs/configuration.md) |

モデル、主モデルが落ちたときの別モデルへのフォールバック（`GEMINI_FALLBACK_MODEL`。既定は無効）、CORS、LLM のタイムアウトや再試行などの全一覧は [docs/configuration.md](docs/configuration.md) にあります。

## API

```bash
curl -s localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"message": "ザーハードの大気について教えて"}'
# {"reply": "…", "thread_id": "…", "context": "…", "mode": "mock"}

# 回答を少しずつ受け取る（画面はこちら。AI SDK の UI message stream の SSE）
curl -sN localhost:8000/chat/stream -H 'Content-Type: application/json' \
  -d '{"message": "ザーハードの大気について教えて"}'
```

同じ `thread_id` を送ると会話が続きます（`/chat` と `/chat/stream` は同じ履歴を使います）。仕様とエラーは [docs/api.md](docs/api.md)、起動後は <http://localhost:8000/docs> でも確認できます。

## 開発

```bash
pip install -r requirements-dev.txt
pytest                          # モックモード固定。実 API は呼びません
cd frontend && npm test    # フロントエンド（Vitest）
```

テストの方針、CI、依存の更新手順は [docs/development.md](docs/development.md) にあります。

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | 処理の流れ、ディレクトリ構成、環境構成（図）、配信の仕組み |
| [docs/guardrails.md](docs/guardrails.md) | 入出力ガードレール（拒否の条件、ストリーミング中の逐次検査） |
| [docs/configuration.md](docs/configuration.md) | 環境変数、LLM の遅延・失敗・切断への対処 |
| [docs/api.md](docs/api.md) | `/health`・`/chat`・`/chat/stream` の仕様、エラー |
| [docs/development.md](docs/development.md) | テスト、CI、依存の更新 |
| [docs/deploy.md](docs/deploy.md) | Cloud Run へのデプロイ手順と本番運用の記録 |
| [docs/roadmap.md](docs/roadmap.md) | これからの拡張計画（DB、認証、Cloudflare、R2、Vercel）と、先に要る変更 |
| [docs/decisions/](docs/decisions/README.md) | 重要な決定と、その理由（1 件 1 ファイル） |
| [.devcontainer/README.md](.devcontainer/README.md) | 開発コンテナの gcloud・gh のログインとガード |
| [CLAUDE.md](CLAUDE.md) | エージェント（Claude Code）が守ること |

## ライセンス

[LICENSE](LICENSE) を参照してください。
