---
paths:
  - "frontend/src/**"
  - "frontend/app/**"
  - "frontend/e2e/**"
---

# frontend/ を触るとき

詳細は [frontend/CLAUDE.md](../../frontend/CLAUDE.md)。画面の見た目・レイアウトの変更は [ui-review.md](ui-review.md)（モックの承認が先）。

- 画面はブラウザーだけで描く SPA。Server Components の取得・Route Handlers・Server Actions を足さない
- 層の向き: `app` → `Root`/`App` → `components` → `hooks` → `api` / `auth` / `lib`。`fetch` は `api/` と `auth/` の中だけ。`components` と `lib` が `api` から import するのは型と定数だけ
- 共有する hooks の部品は `hooks/useLoaded.ts`。別の hook ファイルに同居させない
- `NEXT_PUBLIC_*` は公開される。secret key・`DATABASE_URL`・`GEMINI_API_KEY` を入れない。環境変数を読むのは `api/chat.ts` と `auth/supabase.ts` だけ
- サーバーのエラー本文を出さない（固定メッセージ。日本語の固定 `detail` を返す状態だけ例外）。HTML として解釈しない（`dangerouslySetInnerHTML` 禁止）
- 二重送信の防止（`pendingRef`）と、停止＝切断を壊さない。権限はサーバーが決める
- `package.json` / `package-lock.json` を変えない（変えるなら承認）。デプロイしない（本番は `main` へのプッシュで更新される）
- 触れたら `npm run typecheck`、`npm test`、`NEXT_DIST_DIR=.next-verify npm run build`。ビルドが書き換える `tsconfig.json` の差分はコミットしない。`test:e2e:real-api` は課金なので確認なしに実行しない
