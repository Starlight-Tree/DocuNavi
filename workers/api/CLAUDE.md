# workers/api/（認証 Worker `docunavi-api`）

全体の方針・禁止事項は、ルートの [CLAUDE.md](../../CLAUDE.md)。経緯は [docs/architecture.md](../../docs/architecture.md)、デプロイは [docs/deploy.md](../../docs/deploy.md)。

Better Auth（ログイン・JWT 発行）を担う Cloudflare Worker。本番の認証を持つので、変更は小さく、検査してから出す。

- 構成: `src/index.ts`（Worker の入口）、`src/auth.ts`（Better Auth の設定）、`src/authConfig.ts`（設定値の検査。`BETTER_AUTH_SECRET` などが無い・公開の既定値なら、`/api/auth/*` は 500 を返して動かさない。テストは `authConfig.test.ts`）、`src/displayName.ts`、`src/node.ts`（Node 用の起動。`Dockerfile.node` と `npm run build:node`）。設定は `wrangler.jsonc`（名前 `docunavi-api`、`HYPERDRIVE` の束縛、公開してよい `vars` だけ）。

## 守ること

- **秘密を書かない。** `BETTER_AUTH_SECRET`・`GOOGLE_CLIENT_ID`・`GOOGLE_CLIENT_SECRET` は、`wrangler.jsonc`・コード・文書・コミットに載せない（`vars` に入れるのは公開してよい値だけ）。`.dev.vars` と `.secrets/` は Git 管理外。`.dev.vars` は手で書かず、`scripts/dev-local.sh start|vars` で作る（Infisical `docunavi-local` から。値は表示しない）。**これらのファイルを読まない**（`.claude/settings.json` の `Read` の拒否対象）。
- **配備・Secret の登録をしない。** `wrangler deploy`・`wrangler secret put`・`deploy.sh` は、ホストでユーザーが行う（`deploy.sh` は、ホスト側の配備用。開発コンテナには Cloudflare の資格情報が無い）。本番への反映は、手動の `deploy.yml`（ユーザーの明示の承認を得てから）。フック `.claude/hooks/guard-deploy-cli.py` が、wrangler・infisical・prisma の書き込み系を確認に回す。止められたら回避しない。
- **JWT の発行者・Audience・鍵（`/api/auth/jwks`）の形を変えない。** API（FastAPI）がこれで検証する。変えるときは、API と画面の中継（`frontend/app/api/auth/[...all]/route.ts`）への影響を示して、承認を得る。
- **本番と同じ Google Secret をローカルが共用する。** 更新は両方（Infisical `docunavi-local` と `docunavi-prod`）と実行環境をそろえる（ルートの CLAUDE.md の「セキュリティの運用」）。

## 検査（変更したら実行する）

```bash
cd workers/api
npm run typecheck        # tsc --noEmit（本体と Node 用の 2 つ）
npm test                 # node --test（設定値の検査。依存は足さない）
```

ローカルで動かすときは `npm run dev`（`wrangler dev --local --port 8787`。ふだんは `scripts/dev-local.sh start` が起動する）。まとめて検査するときは `scripts/verify.sh worker`。
