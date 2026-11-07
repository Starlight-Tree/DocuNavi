# Vercel・Supabase から Cloudflare への移行（実行手順と状態）

目的は **Vercel と Supabase の解約**。構成は [target-architecture.md](target-architecture.md) の方針どおり、Cloud Run の FastAPI（エージェント）は残し、
データと画面と認証を Cloudflare（Free プラン）へ移す。決定の理由は E1〜E7 の検証（Python Workers にはエージェントを載せられない）。

| 役割 | 移行前 | 移行後 |
|---|---|---|
| 画面 | Vercel（Next.js） | Workers `docunavi-web`（OpenNext。`frontend/`） |
| 認証 | Supabase Auth（Google） | Better Auth（Google）。データ用 Worker の `/api/auth/*`。画面と同じオリジンへ中継 |
| データ | Supabase Postgres（pgvector） | D1（本文・メタデータ・履歴）+ Vectorize（ベクトル）+ R2（画像・PDF） |
| 入口 | FastAPI（Cloud Run） | 同じ。DB の代わりにデータ用 Worker（`workers/api/`）を HTTPS で呼ぶ |

## 作ったもの（2026-11-08）

- Cloudflare（アカウント Starlight）: D1 `docunavi`、Vectorize `docunavi-chunks`（768 次元・cosine。メタデータ索引 collection / target / document_id）。
- `workers/api/`: Hono + Better Auth。`/v1/sql`（D1）、`/v1/vectors/*`（Vectorize）はサービス用トークン（`DATA_API_TOKEN`）が要る。
  `/api/auth/*` はブラウザー用（Google ログイン、`/token` が 15 分の JWT、`/jwks`）。
- `app/d1.py` ほか: FastAPI 側の DB 層。環境変数は `D1_API_URL` `D1_API_TOKEN`。認証は `AUTH_BASE_URL` `AUTH_AUDIENCE`（`AUTH_JWKS_URL` は任意）。
- `scripts/migrate_supabase_to_d1.py`: 一度きりのデータ移行（Supabase は読み取りのみ）。

## 手順（上から順に）

1. **Google の OAuth クライアント**（ホスト・画面の手順。API では作れない）: <https://console.cloud.google.com/apis/credentials> で、承認済みのリダイレクト URI に
   `https://docunavi-web.wataru-apps.workers.dev/api/auth/callback/google` を足す。クライアント ID とシークレットを
   `workers/api/.secrets/GOOGLE_CLIENT_ID` と `GOOGLE_CLIENT_SECRET` に置く（`deploy.sh` が聞くこともできる）。
2. **データ用 Worker のデプロイ**（ホスト）: `cd ~/projects/docunavi/workers/api && ./deploy.sh`（初回は `wrangler login`。D1 の migration、デプロイ、秘密の登録まで）。
3. **データ移行**（開発コンテナ）: `scripts/migrate_supabase_to_d1.py --apply`（`D1_API_URL` と `D1_API_TOKEN` を渡す）。件数と `chunks.content_hash` の照合まで行う。
4. **画面のデプロイ**（ホスト）: `cd ~/projects/docunavi/frontend && ./deploy-web.sh`。
5. **Cloud Run を新しい構成へ**（本番の書き込み。操作ごとに承認）: Secret `d1-api-token` を作る → `gcloud builds submit` → 0% のタグ付きリビジョンに、
   `D1_API_URL` `D1_API_TOKEN`（Secret）`AUTH_BASE_URL`（画面のオリジン）`AUTH_JWKS_URL` `AUTH_AUDIENCE=docunavi` を足し、`DATABASE_URL` `SUPABASE_URL` を外し、
   `CORS_ORIGINS` に画面のオリジンを足す。確認してから 100% に移す（[deploy.md](deploy.md) の手順）。
6. **実アプリの確認**: 表示（資料・分類）、ログイン、チャット（実 API・課金。質問数と費用を示して承認）、会話の履歴の保存と再開、資料の PDF・ヒーロー画像。
7. **解約**（承認が要る）: Vercel のプロジェクトと Supabase のプロジェクト。解約の前に、Supabase の最終ダンプを保管する。

## 制約と既知のこと

- Workers Free: 1 リクエストの CPU 時間 10 ms、サブリクエスト 50。コーパスの読み込みはページ分け（250 行）にしてある。実機の CPU 時間は未確認。
- Vectorize はメタデータだけの更新ができない。資料の `collection` / `target` を後から変えたら、そのチャンクの `embedding_model` を NULL に戻して
  `python -m app.ingestion.embed --apply` を実行する（再埋め込みの費用は小さい）。
- 会話履歴は移さない（捨てる方針）。ユーザーは Better Auth で作り直す（初回ログインで作られる）。`user_roles` は 0 件だった。
- ロールの割り当ては、いまは SQL（`workers/api` の D1）で行う。Django の管理画面（`ops/access_admin/`）は 2026-11-08 に廃止した。専用の CLI は未作成。
- 評価用のスクリプト（`scripts/eval_answers.py` `eval_vector.py`）は Postgres 前提のまま。
