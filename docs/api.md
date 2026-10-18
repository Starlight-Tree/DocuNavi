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
| `thread_id` | 省略可。`[A-Za-z0-9_-]` で 128 文字以内。省略すると UUID が採番される。同じ値を送ると会話が続く（`AUTH_MODE=required` では、同じ利用者の中だけ） |

`AUTH_MODE=required` のときは `Authorization: Bearer <アクセストークン>` が必要です（`/chat/stream` も同じ。[configuration.md](configuration.md) の「ログイン（認証）」）。
トークンが無い・不正・期限切れなら `401`（`{"detail": "ログインが必要です。ログインし直してください。"}`、`WWW-Authenticate: Bearer`）、
認証サービス（JWKS）に届かず検証できないときは `503`（固定メッセージ）です。`/health` は認証なしです。

レスポンス:

```json
{
  "reply": "…（出典: ザーハード1.pdf）",
  "thread_id": "…",
  "context": "[ザーハード1.pdf]\n…",
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

## `POST /chat/stream`

`/chat` と同じ入力で、回答を少しずつ返します（画面はこちらを使います）。`/chat` は従来どおりで、同じ会話の履歴を共有します。

```json
{"message": "ザーハードの大気について教えて", "thread_id": "optional-conversation-id"}
```

入力の条件（`message`・`thread_id`）と、422 を含む入力検証は `/chat` と同じです。

レスポンスは `text/event-stream` で、[AI SDK の UI message stream](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol)
（ヘッダー `x-vercel-ai-ui-message-stream: v1`）です。1 チャンクは `data: <JSON>` の行と空行で、最後に `data: [DONE]` が来ます。
`@ai-sdk/react` の `useChat` がそのまま読めます。

```text
data: {"type":"start","messageId":"…","messageMetadata":{"thread_id":"…","mode":"gemini"}}
data: {"type":"text-start","id":"text-0"}
data: {"type":"text-delta","id":"text-0","delta":"ザーハードの大気は…"}
data: {"type":"text-end","id":"text-0"}
data: {"type":"message-metadata","messageMetadata":{"context":"[ザーハード1.pdf]\n…"}}
data: {"type":"finish"}
data: [DONE]
```

- **`start` の `messageMetadata`**: `thread_id`（新規なら採番された値）と `mode`。
- **本文は「最後のテキスト部分」です。** ツール呼び出しの前置きがあれば、テキスト部分が複数になります。入力ガードが拒否したとき・出力ガードが
  回答を定型文に置き換えたときは、最後に正しい本文が新しいテキスト部分として届きます（先に届いた部分は無視してよい）。
  最後のテキスト部分は、`/chat` の `reply` と同じ文です。
- **`message-metadata` の `context`**: `/chat` の `context` と同じ（今回のターンで検索された設定資料。完了の直前に届く）。
- **出力ガードの保持:** 本文は、末尾の正規化後 31 文字ぶんを手元に残して送ります（システムプロンプトの連続一致が
  24 文字に達する前に止めるため）。短い回答は、終わりにまとめて届きます。ガードが漏洩を検知したら、受信をやめて定型文を送ります。
  検知の前に届く本文は、漏洩とみなされる長さに達しません。
- **無通信の間:** 10 秒ごとに SSE のコメント行（`: ping`）が届きます。クライアントは無視してください。

エラーは、**最初の本文が届く前か後か**で伝え方が変わります。

| 時点 | 伝え方 |
|---|---|
| 最初の本文が届く前（LLM が最初のトークンを返す前、入力検証、履歴の保存先への接続など） | `/chat` と同じ HTTP ステータス（422・429・500・502・503・504）と固定の `detail`。429 には `Retry-After` が付く場合がある |
| 本文を送り始めた後 | ステータスは 200 のまま、`{"type":"error","errorText":"…"}` を送って `[DONE]` で終わる。`errorText` は `/chat` の `detail` と同じ固定の日本語メッセージ（例外の内容は含まれない） |

クライアントが切断した場合は、LLM 呼び出しを取り消して何も返しません（`/chat` と同じ。[configuration.md](configuration.md) の「クライアントの切断」）。

## `GET /documents`、`GET /documents/{id}/chunks`、`GET /documents/{id}/file`

資料の一覧（読み取り専用）。**DB（`documents` / `chunks`）に保存された内容**を返します。BM25 検索はこの DB を読まず、起動時に
`storage/pdf/` の PDF を読み直すので、ここで見える本文は、BM25 が採点する本文と同じとは限りません（ベクトル検索
`SEARCH_BACKEND=vector` だけがこの DB を読みます）。

```json
{"configured": true, "documents": [{"id": 1, "source": "tsubasa_dorm_rules.pdf",
  "title": "つばさ大学学生寮 入寮規程", "status": "ready", "failure_reason": null,
  "pages": 3, "updated_at": "2026-10-18T09:00:00Z", "chunk_count": 3, "embedded_count": 3}]}
