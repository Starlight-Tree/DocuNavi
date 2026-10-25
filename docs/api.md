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
| `collection` | 省略可。検索対象の資料のグループ（`documents.collection` の識別名。`[A-Za-z0-9_-]` で 1〜40 文字）。空文字は 422。下の「検索対象」を参照 |

### 検索対象（`collection`）

- **省略:** 新しい会話は「すべて」の資料を探す（従来と同じ）。続きの会話は、その会話の検索対象のまま。
- **会話ごとに固定:** 検索対象は会話の最初の発言で決まり、会話の状態（チェックポイント）に残る。続きの会話に違う `collection` を送ると **409** になる
  （検索も LLM も動かない）。`detail` は、会話の検索対象と送られた検索対象の両方を含む固定文
  （例: 「この会話の検索対象は「zahard」です。送られた検索対象は「cafe」で、会話の途中では変えられません。…」）。
  「すべて」は `collection` が `null`（省略）の状態で、`detail` では「すべて」と表示する。
- **古い会話:** 検索対象の項目が無い会話（この機能より前のもの）は「すべて」として扱う。省略すれば全資料を探し、`collection` を送ると 409。
- **既存の会話を開いたとき:** 検索対象は履歴の状態から復元される。`GET /threads/{thread_id}/messages` の `collection` で分かる。
- **識別名の検証:** 形（上の条件）が合わなければ 422。登録の有無は見ない（資料の無いグループは、エラーではなく 0 件になる）。
- **LLM は変えられない:** 検索対象は、LLM が渡すツールの引数ではなく、会話の状態から検索へ渡す（LLM に見えるツールの引数は `query` だけ）。

`AUTH_MODE=required` のときは `Authorization: Bearer <アクセストークン>` が必要です（`/chat/stream` も同じ。[configuration.md](configuration.md) の「ログイン（認証）」）。
トークンが無い・不正・期限切れなら `401`（`{"detail": "ログインが必要です。ログインし直してください。"}`、`WWW-Authenticate: Bearer`）、
認証サービス（JWKS）に届かず検証できないときは `503`（固定メッセージ）です。`/health` は認証なしです。

レスポンス:

```json
{
  "reply": "…",
  "thread_id": "…",
  "context": "[ザーハード1.pdf]\n…",
  "mode": "mock",
  "collection": null,
  "hit_count": 2
}
```

`collection` はこの会話の検索対象（`null` は「すべて」）、`hit_count` は今回のターンの検索で（類似度のしきい値を超えて）拾ったチャンクの数です。
検索しなかったターン（モックモード・入力ガードの拒否・検索が要らない質問）は `null` で、検索して 0 件だった `0` とは別です。画面は「該当する資料がありません」を、
`collection` の指定があって `hit_count` が `0` のときだけ出します（検索の失敗は `hit_count` ではなく、下のエラーで伝わります）。

`context` は今回のターンで検索された設定資料（検索しなかった・該当なしの場合は空）です。`mode` が `mock` のとき（`GEMINI_API_KEY` 未設定）は、
資料を検索せず、`reply` は固定の返答で、`context` は空です。

エラー（`detail` は固定の日本語メッセージで、例外の内容は含まれません。詳細はサーバーログのみ）:

| ステータス | 原因 |
|---|---|
| 422 | 入力検証エラー（空のメッセージ、長すぎる `message`、不正な `thread_id`・`collection` など） |
| 409 | 続きの会話に、会話の検索対象と違う `collection` を送った（検索対象は会話ごとに固定）。`detail` に両方の検索対象を載せる |
| 429 | LLM のレート制限。上流が待ち時間を返した場合は `Retry-After` ヘッダ付き |
| 504 | LLM の応答がタイムアウト |
| 500 | API キー・モデル設定の誤り、想定外の例外 |
| 502 | その他の LLM 呼び出し失敗 |
| 503 | 会話履歴の保存先（Postgres）に接続できない（`DATABASE_URL` を設定したときだけ）、または資料の検索（質問の埋め込み・DB のベクトル検索）に失敗した（「資料の検索に失敗しました。…」。別の検索経路には戻らない） |

