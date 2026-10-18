# ドキュナビ（DocuNavi）フロントエンド

資料に答える RAG チャットボット（FastAPI バックエンド）の Web 画面です。
Vite + React + TypeScript で作られた、依存の少ない最小構成のチャット UI です。

- メッセージの送信（`POST /chat/stream`）と、回答の逐次表示（SSE）。`thread_id` による会話の継続
- 生成中の「停止」と、回答の「コピー」
- 回答の根拠になった設定資料の表示（「参照した設定資料」）
- エラー（422 / 429 / 500 / 502 / 503 / 504 / 通信失敗 / 本文の途中の失敗）の表示と「再試行」
- 動作モード（`mock` / `gemini`）のバッジ表示（`GET /health`）
- 「資料一覧」タブ: DB に保存された資料を、文書ごと・チャンクの全文まで読み取り専用で表示（`GET /documents`、`GET /documents/{id}/chunks`）。
  DB 未設定・文書 0 件・取得失敗を分けて表示する。BM25 検索が読む本文とは別（画面に注意書きがある）

回答の読み取り（SSE のパースと、メッセージの組み立て）は、AI SDK の `useChat`（`@ai-sdk/react`）に任せています。
バックエンドの `/chat/stream` が AI SDK の UI message stream を返すので、トランスポートに URL と `fetch` を渡すだけです。

## 前提

- Node.js 22.12 以上（Vite 8 / Vitest 5 の要件。動作確認は Node 24）と npm
- 動かしているバックエンド（既定では `http://localhost:8000`）

## 開発サーバーの起動

別々のターミナルでバックエンドとフロントエンドを起動します。

```bash
# 1) バックエンド（リポジトリ直下。API キーが無ければ自動でモックモードになる）
.venv/bin/uvicorn app.main:app --port 8000

# 2) フロントエンド
cd frontend
npm install
npm run dev        # http://localhost:5173
```

開発サーバーは `/chat`（`/chat/stream` を含む）・`/health`・`/documents` をバックエンドへ転送（プロキシ）します。
ブラウザから見ると同一オリジンなので、バックエンド側の CORS 設定は不要です。
転送先を変えるときは `VITE_DEV_PROXY_TARGET` を使います。

```bash
VITE_DEV_PROXY_TARGET=http://localhost:9000 npm run dev
```

## スクリプト

| コマンド | 内容 |
|---|---|
| `npm run dev` | 開発サーバー（HMR、API プロキシ付き） |
| `npm run build` | 型チェックのあと `dist/` に本番ビルド |
| `npm run preview` | `dist/` をローカルで確認（`/chat`・`/chat/stream` と `/health` は同じ転送先へプロキシ） |
| `npm test` | Vitest（fetch をモックした単体・UI テスト） |
| `npm run typecheck` | `tsc --noEmit` のみ |

## 環境変数

`.env.example` を `.env.local` にコピーして使います（`.env*.local` は Git に含まれません）。

| 変数 | 既定値 | 用途 |
|---|---|---|
| `VITE_DEV_PROXY_TARGET` | `http://localhost:8000` | 開発サーバーが `/chat`・`/chat/stream`・`/health`・`/documents` を転送する先（ビルド成果物には影響しない） |
| `VITE_SUPABASE_URL` / `VITE_SUPABASE_PUBLISHABLE_KEY` | 空 | 両方そろっていると Google ログイン（Supabase Auth）を使う。**ビルド時に埋め込まれる**。publishable（anon）キーは公開してよい鍵で、`service_role` は絶対に入れない。`.env.local`（Git 管理外）に書く。どちらかが空なら、ログイン画面は出ず認証なしで動く。バックエンドの `AUTH_MODE` と Supabase・Google 側の設定は [docs/configuration.md](../docs/configuration.md) の「ログイン（認証）」 |
| `VITE_API_BASE_URL` | 空（相対 URL） | API のベース URL。**ビルド時に埋め込まれる**。別オリジンの API を直接呼ぶときだけ設定する（バックエンドの `CORS_ORIGINS` も必要） |

クライアントは `${VITE_API_BASE_URL ?? ''}/chat/stream` のように相対 URL で API を呼びます。
そのため、`VITE_API_BASE_URL` を設定しなければ、同じビルドを開発（プロキシ）でも
本番（FastAPI と同一オリジン）でも変更なしで使えます。

## 本番構成（予定）

Cloud Run へ載せることを見据え、**フロントエンドを FastAPI から静的配信する**構成にします。

