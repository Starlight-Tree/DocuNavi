# ドキュナビ（DocuNavi）フロントエンド（frontend）

資料に答える RAG チャットボット（FastAPI バックエンド）の Web 画面です。Next.js（App Router）+ React + TypeScript で、
**Vercel が配信します**。バックエンド（Cloud Run の FastAPI）は API だけを持ち、画面は配信しません（構成は [docs/target-architecture.md](../docs/target-architecture.md)）。
画面は今までどおりブラウザーだけで描きます（`app/client-app.tsx` が `src/Root.tsx` を `ssr: false` で読み込む SPA）。

- メッセージの送信（`POST /chat/stream`）と、回答の逐次表示（SSE）。`thread_id` による会話の継続
- 生成中の「停止」と、回答の「コピー」
- 回答の根拠になった設定資料の表示（「参照した設定資料」）
- エラー（422 / 429 / 500 / 502 / 503 / 504 / 通信失敗 / 本文の途中の失敗）の表示と「再試行」
- 動作モード（`mock` / `gemini`）のバッジ表示（`GET /health`）
- 「資料一覧」タブ: DB に保存された資料を、文書ごと・チャンクの全文まで読み取り専用で表示（`GET /documents`、`GET /documents/{id}/chunks`）。
  DB 未設定・文書 0 件・取得失敗を分けて表示する
- 会話の一覧・再開・削除（`GET /threads` ほか）、入寮規程の関係図（実験）、Google ログイン（Supabase Auth）

回答の読み取り（SSE のパースと、メッセージの組み立て）は、AI SDK の `useChat`（`@ai-sdk/react`）に任せています。
バックエンドの `/chat/stream` が AI SDK の UI message stream を返すので、トランスポートに URL と `fetch` を渡すだけです。

## 前提

- Node.js 22.12 以上（動作確認は Node 24）と npm
- 動かしているバックエンド（既定では `http://localhost:8000`）

## 開発サーバーの起動

別々のターミナルでバックエンドと画面を起動します。

```bash
# 1) バックエンド（リポジトリ直下。API キーが無ければ自動でモックモードになる）
.venv/bin/uvicorn app.main:app --port 8000

# 2) 画面
cd frontend
npm install
cp .env.example .env.local   # 初回だけ。API_PROXY_TARGET の転送で動かすなら NEXT_PUBLIC_API_BASE_URL は空のまま
npm run dev                  # http://localhost:3000
```

`NEXT_PUBLIC_API_BASE_URL` が空のとき、開発サーバーは `/chat`（`/chat/stream` を含む）・`/health`・`/documents`・`/threads` を
`API_PROXY_TARGET` へ転送します（`next.config.ts` の rewrites）。ブラウザーからは同じオリジンに見えるので、バックエンド側の CORS 設定は要りません。
Compose の `frontend` サービスも、この形（`API_PROXY_TARGET=http://api:8000`）で動きます。

## UI モック（`/mock`）

PC 向けの画面の整理案（左サイドバー + チャット / 資料 + 右パネル）を、サンプルデータで操作できる試作です。`src/mock/` に閉じていて、本体の画面・認証・API には触れません
（通信も保存もしない。再読み込みで初期状態に戻る。回答は定型文）。`UI_MOCK=1` で起動・ビルドしたときだけ開き、それ以外（Vercel の本番を含む）では `/` へ戻ります（`next.config.ts`）。

```bash
cd frontend
UI_MOCK=1 npm run dev        # http://localhost:3000/mock（?view=documents で資料画面から開く）
```

資料の取り込みは時間で進みます（追加から約 4.5 秒で利用可能。ファイル名に「エラー」を含むと失敗し、再試行で成功）。

### 対象・資料種別のモック（`/mock/classification`）

資料の分類「対象」「資料種別」を足した画面の案（2026-11-03。承認済みで、本体（`src/components/DocumentFilterBar.tsx` `ScopePicker.tsx` など）へ移した。モックは残す）。`src/mock/classification/` に閉じた、架空データの試作で、本体の部品（`Composer` `MessageList` `AppSidebar` と本体の CSS）を使う。
資料ライブラリ（検索・対象・資料種別 + 「詳細フィルター」）と、チャットの上の帯の質問対象メニューを確かめる（本体は 2026-11-04 に、「フィルター」と、入力欄の中の回答対象チップへ変えた。このモックは変えていない）。上の帯のラジオで、旧 URL（`?collection=`）・旧グループの会話・対象が使えない会話の状態に切り替える。
`UI_MOCK=1 npm run dev` で `http://localhost:3000/mock/classification`。

## スクリプト

