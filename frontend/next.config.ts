import type { NextConfig } from 'next';

// 設定すると、API のパスを、このオリジンから FastAPI へ rewrites で中継する（NEXT_PUBLIC_API_BASE_URL は空にする）。
// 未設定なら中継しない（ブラウザーが NEXT_PUBLIC_API_BASE_URL の API を直接呼ぶ）。
const apiProxyTarget = process.env.API_PROXY_TARGET?.replace(/\/+$/, '');

const nextConfig: NextConfig = {
  // E2E が、認証なし・認証ありの 2 つのビルドを別のフォルダーに作って同時に立てるための切り替え（既定は .next）
  distDir: process.env.NEXT_DIST_DIR || '.next',
  // 開発サーバーを 127.0.0.1 で開くと、既定ではブラウザーへの資産の配信が止まる（空白の画面になる）
  allowedDevOrigins: ['127.0.0.1'],
  // `next dev` が AGENTS.md を自動で作るのを止める
  agentRules: false,
  async rewrites() {
    if (!apiProxyTarget) return [];
    return ['/health', '/chat', '/chat/stream', '/threads', '/threads/:path*', '/documents', '/documents/:path*', '/collections'].map(
      (source) => ({ source, destination: `${apiProxyTarget}${source}` }),
    );
  },
};

export default nextConfig;