1. マルチステージ Dockerfile の最初のステージ（`node`）で `npm ci && npm run build` を実行し、`dist/` を作る。
2. 最終ステージ（Python）へ `dist/` だけをコピーし、FastAPI が `/` から配信する。
   API（`/chat`、`/chat/stream`、`/health`）は同じオリジンのまま。
3. 同一オリジンなので CORS は不要で、`VITE_API_BASE_URL` も設定しない。

`vite.config.ts` は `base: '/'`、`build.outDir: 'dist'` にしてあり、この構成にそのまま使えます。
Dockerfile と FastAPI 側の静的配信は、このディレクトリの外（バックエンド側）の作業です。

## ディレクトリ構成

```
frontend/
├── index.html
├── vite.config.ts            # プロキシ・ビルド・テストの設定
├── src/
│   ├── api/chat.ts           # トランスポート用の fetch（HTTP エラー → ChatApiError）と /health
│   ├── api/documents.ts      # 資料一覧の取得と、応答の形の検査
│   ├── api/graph.ts          # 入寮規程の関係図（/graph と /impact）の取得と、応答の形の検査
│   ├── auth/                 # Google ログイン（Supabase Auth）。types.ts = 窓口の型、supabase.ts = SDK、authFetch.ts = トークンを付けて 401 なら 1 回更新、AuthContext.tsx = ログイン状態
│   ├── hooks/useChat.ts      # AI SDK の useChat を包み、会話の状態（messages / pending / error / thread_id）を作る
│   ├── hooks/useBackendMode.ts
│   ├── hooks/useDocuments.ts # 資料一覧・チャンク・関係図・影響の取得（認証つき fetch。取り直しと中断）
│   ├── lib/relationGraph.ts  # 関係図の、描く項の選び方・並べ方（dagre）・影響の分類（画面に依らない）
│   ├── lib/sources.ts        # context から出典（ファイル名と本文）を取り出す
│   ├── lib/storage.ts        # thread_id の sessionStorage 保存（失敗しても動く）
│   ├── components/           # Header / MessageList / SourcesPanel / ErrorBanner / Composer / LoginScreen / DocumentsView / RelationGraphView（関係図。図の部品 RelationCanvas は React Flow を使い、開いたときに読む）
│   ├── App.tsx, main.tsx, styles.css
│   └── test/                 # テストの共通設定とヘルパー
```

## 仕様メモ

- **thread_id**: サーバーが最初のチャンク（`start` の `messageMetadata`）で返した値を保持し、`sessionStorage` に保存する（タブを閉じると消える）。
  本文の途中で失敗しても分かっているので、再試行は同じ会話に送る。
  ストレージが使えない環境でも、画面を開いている間は会話を続けられる。
  保存されるのは thread_id だけで、画面を再読み込みすると表示中のメッセージは消える
  （サーバー側の履歴は残るため、同じ thread_id で会話は続く）。
- **二重送信の防止**: 回答待ちの間は送信ボタンも Enter も無効。バックエンドには thread ごとの排他が無く、
  同時に 2 通送ると履歴の一部が失われるため（AI SDK は、同時の送信を止めずに受け付けるので、フックの `pendingRef` で止めている）。
- **本文**: メッセージの本文は「最後のテキスト部分」。ツール呼び出しの前置きや、出力ガードが止める前の部分は表示しない。
  回答の生成中は、出典とコピーを出さず、ログに `aria-busy` を付ける（読み上げを完了まで待たせる）。
- **スクロール**: 末尾の近くにいるときだけ、新しい本文に合わせて自動でスクロールする。遡って読んでいるときは動かさない。
- **停止**: 「停止」はリクエストの中断で、サーバーは切断を見て LLM 呼び出しを取り消す。それまでに届いた本文は残り、エラーにはしない。
- **エラー時**: ユーザーの発言は残り、「再試行」で同じ発言を（吹き出しを増やさずに）再送する。
  本文の途中で失敗したときは、届いた分を残したまま、固定メッセージ（サーバーの `error` チャンク）を表示し、再試行で置き換える。
- **日本語入力**: IME の変換確定の Enter では送信しない。
- **出典の表示**: `context` に `[ファイル名.txt]` の見出しがあるときだけ「参照した設定資料」を表示する。
  「該当する設定資料は見つかりませんでした」の定型文は出典として扱わない。
- **安全性**: 返答は常にテキストとして描画し、HTML としては解釈しない。