| コマンド | 内容 |
|---|---|
| `npm run dev` | 開発サーバー（HMR。API の転送付き） |
| `npm run build` / `npm start` | 本番ビルドと、その起動 |
| `npm test` | Vitest（fetch をモックした単体・UI テスト） |
| `npm run typecheck` | `tsc --noEmit` のみ |
| `npm run test:e2e` | Playwright の E2E（下の「E2E」） |
| `npm run test:e2e:real-api` | 実 API の検証（**本物の Gemini を呼ぶ。課金**。通常の E2E・CI には入らない） |

## 環境変数

`.env.example` を `.env.local` にコピーして使います（`.env*.local` は Git に含まれません）。

| 変数 | 既定値 | 用途 |
|---|---|---|
| `NEXT_PUBLIC_API_BASE_URL` | 空（相対 URL） | API のベース URL。**ビルド時に埋め込まれる**。本番（Vercel）では Cloud Run の URL を設定し、バックエンドの `CORS_ORIGINS` に Vercel のオリジンを入れる |
| `NEXT_PUBLIC_STREAM_BASE_URL` | 空（`NEXT_PUBLIC_API_BASE_URL` を使う） | `/chat/stream` だけの接続先（Go の SSE 中継 [ops/sse-relay](../ops/sse-relay/README.md)）。**ビルド時に埋め込まれる**ので、変えるには再ビルド（再デプロイ）が要る。空に戻すと中継を通さない。ほかのエンドポイントは変わらない |
| `API_PROXY_TARGET` | 未設定（転送しない） | 開発サーバーが API のパスを転送する先。**ビルド時に決まる**。`NEXT_PUBLIC_API_BASE_URL` を空にするときに使う |
| `NEXT_PUBLIC_SUPABASE_URL` / `NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY` | 空 | 両方そろっていると Google ログイン（Supabase Auth）を使う。**ビルド時に埋め込まれる**。publishable key は公開してよい鍵で、`service_role` は絶対に入れない。どちらかが空なら、ログイン画面は出ず認証なしで動く。バックエンドの `AUTH_MODE` と Supabase・Google 側の設定は [docs/configuration.md](../docs/configuration.md) の「ログイン（認証）」 |
| `NEXT_DIST_DIR` | `.next` | ビルドの出力先。E2E が、認証なし・認証ありの 2 つのビルドを別のフォルダーに作るための切り替え |

## E2E

`npm run test:e2e` は、実ブラウザー（Chromium）から、画面と API を別のポートで立てて確かめます。実 API は使いません。

- API: `tests/e2e_server.py`（偽の LLM・偽の検索の本物のアプリ。`127.0.0.1:8100`）。画面は配信せず、CORS は 2 つの画面のオリジンだけに許します
- 画面: `npm run build:e2e` / `build:e2e:auth` のビルドを `next start` で立てます（`3100` = ログインなし、`3101` = 偽の Supabase の URL を入れたビルド）
- シナリオ: `e2e/chat.e2e.ts`（質問 → 出典つきの回答）、`e2e/auth.e2e.ts`（偽の Supabase でのログイン、トークンの更新。`e2e/support/supabase-mock.ts`）
- 初回だけ、Chromium を入れます: `npx playwright install chromium`
- 失敗時の成果物（`test-results/`・`playwright-report/`）は Git 管理外です。経緯は [docs/development.md](../docs/development.md)

## ディレクトリ構成

層の境界と守ること（Claude Code 向け）は [CLAUDE.md](CLAUDE.md)。

```
frontend/
├── app/                      # App Router（page.tsx → client-app.tsx → src/Root.tsx）
├── next.config.ts            # API の転送（rewrites）・ビルドの出力先
├── playwright.config.ts, e2e/
└── src/
    ├── api/                  # chat.ts（トランスポート用の fetch と /health）、collections.ts、classifications.ts（/targets・/doc-types）、documents.ts、threads.ts、graph.ts
    ├── auth/                 # Google ログイン（Supabase Auth）。types.ts = 窓口の型、supabase.ts = SDK、authFetch.ts = トークンを付けて 401 なら 1 回更新、AuthContext.tsx = ログイン状態
    ├── hooks/                # useChat（AI SDK の useChat を包む）、useBackendMode、useCollections、useDocuments、useThreads、useLoaded（取得状態 Loaded と認証つき fetch の共通部品）
    ├── lib/                  # relationGraph（関係図の選び方・並べ方・影響の分類）、sources（出典の取り出し）、storage、urlState、markdown ほか
    ├── components/           # AppSidebar / DocumentPanel / PanelHost / MessageList / SourcesPanel / ErrorBanner / Composer / LoginScreen / DocumentsView / ThreadSidebar / RelationGraphView ほか
    ├── App.tsx, Root.tsx, styles.css
    └── test/                 # テストの共通設定とヘルパー
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
