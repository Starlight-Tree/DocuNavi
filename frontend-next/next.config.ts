import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  // 開発サーバーを 127.0.0.1 で開くと、既定ではブラウザーへの資産の配信が止まる（空白の画面になる）
  allowedDevOrigins: ['127.0.0.1'],
  // `next dev` が AGENTS.md を自動で作るのを止める
  agentRules: false,
};

export default nextConfig;
