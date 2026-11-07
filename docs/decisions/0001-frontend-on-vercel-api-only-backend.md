# 0001 画面は Vercel（Next.js）、バックエンドは API だけ

- 状態: 一部置き換え済み（画面の配信先は、2026-11-08 に Vercel から Cloudflare の Worker `docunavi-web` へ移した。下の「追記」）
- 日付と決めた人: 2026-10（利用者）。FastAPI からの画面の配信を外したのは 2026-10-24。Next.js 版への置き換えと `frontend-next/` → `frontend/` の改名は 2026-10-26。

## 背景
当初は FastAPI が Vite の画面を配信していた。クロスオリジンの認証、環境ごとの API の向き先、CORS を実際に動かして学ぶことも目的だった。

## 決定
- 画面（`frontend/`、Next.js）は Vercel が配信する。FastAPI は画面を配信せず、`/` は 404。全捕捉ルート（`/{path}`）も足さない。
- API は別オリジン（Cloud Run）。`CORS_ORIGINS` に画面のオリジンを入れる。
- 画面はブラウザーだけで描く SPA（`ssr: false`）。サーバー側の処理は足さない。
- `NEXT_PUBLIC_*` はバンドルに埋め込まれるので、公開してよい値（Supabase の URL と publishable key、API・中継の URL）だけを入れる。

## 結果と限界
- 本番（Vercel → Cloud Run）で Google ログイン → 質問 → SSE の回答まで確認済み（2026-10-20、10-24〜25）。
- 画面を配信しない API の 405 やリダイレクトを壊さないため、全捕捉ルートは追加しない。
- `main` へのプッシュで Vercel の本番が更新される。

## 出典
[architecture.md](../architecture.md) の「画面はどう配信されるか」、[target-architecture.md](../target-architecture.md)、ルートの CLAUDE.md、[frontend/CLAUDE.md](../../frontend/CLAUDE.md)

## 追記（2026-11-08）: 画面の配信先を Cloudflare の Worker に置き換えた
- 画面（`frontend/`）の配信先は、Vercel から Cloudflare の Worker `docunavi-web`（OpenNext）へ移った。Vercel のプロジェクトは削除済み。
- 本番へのデプロイは、`main` へのプッシュ（Git 連携）ではなく、ユーザーが手動で実行する `.github/workflows/deploy.yml`（Environment の承認つき）。
- 変わらないもの: FastAPI は画面を配信せず `/` は 404、全捕捉ルートを足さない、画面はブラウザーだけで描く SPA（`ssr: false`）、`NEXT_PUBLIC_*` には公開してよい値だけ。
- 移行の手順と状態は [cloudflare-migration.md](../cloudflare-migration.md)。上の本文は、当時の決定として残している。
