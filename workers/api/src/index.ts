import { Hono } from 'hono';
import { Pool } from 'pg';
import { createAuth, type AuthConfig } from './auth';
import { authConfigProblem } from './authConfig';

export { testLoginEmail } from './auth';

export interface Env extends AuthConfig {
  // Better Auth の保存先は Postgres（Hyperdrive 経由）。docs/architecture.md
  HYPERDRIVE: Hyperdrive;
}

const app = new Hono<{ Bindings: Env }>();

app.get('/health', (c) => c.json({ ok: true }));

// Browser-facing: Better Auth (Google sign-in, session, JWT/JWKS). The frontend proxies /api/auth/* here.
app.on(['GET', 'POST'], '/api/auth/*', async (c) => {
  // Secret などの設定漏れは、既定の公開 Secret で動かさず、ここで止める（値は出さない。変数名だけをログへ）
  const problem = authConfigProblem(c.env);
  if (problem !== null) {
    console.error(`auth config: ${problem}`);
    return c.json({ error: 'auth_misconfigured' }, 500);
  }
  // リクエストごとに接続を作り、応答のあとで閉じる（Hyperdrive が裏でプールする）
  const pool = new Pool({ connectionString: c.env.HYPERDRIVE.connectionString, max: 1 });
  try {
    return await createAuth(c.env, pool).handler(c.req.raw);
  } finally {
    c.executionCtx.waitUntil(pool.end());
  }
});

export default app;
