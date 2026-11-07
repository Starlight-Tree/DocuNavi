import { existsSync } from 'node:fs';
import { defineConfig, devices } from '@playwright/test';

// E2E は、偽の LLM・偽の検索で立てた本物の API（tests/e2e_server.py）と、E2E 用にビルドした Next.js の画面を、別のポートで立てて、
// 実ブラウザーでつなぐ。画面と API が別オリジンなのは、本番（Vercel → Cloud Run）と同じ。実 API は使わない。
const API_PORT = Number(process.env.E2E_PORT ?? 8100);
const API_URL = `http://127.0.0.1:${API_PORT}`;
// 画面: 3100 = ログインを使わない画面（NEXT_PUBLIC_AUTH_ENABLED が空のビルド）、
// 3101 = ログインを有効にして、偽の認証サーバーの URL を入れたビルド（ログイン画面が出る。通信は e2e/support/auth-mock.ts が差し替える）
const WEB_PORT = Number(process.env.E2E_WEB_PORT ?? 3100);
const AUTH_WEB_PORT = Number(process.env.E2E_AUTH_WEB_PORT ?? 3101);
const WEB_URL = `http://127.0.0.1:${WEB_PORT}`;
const AUTH_WEB_URL = `http://127.0.0.1:${AUTH_WEB_PORT}`;
// 開発コンテナはリポジトリ直下の .venv、CI などは PATH の python。E2E_PYTHON で上書きできる
const PYTHON =
  process.env.E2E_PYTHON ?? (existsSync('../.venv/bin/python') ? '../.venv/bin/python' : 'python');

// 実 API の検証（`npm run test:e2e:real-api`）。E2E_REAL_API=1 のときだけ、この構成になる（通常の test:e2e には現れない）。
// 本物の Gemini を呼ぶ（課金）ので、通常の E2E・CI とは完全に分ける。サーバーは tests/real_api_server.py（127.0.0.1:8102）。画面は使わない
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

const mockConfig = defineConfig({
  testDir: './e2e',
  testIgnore: '**/real-api/**',
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
      use: { ...devices['Desktop Chrome'], baseURL: WEB_URL },
    },
    {
      name: 'auth',
      testMatch: 'auth.e2e.ts',
      use: { ...devices['Desktop Chrome'], baseURL: AUTH_WEB_URL },
    },
  ],
  webServer: [
    {
      // API だけ（画面は配信しない）。CORS は 2 つの画面のオリジンだけに許す
      command: `${PYTHON} ../tests/e2e_server.py`,
      env: { E2E_PORT: String(API_PORT), E2E_WEB_ORIGINS: `${WEB_URL},${AUTH_WEB_URL}` },
      url: `${API_URL}/health`,
      reuseExistingServer: false, // 別物（古いコードのサーバー）を見ないよう、使い回さない
      timeout: 60_000,
    },
    {
      // 2 つのビルドは、同じフォルダーで同時に走らせない（tsconfig.json などを書き換えるため）。順にビルドしてから、2 つの画面を立てる
      command:
        'npm run build:e2e && npm run build:e2e:auth && { npm run start:e2e:auth & exec npm run start:e2e; }',
      env: { E2E_WEB_PORT: String(WEB_PORT), E2E_AUTH_WEB_PORT: String(AUTH_WEB_PORT) },
      url: WEB_URL,
      reuseExistingServer: false,
      timeout: 240_000,
    },
  ],
});

export default REAL_API ? realApiConfig : mockConfig;
