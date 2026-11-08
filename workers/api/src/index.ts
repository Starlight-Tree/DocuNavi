import { Hono } from 'hono';
import { betterAuth } from 'better-auth';
import { jwt } from 'better-auth/plugins';
import { Pool } from 'pg';

export interface Env {
  // Better Auth の保存先は Postgres（Hyperdrive 経由）。docs/architecture.md
  HYPERDRIVE: Hyperdrive;
  BETTER_AUTH_SECRET: string;
  BETTER_AUTH_URL: string;
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  JWT_AUDIENCE?: string;
}

const app = new Hono<{ Bindings: Env }>();

function auth(env: Env, pool: Pool) {
  return betterAuth({
    database: pool,
    baseURL: env.BETTER_AUTH_URL,
    secret: env.BETTER_AUTH_SECRET,
    socialProviders: { google: { clientId: env.GOOGLE_CLIENT_ID, clientSecret: env.GOOGLE_CLIENT_SECRET } },
    plugins: [
      jwt({
        jwks: { keyPairConfig: { alg: 'ES256' } },
        jwt: { issuer: env.BETTER_AUTH_URL, audience: env.JWT_AUDIENCE ?? 'docunavi', expirationTime: '15m' },
      }),
    ],
  });
}

app.get('/health', (c) => c.json({ ok: true }));

// Browser-facing: Better Auth (Google sign-in, session, JWT/JWKS). The frontend proxies /api/auth/* here.
app.on(['GET', 'POST'], '/api/auth/*', async (c) => {
  // リクエストごとに接続を作り、応答のあとで閉じる（Hyperdrive が裏でプールする）
  const pool = new Pool({ connectionString: c.env.HYPERDRIVE.connectionString, max: 1 });
  try {
    return await auth(c.env, pool).handler(c.req.raw);
  } finally {
    c.executionCtx.waitUntil(pool.end());
  }
});

export default app;
