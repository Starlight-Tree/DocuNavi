# frontend/（Next.js の画面）

全体の方針・禁止事項は、ルートの [CLAUDE.md](../CLAUDE.md)。使い方・環境変数・E2E・仕様メモは [README.md](README.md)。UI/UX の変更は、ルートの CLAUDE.md の「UI/UX」の節と `.claude/rules/ui-review.md`（モックの承認が先）。

Cloudflare の Worker `docunavi-web`（OpenNext）が配信する画面。本番へは、`main` へのプッシュでは出ない（ユーザーが手動の `deploy.yml` を実行する）。API は別オリジン（Cloud Run の FastAPI。`/chat/stream` だけは Go の SSE 中継 `ops/sse-relay/`）。**画面はブラウザーだけで描く SPA**（`app/client-app.tsx` が `src/Root.tsx` を `ssr: false` で読む）。サーバー側の処理（Server Components の取得、Route Handlers、Server Actions）を足さない（例外は 2 本だけ: `app/api/auth/[...all]/route.ts`＝`/api/auth/*` をデータ用 Worker へ中継するだけ。`app/api/documents/[id]/file/route.ts`＝PDF を、Cookie から取った JWT を Bearer にして API から取り、本文をそのまま返すだけ。権限は API が決める。[ADR 0013](../docs/decisions/0013-pdf-via-same-origin-relay.md)）。

## 層の境界（依存は上から下の一方向）

| 層 | 場所 | 責務 | 持たないもの |
|---|---|---|---|
| ページ | `app/` | App Router の入口だけ（`page.tsx` → `client-app.tsx`） | 画面の中身 |
| 画面の骨格 | `src/Root.tsx` `src/App.tsx` | プロバイダーの組み立て、タブ・URL に応じた画面の切り替え | API の呼び出し |
| コンポーネント | `src/components/` | 表示と入力。状態は props で受ける | `fetch`、環境変数、認証の SDK |
| hooks | `src/hooks/` | 取得・状態・副作用。`api/` と `lib/` を組み合わせる | JSX |
| 共通の部品 | `src/hooks/useLoaded.ts` | `Loaded<T>`（loading / error / ready）、`useLoaded`、`useAuthedJsonFetch`。資料・会話・資料グループ・対象と資料種別の hooks が共有する | 個別の API の知識 |
| APIクライアント | `src/api/` | `fetch`、応答の検証、固定のエラー文、型（`chat` `collections` `classifications` `documents` `graph` `threads`）。PDF.js との境界 `pdfjs.ts`（`pdfjs-dist` を import してよいのは、このファイルだけ。テストでは `src/test/fakePdf.ts` に差し替わる） | React、状態 |
| 認証 | `src/auth/` | Better Auth（`betterAuth.ts` だけが SDK を持つ）、トークン付き `fetch`（`authFetch.ts`）、ログイン状態（`AuthContext.tsx`） | チャットの知識 |
| 純粋な関数 | `src/lib/` | 出典の取り出し、関係図の計算、URL・ストレージ、整形。React に依存しない（`markdown.tsx` を除く） | 通信 |
| テスト | `src/**/*.test.ts(x)`、`src/test/`、`e2e/` | 単体・UI は Vitest（`fetch` を差し替える）。E2E は Playwright（偽の LLM・偽の検索） | 実 API |

- `components` と `lib` が `api` から import してよいのは、**型と定数だけ**（入力の上限、検証のパターンなど）。`fetch` は `api/` と `auth/` の中に閉じる（中継の Route Handler を除く）。
- `hooks` どうしで共有するものは `useLoaded.ts` に置く（以前は `useDocuments.ts` に同居していた。2026-11-01 に分けた）。
- テストは対象の隣に置く（`foo.ts` と `foo.test.ts`）。画面をまたぐ統合のテストは `src/*.test.tsx`（`App` `AuthApp` `DocumentsApp` `Scope` `Threads` `RelationGraph` `ChatSources`）。共通の部品は `src/test/`。

## 守ること

