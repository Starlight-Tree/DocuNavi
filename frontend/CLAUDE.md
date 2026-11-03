# frontend/（Next.js の画面）

全体の方針・禁止事項は、ルートの [CLAUDE.md](../CLAUDE.md)。使い方・環境変数・E2E・仕様メモは [README.md](README.md)。UI/UX の変更は、ルートの CLAUDE.md の「UI/UX」の節と `.claude/rules/ui-review.md`（モックの承認が先）。

Vercel が配信する画面。`main` へのプッシュで本番が更新される。API は別オリジン（Cloud Run の FastAPI。`/chat/stream` だけは Go の SSE 中継 `ops/sse-relay/`）。**画面はブラウザーだけで描く SPA**（`app/client-app.tsx` が `src/Root.tsx` を `ssr: false` で読む）。サーバー側の処理（Server Components の取得、Route Handlers、Server Actions）を足さない。

## 層の境界（依存は上から下の一方向）

| 層 | 場所 | 責務 | 持たないもの |
|---|---|---|---|
| ページ | `app/` | App Router の入口だけ（`page.tsx` → `client-app.tsx`） | 画面の中身 |
| 画面の骨格 | `src/Root.tsx` `src/App.tsx` | プロバイダーの組み立て、タブ・URL に応じた画面の切り替え | API の呼び出し |
| コンポーネント | `src/components/` | 表示と入力。状態は props で受ける | `fetch`、環境変数、Supabase |
| hooks | `src/hooks/` | 取得・状態・副作用。`api/` と `lib/` を組み合わせる | JSX |
| 共通の部品 | `src/hooks/useLoaded.ts` | `Loaded<T>`（loading / error / ready）、`useLoaded`、`useAuthedJsonFetch`。資料・会話・資料グループ・対象と資料種別の hooks が共有する | 個別の API の知識 |
| APIクライアント | `src/api/` | `fetch`、応答の検証、固定のエラー文、型（`chat` `collections` `classifications` `documents` `graph` `threads`） | React、状態 |
| 認証 | `src/auth/` | Supabase Auth（`supabase.ts` だけが SDK を持つ）、トークン付き `fetch`（`authFetch.ts`）、ログイン状態（`AuthContext.tsx`） | チャットの知識 |
| 純粋な関数 | `src/lib/` | 出典の取り出し、関係図の計算、URL・ストレージ、整形。React に依存しない（`markdown.tsx` を除く） | 通信 |
| テスト | `src/**/*.test.ts(x)`、`src/test/`、`e2e/` | 単体・UI は Vitest（`fetch` を差し替える）。E2E は Playwright（偽の LLM・偽の検索） | 実 API |

- `components` と `lib` が `api` から import してよいのは、**型と定数だけ**（入力の上限、検証のパターンなど）。`fetch` は `api/` と `auth/` の中に閉じる。
- `hooks` どうしで共有するものは `useLoaded.ts` に置く（以前は `useDocuments.ts` に同居していた。2026-11-01 に分けた）。
- テストは対象の隣に置く（`foo.ts` と `foo.test.ts`）。画面をまたぐ統合のテストは `src/*.test.tsx`（`App` `AuthApp` `DocumentsApp` `Scope` `Threads` `RelationGraph` `ChatSources`）。共通の部品は `src/test/`。

## 守ること

- **`NEXT_PUBLIC_*` はバンドルに埋め込まれ、誰でも読める。** 入れてよいのは Supabase の URL と publishable key、API・中継の URL だけ。secret key・`DATABASE_URL`・`GEMINI_API_KEY` を入れない。読むのは `api/chat.ts`（API・中継の URL）と `auth/supabase.ts` だけ。
- **サーバーのエラー本文を画面に出さない。** 固定のメッセージにする（`api/` の `ChatApiError`）。ただし、サーバーが固定の日本語の `detail` を返す 401 / 403 / 429 / 500 / 502 / 503 / 504 は、その文をそのまま見せる（`api/chat.ts` の `SERVER_DETAIL_STATUSES`。503 は `ops/sse-relay/` の上限の文面と対）。
- **返答は HTML として解釈しない。** `dangerouslySetInnerHTML` を使わない。
- **履歴・資料グループの権限はサーバーが決める。** 画面で隠すだけにしない。権限の単位は資料グループ（`collection`）のまま。チャットの質問対象は「対象」（`target`。`GET /targets`）で、検索は「見える資料 ∩ 対象」。
  対象と資料種別（`GET /doc-types`）の表示名・件数はサーバーが持つ（固定で持たない）。旧グループ（`collection`）の会話は、保存された範囲のまま続け、新しくは選べず、対象に自動で変えない。
  `useChat` の範囲は `{all | target | collection}`。対象と `collection` は同時に送らない（サーバーが 422）。旧 `?collection=` の URL は、資料一覧の「旧フィルター」として知らせ、解除できる（対象と同時なら AND）。
- **二重送信を防ぐ**（`useChat.ts` の `pendingRef`）。**停止は切断**（サーバーが LLM 呼び出しを取り消す）。この 2 つを壊さない。
- **依存を足さない・上げない**（`package.json` と `package-lock.json`）。変えるときは承認を得る。
- **デプロイしない。** 本番は `main` へのプッシュ（承認が要る）で更新される。`vercel` の書き込み系 MCP・CLI を使わない。

## 検査（変更したら実行する）

```bash
cd frontend
npm run typecheck
npm test                                        # Vitest 490 件（2026-11-04）
NEXT_DIST_DIR=.next-verify npm run build        # コンテナの next dev の .next を壊さないよう、出力先を分ける
```

- `next build` は、出力先を変えると `tsconfig.json` の `include` に `<出力先>/types/**` を足す。**その差分をコミットしない**（元の `.next` / `.next-e2e*` の行だけが正）。
- `.next*/` は Git 管理外だが、`.next-verify` のように `.gitignore` に無い名前は管理外にならない。使ったら、承認を得て消すか、`.next-e2e*` の名前を使う。
- E2E（`npm run test:e2e`）は、Chromium とバックエンドの venv が要る。触ったのが画面の動きなら実行する。`test:e2e:real-api` は**課金**なので、確認なしに実行しない。
