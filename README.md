# ザーハード設定資料チャットボット（FastAPI × LangGraph × Gemini）

架空の惑星「ザーハード」の設定資料（`storage/chunk/*.txt`）を根拠に答える RAG チャットボットです。
バックエンド（FastAPI）に加えて、チャット画面（Vite + React + TypeScript、`frontend/`）を同じサーバーから配信します。

- **FastAPI**: HTTP API（`/chat`, `/health`）
- **LangGraph**: 入力ガード → エージェント（ツール呼び出しのループ）→ 出力ガード
- **Gemini**（`gemini-3.1-flash-lite`）: 回答生成。Gemini API キーまたは Vertex AI（ADC）で認証し、どちらも未設定ならモックモードで動く
- **RAG**: 設定資料を文単位に分けて BM25 で検索（外部の埋め込み API・ベクトル DB は不使用）
- **画面**: `frontend/`（Vite + React + TypeScript）。ビルド成果物 `frontend/dist` を FastAPI が `/` で配信する

## 起動

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # GEMINI_API_KEY を設定（未設定ならモックモード）
uvicorn app.main:app --reload
```

Docker Compose を使う場合は、ホスト側で `docker compose up --build` を実行します（`.env` が無くても起動し、その場合はモックモードです）。
イメージのビルド時にフロントエンドもビルドされるので、起動後は <http://localhost:8000> で画面を開けます。

画面を手元でビルド・開発するとき:

```bash
cd frontend
npm ci
npm run build        # frontend/dist を作る。FastAPI が / で配信する
npm run dev          # 開発サーバー（http://localhost:5173）。/chat と /health は localhost:8000 へ転送
```

`frontend/dist` は Git 管理外です。無い状態でも API は動き、`/` はビルド方法を示す 404 を返します（起動後にビルドしても再起動は不要）。
画面の詳細は [frontend/README.md](frontend/README.md) を参照してください。

## 設定

最低限は次の 2 つです。どちらも未設定ならモックモードで動きます。会話履歴を再起動後も残したいときだけ、`DATABASE_URL` を足します。

| 環境変数 | 説明 |
|---|---|
| `GEMINI_API_KEY` | Gemini API キー。空または `your_api_key_here` のときはモックモード |
| `GOOGLE_GENAI_USE_VERTEXAI` | `true` で Vertex AI を使う。キー不要で ADC（Cloud Run ならサービスアカウント）で認証する |
| `DATABASE_URL` | 会話履歴の保存先（Postgres の接続 URL）。空ならプロセス内メモリで、再起動で消える。パスワードを含むのでコミットしない。詳細は [docs/configuration.md](docs/configuration.md) の「会話履歴の保存先」 |

モデル、CORS、LLM のタイムアウトや再試行などの全一覧は [docs/configuration.md](docs/configuration.md) にあります。

## API

```bash
curl -s localhost:8000/chat -H 'Content-Type: application/json' \
  -d '{"message": "ザーハードの大気について教えて"}'
# {"reply": "…", "thread_id": "…", "context": "…", "mode": "mock"}
```

同じ `thread_id` を送ると会話が続きます。仕様とエラーは [docs/api.md](docs/api.md)、起動後は <http://localhost:8000/docs> でも確認できます。

## 開発

```bash
pip install -r requirements-dev.txt
pytest                          # モックモード固定。実 API は呼びません
cd frontend && npm test         # フロントエンド（Vitest）
```

テストの方針、CI、依存の更新手順は [docs/development.md](docs/development.md) にあります。

## ドキュメント

| 文書 | 内容 |
|---|---|
| [docs/architecture.md](docs/architecture.md) | グラフの流れ、ディレクトリ構成、配信の仕組み、ガードレール |
| [docs/configuration.md](docs/configuration.md) | 環境変数、LLM の遅延・失敗・切断への対処 |
| [docs/api.md](docs/api.md) | `/health` と `/chat` の仕様、エラー |
| [docs/development.md](docs/development.md) | テスト、CI、依存の更新 |
| [docs/deploy.md](docs/deploy.md) | Cloud Run へのデプロイ手順（未実行の案） |
| [docs/roadmap.md](docs/roadmap.md) | これからの拡張計画（DB、認証、Cloudflare、R2、Vercel）と、先に要る変更 |
| [.devcontainer/README.md](.devcontainer/README.md) | 開発コンテナの gcloud・gh のログインとガード |
| [CLAUDE.md](CLAUDE.md) | エージェント（Claude Code）が守ること |

## 開発環境の gcloud（ログインとガード）

開発コンテナの gcloud のログイン手順と、それを守る層の説明は [.devcontainer/README.md](.devcontainer/README.md) に移しました。

## 開発環境の GitHub CLI（gh）

開発コンテナの gh のトークンの作り方と、それを守る層の説明は [.devcontainer/README.md](.devcontainer/README.md) に移しました。

## ライセンス

[LICENSE](LICENSE) を参照してください。
