# ザーハード設定資料チャットボット（FastAPI × LangGraph × Gemini）

架空の惑星「ザーハード」の設定資料（`storage/chunk/*.txt`）を根拠に答える RAG チャットボットのバックエンドです。

- **FastAPI**: HTTP API（`/chat`, `/health`）
- **LangGraph**: 入力ガード → エージェント（ツール呼び出しのループ）→ 出力ガード
- **Gemini**（`gemini-3.1-flash-lite`）: 回答生成。Gemini API キーまたは Vertex AI（ADC）で認証し、どちらも未設定ならモックモードで動く
- **RAG**: 設定資料を文単位に分けて BM25 で検索（外部の埋め込み API・ベクトル DB は不使用）

## 起動

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        # GEMINI_API_KEY を設定（未設定ならモックモード）
uvicorn app.main:app --reload
```

Docker Compose を使う場合は、ホスト側で `docker compose up --build` を実行します（`.env` が無くても起動し、その場合はモックモードです）。

| 環境変数 | 既定値 | 説明 |
|---|---|---|
| `GEMINI_API_KEY` | （空） | 空または `your_api_key_here` のときはモックモード |
| `GOOGLE_GENAI_USE_VERTEXAI` | （空） | `true` で Vertex AI を使う。キー不要で ADC（Cloud Run ならサービスアカウント）で認証し、キーの有無にかかわらずモックにならない |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | （空） | Vertex AI 利用時のプロジェクトとロケーション。未設定なら SDK が ADC やメタデータサーバーから解決する |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | モデル名の上書き（Vertex AI はリージョンごとに提供モデルが違う） |
| `CORS_ORIGINS` | `*` | 許可するオリジン（カンマ区切り） |
| `LLM_TIMEOUT_SECONDS` | `60` | 1 回の `/chat` で LLM を待つ上限秒数。不正値は既定値に戻る |

## API

### `GET /health`

```json
{"status": "ok", "mode": "mock"}
```

`mode` は `mock`（ダミー応答）または `gemini`（本物の LLM。Vertex AI の場合も `gemini`）。

### `POST /chat`

```json
{"message": "ザーハードの大気について教えて", "thread_id": "optional-conversation-id"}
```

| フィールド | 条件 |
|---|---|
| `message` | 必須。前後の空白を除いて 1〜2000 文字 |
| `thread_id` | 省略可。`[A-Za-z0-9_-]` で 128 文字以内。省略すると UUID が採番される。同じ値を送ると会話が続く |

レスポンス:

```json
{
  "reply": "…（出典: ザーハード1.txt）",
  "thread_id": "…",
  "context": "[ザーハード1.txt]\n…",
  "mode": "mock"
}
```

`context` は今回のターンで検索された設定資料（検索しなかった・該当なしの場合は空）です。

エラー（`detail` は固定の日本語メッセージで、例外の内容は含まれません。詳細はサーバーログのみ）:

| ステータス | 原因 |
|---|---|
| 422 | 入力検証エラー（空のメッセージ、長すぎる `message`、不正な `thread_id` など） |
| 429 | LLM のレート制限。上流が待ち時間を返した場合は `Retry-After` ヘッダ付き |
| 504 | LLM の応答がタイムアウト |
| 500 | API キー・モデル設定の誤り、想定外の例外 |
| 502 | その他の LLM 呼び出し失敗 |

## ガードレール

ルールベース（LLM を使わない）の入出力チェックです。`app/agents/guardrails.py` にあります。

- **入力**: 指示の上書き、システムプロンプトの開示要求、役割の乗っ取り、チャットテンプレートの偽装を検出すると、LLM とツールを呼ばずに定型文で拒否します。拒否したターンの発言は履歴から伏せ字に置き換えます（NFKC 正規化と不可視文字の除去、空白・記号の挿入に対応）。
- **出力**: 回答が `SYSTEM_PROMPT` をほぼそのまま含んでいる場合は定型文に置き換えます。

これは典型的な手口を安く弾くためのもので、プロンプトインジェクションを完全に防ぐものではありません。

## テスト

```bash
pip install -r requirements-dev.txt
pytest
```

`tests/conftest.py` が `GEMINI_API_KEY` を空にしてからアプリを読み込むため、`.env` に実キーがあってもテストは常にモックモードで動き、Gemini API を呼びません。

RAG の精度は `tests/rag_eval_cases.py` の評価セット（関連 21 件・無関係 8 件）で回帰テストしています。2-gram 照合では拾えない言い換え（「気温差」→「温度差」など）は既知の取りこぼしです。

## 構成

```
app/
  main.py            FastAPI アプリ（CORS、例外ハンドラ）
  api/routes.py      /health, /chat、入力検証、LLM エラーの分類
  agents/graph.py    LangGraph のグラフ、SYSTEM_PROMPT、モック LLM
  agents/guardrails.py  入出力ガードレール
  agents/tools.py    設定資料の検索（search_zahard_lore）
storage/chunk/       設定資料（RAG の検索対象）
tests/               pytest（モックモード固定）
```

会話履歴は `thread_id` ごとにプロセス内メモリ（`MemorySaver`）へ保持するため、再起動で消え、複数ワーカー間では共有されません。