クライアントが切断した場合は、LLM 呼び出しを取り消して何も返しません（ステータスはクライアントに届かないため、上の表にはありません）。

## `POST /chat/stream`

`/chat` と同じ入力で、回答を少しずつ返します（画面はこちらを使います）。`/chat` は従来どおりで、同じ会話の履歴を共有します。

```json
{"message": "ザーハードの大気について教えて", "thread_id": "optional-conversation-id"}
```

入力の条件（`message`・`thread_id`・`collection`）と、422・409 を含む入力検証は `/chat` と同じです。409 は、レスポンスを始める前に HTTP で返します。

レスポンスは `text/event-stream` で、[AI SDK の UI message stream](https://ai-sdk.dev/docs/ai-sdk-ui/stream-protocol)
（ヘッダー `x-vercel-ai-ui-message-stream: v1`）です。1 チャンクは `data: <JSON>` の行と空行で、最後に `data: [DONE]` が来ます。
`@ai-sdk/react` の `useChat` がそのまま読めます。

```text
data: {"type":"start","messageId":"…","messageMetadata":{"thread_id":"…","mode":"gemini"}}
data: {"type":"data-status","data":{"phase":"analyzing"},"transient":true}
data: {"type":"data-status","data":{"phase":"searching"},"transient":true}
data: {"type":"data-status","data":{"phase":"generating"},"transient":true}
data: {"type":"text-start","id":"text-0"}
data: {"type":"text-delta","id":"text-0","delta":"ザーハードの大気は…"}
data: {"type":"text-end","id":"text-0"}
data: {"type":"message-metadata","messageMetadata":{"context":"[ザーハード1.pdf]\n…","sources":[{"name":"ザーハード1.pdf","page":1,"page_end":null,"weak":false,"collection":"zahard"}],"collection":null,"hit_count":1}}
data: {"type":"finish"}
data: [DONE]
```

- **`start` の `messageMetadata`**: `thread_id`（新規なら採番された値）と `mode`。
- **`data-status`（`transient: true`）**: 本文より前に届く、いま何をしているかの合図。`phase` は `analyzing`（質問を読み、検索が要るかを LLM が決めている）、
  `searching`（資料の検索）、`generating`（検索の結果を受けて、回答を作っている）。検索しない質問は `analyzing` だけ。入力ガードが拒否した質問には出ない。
  transient なので、履歴にも会話の発言にも入らない。画面は、待機中の吹き出しの文言に使う。知らない `phase` は無視してよい。
- **本文は「最後のテキスト部分」です。** ツール呼び出しの前置きがあれば、テキスト部分が複数になります。入力ガードが拒否したとき・出力ガードが
  回答を定型文に置き換えたときは、最後に正しい本文が新しいテキスト部分として届きます（先に届いた部分は無視してよい）。
  最後のテキスト部分は、`/chat` の `reply` と同じ文です。
- **`message-metadata` の `context`**: `/chat` の `context` と同じ（今回のターンで検索された設定資料。完了の直前に届く）。
- **`message-metadata` の `collection` と `hit_count`**: `/chat` と同じ意味（この会話の検索対象と、今回のターンの検索で拾ったチャンクの数。検索していなければ `hit_count` は `null`）。
- **`message-metadata` の `sources`**: `context` の出典の見出しと同じ順の `{name, page, page_end, weak, collection}`。`collection` はその資料のグループ（未設定なら `null`）。`page` は DB の `chunks.page`（PDF の 1 始まりのページ）、
  `page_end` はページをまたぐ表の最終ページ（またがなければ `null`）。値が不正なとき（1 未満・整数でない・`page_end` が `page` 以下）は `null`。
  各要素の `weak` は、同じ検索の 1 位との類似度の差が 0.05（`app/agents/tools.py` の `WEAK_GAP`）を超える 2 件目以降で `true`。画面は出典カードに出さない
  （LLM が受け取った抜粋は変えず、折りたたみの一覧には残る）。類似度が無いときと 1 件目は `false`。
  LLM に渡す文面（`context`）にはページを載せない（ツールの `artifact` で画面にだけ渡す）。検索しなかったターン・入力ガードが拒否したターンは空の配列。
  `/chat` の応答にはない。
- **出力ガードの保持:** 本文は、末尾の正規化後 31 文字ぶんを手元に残して送ります（システムプロンプトの連続一致が
  24 文字に達する前に止めるため）。短い回答は、終わりにまとめて届きます。ガードが漏洩を検知したら、受信をやめて定型文を送ります。
  検知の前に届く本文は、漏洩とみなされる長さに達しません。
- **無通信の間:** 10 秒ごとに SSE のコメント行（`: ping`）が届きます。クライアントは無視してください。

エラーは、**レスポンスを始める前か後か**で伝え方が変わります。レスポンスは、入力検証・認証・グラフの組み立てが済んだら、最初の本文を待たずに始めます
（最初の本文までに 20 秒以上かかることがあり、その間に `data-status` で段階を見せるため）。

| 時点 | 伝え方 |
|---|---|
| レスポンスを始める前（入力検証の 422、認証の 401、LLM クライアントの設定の失敗の 500 など） | `/chat` と同じ HTTP ステータスと固定の `detail`。429 には `Retry-After` が付く場合がある |
| レスポンスを始めた後（LLM の失敗・タイムアウト・資料の検索や履歴の保存先の失敗など。最初の本文の前後を問わない） | ステータスは 200 のまま、`{"type":"error","errorText":"…"}` を送って `[DONE]` で終わる。`errorText` は `/chat` の `detail` と同じ固定の日本語メッセージ（例外の内容は含まれない） |

`error` チャンクは `errorText` しか運べない（AI SDK のクライアントも、これだけを読む）ので、`/chat` が HTTP で返す 429・504・503・502・500 のステータスと
`Retry-After` は、`error` の直前の **`data-failure`**（`transient: true`）で伝えます。`data` は `{"status": 429, "retryAfterSeconds": 12}`
（`retryAfterSeconds` は 429 で待ち時間が分かるときだけ）。画面は、これを HTTP のときと同じ案内（「約 12 秒待ってから…」）に使います。

クライアントが切断した場合は、LLM 呼び出しを取り消して何も返しません（`/chat` と同じ。[configuration.md](configuration.md) の「クライアントの切断」）。

## `GET /threads`、`GET /threads/{thread_id}/messages`、`DELETE /threads/{thread_id}`

会話の一覧と、会話の削除。画面は、ログイン中のチャット画面の左サイドバーが使う（一覧・選んで続きから・削除。選ぶと `/messages` で以前のやり取りを取得して画面に並べる。ページの再読み込みだけでは取得しない）。履歴の保存先（Postgres またはメモリ）を、`/chat` と同じ checkpointer 経由で読み書きする。

- **`GET /threads`:** ログイン中の利用者の会話を、最後の更新が新しい順に、最大 50 件返す。

  ```json
  {"threads": [{"thread_id": "…", "title": "ザーハードの大気について…", "created_at": "2026-10-19T…+09:00", "updated_at": "…"}]}
  ```

  `thread_id` は `/chat` に送ると会話が続く値（利用者 ID の接頭辞は付かない）。`title` は最初のユーザー発言の先頭（空白をまとめて 40 文字まで。
  超えたら末尾が `…`）で、新しい列も LLM 呼び出しも使わない。`created_at` / `updated_at` は最初と最後のチェックポイントの時刻で、無ければ `null`。
- **`GET /threads/{thread_id}/messages`:** 会話の発言を古い順に返す。ユーザーの発言と、ツール呼び出しの無い最終回答だけで、検索で引いた資料の本文（ツールの結果）は含めない。
  本文が空のものは除く。履歴に無い会話は 404（required では他人の会話も同じ 404）。

  ```json
  {"messages": [{"role": "user", "text": "ザーハードの大気は？"}, {"role": "assistant", "text": "…"}], "collection": "zahard"}
  ```

  `collection` はこの会話の検索対象（`null` は「すべて」。検索対象の項目が無い古い会話も `null`）。続きの会話で `/chat` に送る値は、これと同じか省略にする（違うと 409）。

  回答の出典（`sources`）は、会話の状態に直近の 1 回分しか無い（毎ターン作り直す）ので返さない。
- **`DELETE /threads/{thread_id}`:** 会話を 1 件消す。成功は 204（本文なし）。`thread_id` の形式は `/chat` と同じ（`:` を含むと 422）。
- **認証:**

  | `AUTH_MODE` | `GET /threads` | `GET …/messages`、`DELETE /threads/{thread_id}` |
  |---|---|---|
  | `required` | 本人の会話だけ。トークンが無い・不正なら 401 | 本人の会話だけ。他人の ID は、存在しないのと同じ 404（区別しない） |
  | `off`（既定） | 404（匿名の会話には持ち主がなく、一覧にすると他人の会話が見えるため） | `thread_id` を知っていれば読める・消せる（`/chat` で続きを書ける条件と同じ） |

- **エラー:** 404（`指定された会話が見つかりません。` / off の一覧は `会話の一覧は、ログイン（AUTH_MODE=required）のときだけ使えます。`）、
  422（不正な `thread_id`）、503（履歴の保存先を操作できない。固定メッセージで、例外の内容・接続先は含まない）。
- **確認の状況:** 本物のログイン（Supabase・Google）での動作は、利用者が実機で確認済み（2026-10-19、利用者の報告。確認した範囲の詳細は記録していない）。
  テストは、メモリと、ローカルの Postgres 17 で行った（`tests/test_threads.py`、`tests/test_threads_postgres.py`）。
  **個別には未確認:** Supabase の実 DB での速度（題名のために会話ごとに履歴を読むので、最大 50 回の読み出しがある）、返答の生成中に同じ会話を削除したときの挙動。

## `GET /documents`、`GET /documents/{id}/chunks`、`GET /documents/{id}/file`

資料の一覧（読み取り専用）。**DB（`documents` / `chunks`）に保存された内容**を返します。チャットの検索（ベクトル検索）が読むのと同じ DB なので、
ここで見える本文が、検索の対象になる本文です。

```json
{"configured": true, "documents": [{"id": 1, "source": "tsubasa_dorm_rules.pdf",
  "title": "つばさ大学学生寮 入寮規程", "collection": "guides", "status": "ready", "failure_reason": null,
  "pages": 3, "updated_at": "2026-10-18T09:00:00Z", "chunk_count": 3, "embedded_count": 3}]}
```

- **`title`:** PDF のメタデータ `/Title`（取り込み時に保存）。無い・空白だけなら `null` で、画面はファイル名（`source`）で代えます。
  `source` はファイル名のままで、題名とは別に返します。
- **`collection`:** 資料群の名前（`storage/pdf/<名前>/` のディレクトリ名。`cafe` / `guides` / `zahard` など）。未設定、または形（英数字・`_`・`-`、40 文字まで）が
  合わない値は `null`。取り込み（`python -m app.ingestion`）が、入力のディレクトリ名（`--collection` で上書き）を保存します。
  日本語の表示名（設定資料・店舗案内・規程・取扱説明書）は画面が持ち、API は名前だけを返します。一覧の絞り込みも画面側で行います。
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

## `GET /collections`

資料グループ（`documents.collection` の値）の一覧。画面が、グループの表示名・説明・資料の数を出すために読む（読み取り専用）。

```json
{"configured": true, "collections": [{"name": "zahard", "label": "設定資料", "description": "ザーハードに関する設定資料です。",
  "sort_order": 10, "registered": true, "document_count": 3, "ready_count": 3}]}
```

- **`name`:** 検索の絞り込みに渡す値（`POST /chat` の `collection`）。`documents.collection` と同じ形で、変わらない。
- **`label` / `description` / `sort_order`:** `chatbot.document_collections`（`supabase/migrations/20261025205258_document_collections.sql`）に登録した値。
  並びは `sort_order` の小さい順、同じなら名前順。
- **未登録の名前:** `documents.collection` にあって表に無い名前（フォルダを置いて取り込んだだけのグループ）も返す。`registered: false`、
  `label` は名前そのもの、`description` は空、`sort_order` は `null`（登録済みの後ろ）。
- **`document_count` / `ready_count`:** 全ての文書の数と、検索の対象になる文書（`status = 'ready'`）の数。登録済みで資料が無いグループは 0 / 0。
- **`configured`:** `false` のときは `DATABASE_URL` が空（エラーではなく 200、`collections` は空）。
- **エラー:** 表が無い（マイグレーション 0006 の前）ときは、固定メッセージ「資料グループの情報がまだ用意されていません。」の 503。
  そのほかの DB の失敗は、固定メッセージの 503（例外の内容・接続先は含まない）。表が空なのはエラーではない。
- **認証:** `/documents` と同じ（`AUTH_MODE=required` では Bearer が要る）。`Cache-Control: no-store`。書き込みの API はない（POST などは 405）。

## `GET /documents/{id}/graph`、`GET /documents/{id}/impact`

入寮規程（`tsubasa_dorm_rules.pdf`）の条文の関係（実験）。**対象はこの 1 資料だけ**で、他の資料は 404（`この資料は、関係図の対象ではありません。`）。
DB には何も保存せず、元の PDF（`/file` と同じく、名前とハッシュが DB と合うもの）を、リクエストのたびに読んで計算する。
規則だけで作る（LLM は使わない）。抽出の仕組みと精度は `app/relations/` と `evals/relations/`。

- **`/graph`:** `nodes`（`id`・`type`＝`chapter` / `article` / `paragraph` / `footnote`・`label`・`text`・`parent`・`page`）と、
  `edges`（`type`・`src`・`dst`・`evidence`・`page`・`extracted_by`・`updated_at`）。ノード id は章 `ch3`、条 `a13`、項 `a13p2`、脚注 `fn1`。
  辺の向きは「`src` が `dst` に依存する」。`type` は `references`（参照）・`exception_of`（「前項の定めにかかわらず」）・
  `uses_term`（辞書の定義語を使う）・`same_chapter`（条 → 章の所属）。`evidence` は根拠にした本文。
- **`/impact?node=a13p1&hops=2`:** `node`（項・条・脚注。条を指定すると、その条の全ての項が起点）を変えたとき、依存をたどって
  影響が及ぶ先。`impacted`（`id` と起点からの `hops`）、`articles`（影響が及ぶ条。起点の条は除く）、たどった `edges`、`start`。
  たどる辺は `references` / `exception_of` / `uses_term` だけ（`same_chapter` は依存ではない）。
  `hops` は 1〜10（既定 2）。形の違う `node` と範囲外の `hops` は 422、規程に無い `node` は 404、章（`ch…`）は起点にできず 422。
- **エラー:** 存在しない `id` は 404、元の PDF が無いとき 404、内容が DB と違うとき 409、DB を読めない・未設定は固定メッセージの 503、
  PDF の読み取り・条文の分割に失敗したときは固定メッセージの 500（例外の内容は含まない）。応答は `Cache-Control: no-store`。
- **認証:** `/documents` と同じ（`AUTH_MODE=required` で `Authorization: Bearer`）。
- **画面:** 資料一覧で入寮規程を開き、元の PDF が使えるときだけ「関係図」の切り替えが出る。図に描くのは、依存の辺に出てくる項・脚注だけ
  （依存のない項は選択欄から選ぶ）。項を選ぶと `/impact`（`hops=2` 固定）を呼び、直接（1 ホップ）・間接（2 ホップ）を色と文字で示す。
- **限界:** 辞書（寮費・共益費・入寮期間・保証金の 4 語）に無い語の依存は出ない。語は文字列の一致で見る。測定は `evals/relations/`（同じ人が作った正解に対する値）。
