import { Hono } from 'hono';
import { betterAuth } from 'better-auth';
import { jwt } from 'better-auth/plugins';

export interface Env {
  DB: D1Database;
  VECTORS: VectorizeIndex;
  DATA_API_TOKEN: string;
  BETTER_AUTH_SECRET: string;
  BETTER_AUTH_URL: string;
  GOOGLE_CLIENT_ID: string;
  GOOGLE_CLIENT_SECRET: string;
  JWT_AUDIENCE?: string;
}

const app = new Hono<{ Bindings: Env }>();

function safeEqual(a: string, b: string): boolean {
  const ea = new TextEncoder().encode(a);
  const eb = new TextEncoder().encode(b);
  if (ea.length !== eb.length) return false;
  let diff = 0;
  for (let i = 0; i < ea.length; i++) diff |= ea[i] ^ eb[i];
  return diff === 0;
}

function auth(env: Env) {
  return betterAuth({
    database: env.DB,
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
app.on(['GET', 'POST'], '/api/auth/*', (c) => auth(c.env).handler(c.req.raw));

// Service-facing (Cloud Run -> Worker): bearer token.
const v1 = new Hono<{ Bindings: Env }>();
v1.use('*', async (c, next) => {
  const m = /^Bearer (.+)$/.exec(c.req.header('authorization') ?? '');
  if (!c.env.DATA_API_TOKEN || !m || !safeEqual(m[1], c.env.DATA_API_TOKEN)) return c.json({ error: 'unauthorized' }, 401);
  await next();
});

type Stmt = { sql: string; params?: unknown[] };

// SQL passthrough to D1 (SQLite dialect). Several statements run as one transaction via batch().
v1.post('/sql', async (c) => {
  const body = await c.req.json<{ statements: Stmt[] }>();
  const list = body.statements;
  if (!Array.isArray(list) || list.length === 0 || list.length > 100) return c.json({ error: 'bad statements' }, 400);
  const prepared = list.map((s) => c.env.DB.prepare(s.sql).bind(...(s.params ?? [])));
  try {
    const results = list.length === 1 ? [await prepared[0].all()] : await c.env.DB.batch(prepared);
    return c.json({ results: results.map((r) => ({ rows: r.results ?? [], meta: r.meta })) });
  } catch (e) {
    return c.json({ error: String((e as Error).message ?? e).slice(0, 300) }, 400);
  }
});

type VecIn = { id: string; values: number[]; metadata?: Record<string, string | number | boolean> };

v1.post('/vectors/upsert', async (c) => {
  const { vectors } = await c.req.json<{ vectors: VecIn[] }>();
  if (!Array.isArray(vectors) || vectors.length === 0 || vectors.length > 500) return c.json({ error: 'bad vectors' }, 400);
  const r = await c.env.VECTORS.upsert(vectors);
  return c.json({ count: r.count });
});

v1.post('/vectors/delete', async (c) => {
  const { ids } = await c.req.json<{ ids: string[] }>();
  if (!Array.isArray(ids) || ids.length === 0 || ids.length > 1000) return c.json({ error: 'bad ids' }, 400);
  const r = await c.env.VECTORS.deleteByIds(ids);
  return c.json({ count: r.count });
});

v1.post('/vectors/query', async (c) => {
  const { vector, topK, filter } = await c.req.json<{ vector: number[]; topK: number; filter?: Record<string, unknown> }>();
  if (!Array.isArray(vector) || vector.length !== 768) return c.json({ error: 'bad vector' }, 400);
  const k = Math.min(Math.max(Math.trunc(topK || 10), 1), 100);
  const r = await c.env.VECTORS.query(vector, { topK: k, returnValues: false, returnMetadata: 'none', filter: filter as VectorizeVectorMetadataFilter });
  return c.json({ matches: r.matches.map((m) => ({ id: m.id, score: m.score })) });
});

app.route('/v1', v1);

export default app;
