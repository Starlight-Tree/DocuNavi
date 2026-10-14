# API

サーバー起動後は、自動生成の対話的なドキュメントを <http://localhost:8000/docs> で開けます（OpenAPI は `/openapi.json`）。
ここには、そこに出ないエラーの意味や挙動の補足をまとめています。

## `GET /health`

```json
{"status": "ok", "mode": "mock", "history": "memory"}
```

- `mode`: `mock`（ダミー応答）または `gemini`（本物の LLM。Vertex AI の場合も `gemini`）。
- `history`: 会話履歴の置き場所。`memory`（プロセス内。再起動で消える）または `postgres`（`DATABASE_URL` の DB）。
  DB には問い合わせないので、DB の障害では値も応答も変わりません（[configuration.md](configuration.md) の「会話履歴の保存先」）。

## `POST /chat`

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
| 503 | 会話履歴の保存先（Postgres）に接続できない。`DATABASE_URL` を設定したときだけ起こる |

クライアントが切断した場合は、LLM 呼び出しを取り消して何も返しません（ステータスはクライアントに届かないため、上の表にはありません）。
