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

### 画面（フロントエンド）

```bash
cd frontend
npm ci
npm run build        # frontend/dist を作る。FastAPI が / で配信する
npm run dev          # 開発サーバー（http://localhost:5173）。/chat と /health は localhost:8000 へ転送
```

- `frontend/dist` は Git 管理外です。無い状態でも API は動き、`/` はビルド方法を示す 404 を返します（起動後にビルドしても再起動は不要）。
- 配信の仕組み: どのルートにも当たらなかった `GET`/`HEAD` だけが画面側に回り、拡張子のないパスは `index.html`（SPA のルーティング）、
  無いファイル（`/assets` 配下や拡張子付き）は 404 になります。`/chat`・`/health`・`/docs` などの API は影響を受けません。
- 詳細は [frontend/README.md](frontend/README.md)。

| 環境変数 | 既定値 | 説明 |
|---|---|---|
| `GEMINI_API_KEY` | （空） | 空または `your_api_key_here` のときはモックモード |
| `GOOGLE_GENAI_USE_VERTEXAI` | （空） | `true` で Vertex AI を使う。キー不要で ADC（Cloud Run ならサービスアカウント）で認証し、キーの有無にかかわらずモックにならない |
| `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | （空） | Vertex AI 利用時のプロジェクトとロケーション。未設定なら SDK が ADC やメタデータサーバーから解決する |
| `GEMINI_MODEL` | `gemini-3.1-flash-lite` | モデル名の上書き（Vertex AI はリージョンごとに提供モデルが違う） |
| `CORS_ORIGINS` | `*` | 許可するオリジン（カンマ区切り） |
| `LLM_TIMEOUT_SECONDS` | `60` | 1 回の `/chat` で LLM を待つ上限秒数（ヘッジ・再試行を含む全体）。不正値は既定値に戻る |
| `LLM_HEDGE_DELAY_SECONDS` | `8` | LLM がこの秒数たっても応答しなければ、追加の試行を投げる（下記）。`0` でヘッジを無効にする。小さくしすぎると、毎回 `LLM_MAX_ATTEMPTS` 本をほぼ同時に投げる。不正値（数値でない・負）は既定値に戻る |
| `LLM_ATTEMPT_TIMEOUT_SECONDS` | `30`（ヘッジなしは `15`） | LLM への 1 回の試行を待つ上限秒数（接続・送信・受信の段階ごとの上限）。不正値は既定値に戻る |
| `LLM_MAX_ATTEMPTS` | `3` | 1 回の LLM 呼び出しで投げる試行の最大数（最初の 1 回を含む）。`1` で追加・再試行なし。不正値は既定値に戻る |

### LLM の遅延・失敗への対処

2026-10 に開発環境から観測した範囲では、Gemini（特に `gemini-3.1-flash-lite`）は、1〜3 秒で返る回と、10 秒を超える回・応答がないまま 60 秒でサーバー側に打ち切られる回が混在していました。
待ち続けると 1 回の遅延で `/chat` が 504 になるため、遅い呼び出しは待たずに**同じ要求をもう一度投げて、最初に成功したものを採ります**（ヘッジ）。

- `LLM_HEDGE_DELAY_SECONDS` 秒たっても応答がなければ、追加の試行を投げる（`LLM_MAX_ATTEMPTS` 本まで）。成功したら残りは取り消す。
- 試行が失敗（無応答・接続の失敗・408/500/502/503/504）したときは、0.5 秒後に次を投げる。ほかの試行が結果待ちなら、その結果か次のヘッジの時刻まで待つ。認証や不正なリクエストなど、投げ直しても同じ失敗は即座に返す。
- 429（レート制限）を受けたら、追加を止めて間隔（2 秒、4 秒…）を空けて再試行する。全て失敗したときは 429 を返す（上流が待ち時間を示していれば `Retry-After` が付く）。
- 追加・再試行が起きるとアプリのログに `WARNING` が出る（`app.agents.resilience`）。頻発するなら上流（Gemini）が不安定。

実 API で同じ時間帯に交互に流した比較（各 12 回、Gemini が不安定だった時間帯）では、成功率はヘッジなし 5/12、ヘッジあり 8/12 でした。小さな標本なので目安です。

**コストの注意:** 遅い呼び出しでは、1 回の LLM 呼び出しに最大 `LLM_MAX_ATTEMPTS` 本のリクエストが投げられ、その分だけ入力トークンが増えます
（取り消した試行の課金・クォータの扱いは Gemini 側の仕様次第です）。速く返っているときは 1 本のままです。
抑えたいときは `LLM_MAX_ATTEMPTS` を下げるか、`LLM_HEDGE_DELAY_SECONDS=0` で無効にします（その場合は SDK が最大 `LLM_MAX_ATTEMPTS` 回（最初の 1 回を含む）を順に試し、試行の上限は短い `15` 秒になります）。

**容量の注意:** Gemini が全体に遅いときは、1 呼び出しあたり最大 `LLM_MAX_ATTEMPTS` 本が同時に飛びます。SDK の HTTP 接続プール（既定 100 接続）を超えると、追加の試行が待たされて効果が薄れます。
同時に処理する `/chat` の数 × `LLM_MAX_ATTEMPTS` が 100 を大きく超える環境（Cloud Run の同時実行数を上げるときなど）では、確認してください。
また、クライアントが切断しても、処理中の LLM 呼び出しは成功するか `LLM_TIMEOUT_SECONDS` に達するまで続きます。

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

フロントエンドは Vitest です（`cd frontend && npm test`）。

`tests/conftest.py` が `GEMINI_API_KEY` を空にしてからアプリを読み込むため、`.env` に実キーがあってもテストは常にモックモードで動き、Gemini API を呼びません。

RAG の精度は `tests/rag_eval_cases.py` の評価セット（関連 21 件・無関係 8 件）で回帰テストしています。2-gram 照合では拾えない言い換え（「気温差」→「温度差」など）は既知の取りこぼしです。

## 構成

```
app/
  main.py            FastAPI アプリ（CORS、例外ハンドラ）
  api/routes.py      /health, /chat、入力検証、LLM エラーの分類
  frontend.py        frontend/dist の配信と SPA フォールバック
  agents/graph.py    LangGraph のグラフ、SYSTEM_PROMPT、モック LLM
  agents/guardrails.py  入出力ガードレール
  agents/resilience.py  LLM 呼び出しのヘッジ付き再試行
  agents/tools.py    設定資料の検索（search_zahard_lore）
frontend/            チャット画面（Vite + React + TypeScript）。dist/ がビルド成果物
storage/chunk/       設定資料（RAG の検索対象）
tests/               pytest（モックモード固定）
```

会話履歴は `thread_id` ごとにプロセス内メモリ（`MemorySaver`）へ保持するため、再起動で消え、複数ワーカー間では共有されません。
