import type { NextConfig } from 'next';

// 設定すると、API のパスを、このオリジンから FastAPI へ rewrites で中継する（NEXT_PUBLIC_API_BASE_URL は空にする）。
// 未設定なら中継しない（ブラウザーが NEXT_PUBLIC_API_BASE_URL の API を直接呼ぶ）。
const apiProxyTarget = process.env.API_PROXY_TARGET?.replace(/\/+$/, '');
// 認証の API（データ用 Worker の /api/auth/*）を、この画面と同じオリジンから使えるようにする中継先（Cookie を同じオリジンに保つため）。
// 未設定なら中継しない（NEXT_PUBLIC_AUTH_BASE_URL で別のオリジンを直接指す）
const authProxyTarget = process.env.AUTH_PROXY_TARGET?.replace(/\/+$/, '');
// PDF の中継（app/api/documents/[id]/file/route.ts。ADR 0013）が API（Cloud Run）を呼ぶ先。API_PROXY_TARGET は使わない（設定すると rewrites が全部この Worker に向く）。
// 未設定なら、その中継は 404 を返す（認証なしの構成では、画面が API を直接指す）
const fileProxyTarget = process.env.FILE_PROXY_TARGET?.replace(/\/+$/, '');

const nextConfig: NextConfig = {
  // route.ts がビルド時に中継先を読めるようにする（Workers の実行時には環境変数が無い）
  env: { AUTH_PROXY_TARGET: authProxyTarget ?? '', FILE_PROXY_TARGET: fileProxyTarget ?? '' },
  // E2E が、認証なし・認証ありの 2 つのビルドを別のフォルダーに作って同時に立てるための切り替え（既定は .next）
  distDir: process.env.NEXT_DIST_DIR || '.next',
  // 開発サーバーを 127.0.0.1 で開くと、既定ではブラウザーへの資産の配信が止まる（空白の画面になる）
  allowedDevOrigins: ['127.0.0.1'],
  // `next dev` が AGENTS.md を自動で作るのを止める
  agentRules: false,
  // `next dev` の左下の「N」のアイコンが、画面の左下（モックのアイコンなど）に重なるので出さない（開発時だけ。本番には影響しない。エラーの表示は残る）
  devIndicators: false,
  // UI モック（/mock）は、UI_MOCK=1 で起動・ビルドしたときだけ開く。本番（Worker）では設定しないので、/ へ戻す
  async redirects() {
    if (process.env.UI_MOCK === '1') return [];
    return [{ source: '/mock/:path*', destination: '/', permanent: false }];
  },
  async rewrites() {
    // /api/auth/* は rewrites ではなく app/api/auth/[...all]/route.ts が中継する（rewrites は 302 をたどって Set-Cookie を捨てる）
    if (!apiProxyTarget) return [];
    const api = ['/health', '/chat', '/chat/stream', '/threads', '/threads/:path*', '/documents', '/documents/:path*', '/collections', '/targets', '/doc-types'].map(
      (source) => ({ source, destination: `${apiProxyTarget}${source}` }),
    );
    return api;
  },
};

export default nextConfig;
