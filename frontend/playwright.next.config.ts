import { existsSync } from 'node:fs';
import { defineConfig, devices } from '@playwright/test';

// frontend-next（`next build` + `next start`）に、frontend/e2e の同じシナリオを流す。実 API は使わない。
// 画面は Next（3100 = ログインなし、3101 = 偽の Supabase の URL を入れたビルド）が配り、
// API は偽の LLM・偽の検索の tests/e2e_server.py（8100）を、ブラウザーが直接呼ぶ（本番の Vercel → Cloud Run と同じ向き。CORS は 2 つの画面のオリジンだけ許す）。
// Vite 版を frontend-next に置き換えたら、このファイルと e2e/ は frontend-next に移す。
const API_PORT = Number(process.env.E2E_NEXT_API_PORT ?? 8100);
const PORT = Number(process.env.E2E_NEXT_PORT ?? 3100);
const AUTH_PORT = Number(process.env.E2E_NEXT_AUTH_PORT ?? 3101);
const PYTHON =
  process.env.E2E_PYTHON ?? (existsSync('../.venv/bin/python') ? '../.venv/bin/python' : 'python');

// frontend-next/package.json の build:e2e と NEXT_PUBLIC_API_BASE_URL（http://127.0.0.1:8100）が前提なので、API のポートだけ変えても届かない
if (API_PORT !== 8100) throw new Error('E2E_NEXT_API_PORT は 8100 のまま使う（Next のビルドに API の向き先を埋め込んでいるため）');

const NEXT_DIR = '../frontend-next';
const start = (distDir: string, port: number) =>
  `NEXT_DIST_DIR=${distDir} npx --prefix ${NEXT_DIR} next start ${NEXT_DIR} -H 127.0.0.1 -p ${port}`;

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  reporter: [['list']],
  use: { trace: 'retain-on-failure' },
  projects: [
    {
      name: 'next',
      testMatch: 'chat.e2e.ts',
      use: { ...devices['Desktop Chrome'], baseURL: `http://127.0.0.1:${PORT}` },
    },
    {
      name: 'next-auth',
      testMatch: 'auth.e2e.ts',
      use: { ...devices['Desktop Chrome'], baseURL: `http://127.0.0.1:${AUTH_PORT}` },
    },
  ],
  webServer: [
    {
      // e2e_server.py は frontend/dist-e2e を要求する（画面は配らないが、起動の検査がある）ので、先に作る
      command: `npm run build:e2e && ${PYTHON} ../tests/e2e_server.py`,
      // .env に CORS_ORIGINS があると既定の * にならない（既存の環境変数を load_dotenv は上書きしない）ので、画面のオリジンを明示する
      env: {
        E2E_PORT: String(API_PORT),
        CORS_ORIGINS: `http://127.0.0.1:${PORT},http://127.0.0.1:${AUTH_PORT}`,
      },
      url: `http://127.0.0.1:${API_PORT}/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      command: `npm --prefix ${NEXT_DIR} run build:e2e && ${start('.next-e2e', PORT)}`,
      url: `http://127.0.0.1:${PORT}/`,
      reuseExistingServer: false,
      timeout: 180_000,
    },
    {
      // webServer は配列の順に 1 つずつ立ち上がるので、同じプロジェクトのビルドが並ぶことはない（next-env.d.ts などを同時に書き換えない）
      command: `npm --prefix ${NEXT_DIR} run build:e2e:auth && ${start('.next-e2e-auth', AUTH_PORT)}`,
      url: `http://127.0.0.1:${AUTH_PORT}/`,
      reuseExistingServer: false,
      timeout: 180_000,
    },
  ],
});