- **`NEXT_PUBLIC_*` はバンドルに埋め込まれ、誰でも読める。** 入れてよいのは API・中継・認証（Better Auth）の URL と、ログインの有効/無効の値だけ。秘密鍵・`D1_API_TOKEN`・`GEMINI_API_KEY` を入れない。読むのは `api/chat.ts`（API・中継の URL）と `auth/betterAuth.ts`（`NEXT_PUBLIC_AUTH_ENABLED` `NEXT_PUBLIC_AUTH_BASE_URL`）だけ。中継の Route Handler が読むのは、ビルド時に埋め込む非公開名の中継先（`AUTH_PROXY_TARGET` と、新設する `FILE_PROXY_TARGET`）だけ（`next.config.ts` の `env`）。`NEXT_PUBLIC_*` は読まない。
- **サーバーのエラー本文を画面に出さない。** 固定のメッセージにする（`api/` の `ChatApiError`）。ただし、サーバーが固定の日本語の `detail` を返す 401 / 403 / 413 / 429 / 500 / 502 / 503 / 504 は、その文をそのまま見せる（`api/chat.ts` の `SERVER_DETAIL_STATUSES`。413・429・503 は `ops/sse-relay/` の固定の文面と対）。PDF の Route Handler も、失敗時は API の本文を通さず、固定の日本語の HTML を返す。
- **返答は HTML として解釈しない。** `dangerouslySetInnerHTML` を使わない。
- **履歴・資料グループの権限はサーバーが決める。** 画面で隠すだけにしない。権限の単位は資料グループ（`collection`）のまま。チャットの質問対象は「対象」（`target`。`GET /targets`）で、検索は「見える資料 ∩ 対象」。
  対象と資料種別（`GET /doc-types`）の表示名・件数はサーバーが持つ（固定で持たない）。旧グループ（`collection`）の会話は、保存された範囲のまま続け、新しくは選べず、対象に自動で変えない。
  `useChat` の範囲は `{all | target | collection}`。対象と `collection` は同時に送らない（サーバーが 422）。旧 `?collection=` の URL は、資料一覧の「旧フィルター」として知らせ、解除できる（対象と同時なら AND）。
- **二重送信を防ぐ**（`useChat.ts` の `pendingRef`）。**停止は切断**（サーバーが LLM 呼び出しを取り消す）。この 2 つを壊さない。
- **依存を足さない・上げない**（`package.json` と `package-lock.json`）。変えるときは承認を得る。`pdfjs-dist` は版を固定し（2026-11-19 に追加。[ADR 0014](../docs/decisions/0014-pdfjs-viewer.md)）、上げるときは `public/pdfjs/`（同梱の CMap・フォント・wasm のコピー）も同じ版に作り直す（手順は ADR 0014。`src/pdfjsAssets.test.ts` が食い違いを検出する）。
- **デプロイしない。** 本番は、手動の `deploy.yml`（`workflow_dispatch` だけ。`main` で実行）で更新される。Environment「production」の必須レビュアーは 2026-11-15 に外してあり（許可するブランチは `main` だけ。`deploy.yml` 冒頭のコメントで確認、2026-11-18）、**技術的な承認の関門は無い。** 関門は会話の約束だけで、起動の前に、対象のコミットと `frontend/` `workers/` の変更の有無を示して、ユーザーの明示の承認を得る（そのたびに。前の承認は次に及ばない。ルートの [CLAUDE.md](../CLAUDE.md) の「Git」の「CI」）。`wrangler deploy` などの書き込み系を、自分で実行しない。

## 検査（変更したら実行する）

```bash
cd frontend
npm run typecheck
npm test                                        # Vitest 719 件（2026-11-20）
NEXT_DIST_DIR=.next-verify npm run build        # コンテナの next dev の .next を壊さないよう、出力先を分ける
```

- `next build` は、出力先を変えると `tsconfig.json` の `include` に `<出力先>/types/**` を足す。**その差分をコミットしない**（元の `.next` / `.next-e2e*` の行だけが正）。
- `.next*/` は Git 管理外だが、`.next-verify` のように `.gitignore` に無い名前は管理外にならない。使ったら、承認を得て消すか、`.next-e2e*` の名前を使う。
- E2E（`npm run test:e2e`）は、Chromium とバックエンドの venv が要る。触ったのが画面の動きなら実行する。`test:e2e:real-api` は**課金**なので、確認なしに実行しない。
