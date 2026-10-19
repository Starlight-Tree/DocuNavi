import { existsSync } from 'node:fs';
import { defineConfig, devices } from '@playwright/test';

// E2E は、偽の LLM・偽の検索で立てた本物のアプリ（tests/e2e_server.py）に、実ブラウザーでつなぐ。実 API は使わない。
const PORT = Number(process.env.E2E_PORT ?? 8100);
const BASE_URL = `http://127.0.0.1:${PORT}`;
// 開発コンテナはリポジトリ直下の .venv、CI などは PATH の python。E2E_PYTHON で上書きできる
const PYTHON =
  process.env.E2E_PYTHON ?? (existsSync('../.venv/bin/python') ? '../.venv/bin/python' : 'python');

export default defineConfig({
  testDir: './e2e',
  testMatch: '**/*.e2e.ts', // vitest の既定の対象（*.test.* / *.spec.*）と重ならない名前
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list'], ['html', { open: 'never' }]],
  use: {
    baseURL: BASE_URL,
    trace: 'retain-on-failure',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    // 画面を E2E 用にビルドしてから（VITE_SUPABASE_* を空にして、ログイン画面が出ない構成にする）、サーバーを立てる
    command: `npm run build:e2e && ${PYTHON} ../tests/e2e_server.py`,
    env: { E2E_PORT: String(PORT) },
    url: `${BASE_URL}/health`,
    reuseExistingServer: false, // 別物（古いコードのサーバー）を見ないよう、使い回さない
    timeout: 120_000,
  },
});
