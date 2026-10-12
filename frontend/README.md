# ザーハード設定資料チャット（フロントエンド）

架空の惑星「ザーハード」の設定資料に答える RAG チャットボット（FastAPI バックエンド）の Web 画面です。
Vite + React + TypeScript で作られた、依存の少ない最小構成のチャット UI です。

- メッセージの送受信（`POST /chat`）と、`thread_id` による会話の継続
- 回答の根拠になった設定資料の表示（「参照した設定資料」）
- エラー（422 / 429 / 500 / 502 / 504 / 通信失敗）の表示と「再試行」
- 動作モード（`mock` / `gemini`）のバッジ表示（`GET /health`）

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

開発サーバーは `/chat` と `/health` をバックエンドへ転送（プロキシ）します。
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
| `npm run preview` | `dist/` をローカルで確認（`/chat` と `/health` は同じ転送先へプロキシ） |
| `npm test` | Vitest（fetch をモックした単体・UI テスト） |
| `npm run typecheck` | `tsc --noEmit` のみ |

## 環境変数

`.env.example` を `.env.local` にコピーして使います（`.env*.local` は Git に含まれません）。

| 変数 | 既定値 | 用途 |
|---|---|---|
| `VITE_DEV_PROXY_TARGET` | `http://localhost:8000` | 開発サーバーが `/chat`・`/health` を転送する先（ビルド成果物には影響しない） |
| `VITE_API_BASE_URL` | 空（相対 URL） | API のベース URL。**ビルド時に埋め込まれる**。別オリジンの API を直接呼ぶときだけ設定する（バックエンドの `CORS_ORIGINS` も必要） |

クライアントは `${VITE_API_BASE_URL ?? ''}/chat` のように相対 URL で API を呼びます。
そのため、`VITE_API_BASE_URL` を設定しなければ、同じビルドを開発（プロキシ）でも
本番（FastAPI と同一オリジン）でも変更なしで使えます。

## 本番構成（予定）

Cloud Run へ載せることを見据え、**フロントエンドを FastAPI から静的配信する**構成にします。

1. マルチステージ Dockerfile の最初のステージ（`node`）で `npm ci && npm run build` を実行し、`dist/` を作る。
2. 最終ステージ（Python）へ `dist/` だけをコピーし、FastAPI が `/` から配信する。
   API（`/chat`、`/health`）は同じオリジンのまま。
3. 同一オリジンなので CORS は不要で、`VITE_API_BASE_URL` も設定しない。

`vite.config.ts` は `base: '/'`、`build.outDir: 'dist'` にしてあり、この構成にそのまま使えます。
Dockerfile と FastAPI 側の静的配信は、このディレクトリの外（バックエンド側）の作業です。

## ディレクトリ構成

```
frontend/
├── index.html
├── vite.config.ts            # プロキシ・ビルド・テストの設定
├── src/
│   ├── api/chat.ts           # /chat・/health のクライアントと ChatApiError
│   ├── hooks/useChat.ts      # 会話の状態（messages / pending / error / thread_id）
│   ├── hooks/useBackendMode.ts
│   ├── lib/sources.ts        # context から出典（ファイル名と本文）を取り出す
│   ├── lib/storage.ts        # thread_id の sessionStorage 保存（失敗しても動く）
│   ├── components/           # Header / MessageList / SourcesPanel / ErrorBanner / Composer
│   ├── App.tsx, main.tsx, styles.css
│   └── test/                 # テストの共通設定とヘルパー
```

## 仕様メモ

- **thread_id**: サーバーが返した値を保持し、`sessionStorage` に保存する（タブを閉じると消える）。
  ストレージが使えない環境でも、画面を開いている間は会話を続けられる。
  保存されるのは thread_id だけで、画面を再読み込みすると表示中のメッセージは消える
  （サーバー側の履歴は残るため、同じ thread_id で会話は続く）。
- **二重送信の防止**: 回答待ちの間は送信ボタンも Enter も無効。バックエンドには thread ごとの排他が無く、
  同時に 2 通送ると履歴の一部が失われるため。
- **エラー時**: ユーザーの発言は残り、「再試行」で同じ発言を（吹き出しを増やさずに）再送する。
- **日本語入力**: IME の変換確定の Enter では送信しない。
- **出典の表示**: `context` に `[ファイル名.txt]` の見出しがあるときだけ「参照した設定資料」を表示する。
  「該当する設定資料は見つかりませんでした」の定型文は出典として扱わない。
- **安全性**: 返答は常にテキストとして描画し、HTML としては解釈しない。