```

- **`title`:** PDF のメタデータ `/Title`（取り込み時に保存）。無い・空白だけなら `null` で、画面はファイル名（`source`）で代えます。
  `source` はファイル名のままで、題名とは別に返します。
- **`GET /documents`:** 文書ごとの情報とチャンク数、埋め込み済みの数。`configured` が `false` のときは、`DATABASE_URL` が空で資料の DB を
  使っていません（エラーではなく 200。`documents` は空）。`configured: true` で `documents` が空なら、DB はあるが文書が 0 件です。
- **`GET /documents/{id}/chunks`:** その文書のチャンクを `chunk_index` 順に、`page`・`page_end`・`kind`・`heading`・`content`（全文）・
  `embedded`（埋め込みがあるか）・`embedding_model` で返します。**埋め込みのベクトルそのものは返しません。**
  存在しない `id` は 404、`id` が 1 以上の整数でなければ 422。応答の `original_pdf`（真偽値）は、元の PDF を `/file` で返せるか。
- **`GET /documents/{id}/file`:** 取り込んだ元の PDF（`application/pdf`、`Content-Disposition: inline`。日本語のファイル名は `filename*=UTF-8''…`）。
  DB の `source`（ファイル名）と `content_hash`（取り込んだ PDF の sha256）に合うものだけを、`storage/pdf/<資料群>/` の直下から探して返す。
  **名前だけでは選ばない**（同じ名前で中身が違う PDF は返さない）。返すのは、ハッシュを確かめたバイト列そのもの。
  `source` はファイル名としてだけ扱い（`/` `\` `..` を含むものは探さない）、シンボリックリンクは、行き先が置き場の中でも使わない。
  20 MiB を超えるファイルは返さない。
  - 存在しない `id` は 404。ファイルが無い・探せないときは 404（`元の PDF は利用できません。`）。
    名前の合うファイルはあるが内容が DB と違う（または DB にハッシュが無い）ときは 409（`元の PDF が、DB に保存された資料と一致しません。`）。
  - 画面は、認証付きの fetch で取った PDF を Blob URL にして、ブラウザー内蔵の PDF 表示（iframe）で見せる。
    PC のブラウザーだけを確かめている（スマホ・iOS は未確認）。
- **認証:** `AUTH_MODE=required` のときは `/chat` と同じ（`Authorization: Bearer`、失敗は固定メッセージの 401）。資料ごとの閲覧権限はまだ無く、
  ログインした人は全員が全文を読めます。**`AUTH_MODE=off` は自分だけが使うローカル開発に限り、他の人が届く環境では `required` にしてください。**
- **エラー:** DB を読めないときは固定メッセージの 503（例外の内容・接続先は含まない）。`/chunks` で DB が未設定のときも 503。
- 応答には `Cache-Control: no-store` が付きます。書き込みの API はありません（POST などは 405）。

