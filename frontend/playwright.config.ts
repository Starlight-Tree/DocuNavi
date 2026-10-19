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

// 実 API の検証（`npm run test:e2e:real-api`）。E2E_REAL_API=1 のときだけ、この構成になる（通常の test:e2e には現れない）。
// 本物の Gemini を呼ぶ（課金）ので、通常の E2E・CI とは完全に分ける。サーバーは tests/real_api_server.py（127.0.0.1:8102）
const REAL_API = process.env.E2E_REAL_API === '1';
const REAL_API_PORT = Number(process.env.E2E_REAL_API_PORT ?? 8102);
const REAL_API_BASE_URL = `http://127.0.0.1:${REAL_API_PORT}`;

const realApiConfig = defineConfig({
  testDir: './e2e/real-api',
  fullyParallel: false,
  workers: 1,
  retries: 0, // 再実行は課金が増えるので、しない
  reporter: [['list']],
  projects: [{ name: 'real-api', testMatch: '*.e2e.ts', use: { baseURL: REAL_API_BASE_URL } }],
  webServer: {
    command: `${PYTHON} ../tests/real_api_server.py`,
    env: { E2E_REAL_API_PORT: String(REAL_API_PORT) },
    url: `${REAL_API_BASE_URL}/health`,
    reuseExistingServer: false,
    timeout: 60_000,
  },
});

// 2 つのアプリを立てる: 8100 = ログインを使わない画面（VITE_SUPABASE_* が空のビルド）、
// 8101 = 偽の Supabase の URL を入れたビルド（ログイン画面が出る。通信は e2e/support/supabase-mock.ts が差し替える）
const mockConfig = defineConfig({
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

export default REAL_API ? realApiConfig : mockConfig;
