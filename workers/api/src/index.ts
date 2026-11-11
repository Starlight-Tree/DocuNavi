import { Hono } from 'hono';
import { betterAuth } from 'better-auth';
import { APIError, createAuthMiddleware } from 'better-auth/api';
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
  // 本番の自動テスト用ログイン（メール + パスワード）。**既定は無効**。TEST_LOGIN_TOKEN（Secret。32 文字以上）が設定されたときだけ有効になり、
  // 外すと無効に戻る。有効な間も、TEST_LOGIN_EMAIL の 1 アドレスだけが、ヘッダー x-test-login-token が TEST_LOGIN_TOKEN と一致するときだけ
  // 登録・ログインできる（一般の登録は開かない）。使い方は docs/development.md の「本番の /chat の自動テスト」。
  TEST_LOGIN_TOKEN?: string;
  TEST_LOGIN_EMAIL?: string;
}

const TEST_LOGIN_MIN_TOKEN = 32;

/** 自動テスト用ログインが有効なときだけ、許可するメールアドレス（小文字）を返す。無効なら null。 */
export function testLoginEmail(env: Pick<Env, 'TEST_LOGIN_TOKEN' | 'TEST_LOGIN_EMAIL'>): string | null {
  const token = env.TEST_LOGIN_TOKEN ?? '';
  const email = (env.TEST_LOGIN_EMAIL ?? '').trim().toLowerCase();
  return token.length >= TEST_LOGIN_MIN_TOKEN && email !== '' ? email : null;
}

/** 長さの違いも含めて、時間が内容に依らない比較。 */
function sameSecret(a: string, b: string): boolean {
  let diff = a.length ^ b.length;
  for (let i = 0; i < Math.max(a.length, b.length); i++) diff |= (a.charCodeAt(i) || 0) ^ (b.charCodeAt(i) || 0);
  return diff === 0;
}

const app = new Hono<{ Bindings: Env }>();

function auth(env: Env, pool: Pool) {
  const allowedEmail = testLoginEmail(env);
  return betterAuth({
    database: pool,
    baseURL: env.BETTER_AUTH_URL,
    secret: env.BETTER_AUTH_SECRET,
    socialProviders: { google: { clientId: env.GOOGLE_CLIENT_ID, clientSecret: env.GOOGLE_CLIENT_SECRET } },
    // セッションの確認（get-session・/token）を、5 分間は Cookie の署名つきの写しで済ませ、DB を引かない（Prisma Postgres の operations を減らす）。
    // 公式の既定の方式（compact。BETTER_AUTH_SECRET で署名）と、公式の既定の 5 分。サーバー側でセッションを失効させても、最大 5 分は有効に見える
    // （利用者自身のログアウトは Cookie も消すので、すぐ効く）。refreshCache は使わない（DB に確認しないまま延び続けるため）。
    session: { cookieCache: { enabled: true, maxAge: 5 * 60 } },
    // 既定は無効（Google ログインだけ）。自動テスト用の 1 アドレスのときだけ有効にする（上の Env の説明）
    emailAndPassword: { enabled: allowedEmail !== null, autoSignIn: false, minPasswordLength: 24, maxPasswordLength: 128 },
    hooks: {
      before: createAuthMiddleware(async (ctx) => {
        if (allowedEmail === null || (ctx.path !== '/sign-up/email' && ctx.path !== '/sign-in/email')) return;
        const body = (ctx.body ?? {}) as { email?: unknown };
        const email = typeof body.email === 'string' ? body.email.trim().toLowerCase() : '';
        const presented = ctx.headers?.get('x-test-login-token') ?? '';
        if (email !== allowedEmail || !sameSecret(presented, env.TEST_LOGIN_TOKEN ?? '')) {
          throw new APIError('FORBIDDEN', { message: 'forbidden' });
        }
      }),
    },
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
