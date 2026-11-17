// Better Auth を Node（Cloud Run）で動かす入口。Cloud SQL / AlloyDB 版スタック `docunavi-gcp-auth` 用。
// Worker（index.ts）と同じ設定（auth.ts の createAuth）を使い、違いは接続だけ:
//   - Pool は起動時に 1 つ作って使い回す（Worker はリクエストごと）。waitUntil は無い。
//   - TLS は DATABASE_CA_FILE の CA で証明書の署名を検証する（verify-ca 相当。接続先が IP なのでホスト名は見ない）。
//   - DATABASE_URL は postgresql://user:password@host:5432/db の形だけ（問い合わせ文字列は付けない。付いていたら起動を失敗させる）。
// 値はすべて環境変数（Secret Manager から渡す）。足りなければ、値を出さずに、変数名だけ示して起動を失敗させる。
import { readFileSync } from 'node:fs';
import { serve } from '@hono/node-server';
import { Hono } from 'hono';
import pg from 'pg';
import { createAuth, type AuthConfig } from './auth';

// pg は CommonJS なので、ESM からは既定の import で受ける
const { Pool } = pg;

function required(name: string): string {
  const value = (process.env[name] ?? '').trim();
  if (value === '') {
    console.error(`missing environment variable: ${name}`);
    process.exit(1);
  }
  return value;
}

const config: AuthConfig = {
  BETTER_AUTH_SECRET: required('BETTER_AUTH_SECRET'),
  BETTER_AUTH_URL: required('BETTER_AUTH_URL'),
  GOOGLE_CLIENT_ID: required('GOOGLE_CLIENT_ID'),
  GOOGLE_CLIENT_SECRET: required('GOOGLE_CLIENT_SECRET'),
  JWT_AUDIENCE: process.env.JWT_AUDIENCE,
  TEST_LOGIN_TOKEN: process.env.TEST_LOGIN_TOKEN,
  TEST_LOGIN_EMAIL: process.env.TEST_LOGIN_EMAIL,
};

const databaseUrl = required('DATABASE_URL');
if (new URL(databaseUrl).search !== '') {
  console.error('DATABASE_URL must not have query parameters (TLS is set by DATABASE_CA_FILE)');
  process.exit(1);
}

const pool = new Pool({
  connectionString: databaseUrl,
  ssl: {
    ca: readFileSync(required('DATABASE_CA_FILE'), 'utf8'),
    rejectUnauthorized: true,
    // 接続先は IP（PSC）。署名の検証（verify-ca）だけを行い、ホスト名は見ない
    checkServerIdentity: () => undefined,
  },
  max: Number(process.env.DATABASE_POOL_MAX_SIZE ?? '2'),
  // AlloyDB Express は停止から再開するまで待つことがある
  connectionTimeoutMillis: 30_000,
  idleTimeoutMillis: 30_000,
});
// 接続が切れても（DB の自動停止など）プロセスを落とさない。次のクエリで張り直す。本文は出さない
pool.on('error', (err) => console.error(`database pool error: ${err.name}`));

const auth = createAuth(config, pool);
const app = new Hono();

app.get('/health', (c) => c.json({ ok: true }));
app.on(['GET', 'POST'], '/api/auth/*', (c) => auth.handler(c.req.raw));

const server = serve({ fetch: app.fetch, port: Number(process.env.PORT ?? '8080') });

process.on('SIGTERM', () => {
  server.close(() => {
    void pool.end().finally(() => process.exit(0));
  });
});
