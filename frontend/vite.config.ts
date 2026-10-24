import react from '@vitejs/plugin-react';
import { loadEnv } from 'vite';
import { defineConfig } from 'vitest/config';

// 開発時のプロキシ先。シェルの環境変数を優先し、無ければ .env / .env.local から読む。
function resolveProxyTarget(mode: string): string {
  const fromFiles = loadEnv(mode, process.cwd(), 'VITE_').VITE_DEV_PROXY_TARGET;
  return process.env.VITE_DEV_PROXY_TARGET || fromFiles || 'http://localhost:8000';
}

export default defineConfig(({ mode }) => {
  const target = resolveProxyTarget(mode);
  const proxy = { target, changeOrigin: true };

  return {
    plugins: [react()],
    // 本番は FastAPI が dist/ を `/` から配信する想定なので、資産パスは絶対パスのルート基準にする
    base: '/',
    build: {
      outDir: 'dist',
      sourcemap: false,
    },
    server: {
      // 既定は手元だけ（開発コンテナのポート転送もこれで届く）。Compose のコンテナ内では
      // 外から届くよう VITE_DEV_HOST=0.0.0.0 を渡す（docker-compose.yml の frontend）
      host: process.env.VITE_DEV_HOST || '127.0.0.1',
      // frontend-next の rewrites が開発コンテナから Compose の frontend へ中継するときの Host（DNS リバインディング対策の許可リスト）
      allowedHosts: ['host.docker.internal'],
      proxy: {
        '/chat': proxy,
        '/health': proxy,
        '/documents': proxy,
        '/threads': proxy,
      },
    },
    preview: {
      proxy: {
        '/chat': proxy,
        '/health': proxy,
        '/documents': proxy,
        '/threads': proxy,
      },
    },
    test: {
      environment: 'jsdom',
      setupFiles: ['./src/test/setup.ts'],
      css: false,
      restoreMocks: true,
      unstubGlobals: true,
      unstubEnvs: true,
    },
  };
});
