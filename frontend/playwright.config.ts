import { existsSync } from 'node:fs';
import { defineConfig, devices } from '@playwright/test';

// E2E は、偽の LLM・偽の検索で立てた本物のアプリ（tests/e2e_server.py）に、実ブラウザーでつなぐ。実 API は使わない。
const PORT = Number(process.env.E2E_PORT ?? 8100);
const BASE_URL = `http://127.0.0.1:${PORT}`;
// 開発コンテナはリポジトリ直下の .venv、CI などは PATH の python。E2E_PYTHON で上書きできる
const PYTHON =
  process.env.E2E_PYTHON ?? (existsSync('../.venv/bin/python') ? '../.venv/bin/python' : 'python');

const AUTH_PORT = Number(process.env.E2E_AUTH_PORT ?? 8101);
const AUTH_BASE_URL = `http://127.0.0.1:${AUTH_PORT}`;

// 2 つのアプリを立てる: 8100 = ログインを使わない画面（VITE_SUPABASE_* が空のビルド）、
// 8101 = 偽の Supabase の URL を入れたビルド（ログイン画面が出る。通信は e2e/support/supabase-mock.ts が差し替える）
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    trace: 'retain-on-failure',
  },
  projects: [
    {
      name: 'chromium',
      testMatch: 'chat.e2e.ts', // vitest の既定の対象（*.test.* / *.spec.*）と重ならない名前
      use: { ...devices['Desktop Chrome'], baseURL: BASE_URL },
    },
    {
      name: 'auth',
      testMatch: 'auth.e2e.ts',
      use: { ...devices['Desktop Chrome'], baseURL: AUTH_BASE_URL },
    },
  ],
  webServer: [
    {
      // 画面を E2E 用にビルドしてから（VITE_SUPABASE_* を空にして、ログイン画面が出ない構成にする）、サーバーを立てる
      command: `npm run build:e2e && ${PYTHON} ../tests/e2e_server.py`,
      env: { E2E_PORT: String(PORT) },
      url: `${BASE_URL}/health`,
      reuseExistingServer: false, // 別物（古いコードのサーバー）を見ないよう、使い回さない
      timeout: 120_000,
    },
    {
      command: `npm run build:e2e:auth && ${PYTHON} ../tests/e2e_server.py`,
      env: { E2E_PORT: String(AUTH_PORT), E2E_DIST: 'dist-e2e-auth' },
      url: `${AUTH_BASE_URL}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
