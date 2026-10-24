'use client';

import dynamic from 'next/dynamic';

// 画面は今までどおりブラウザーだけで描く（Vite 版と同じ SPA）。サーバー側の描画は、移行のあとで別に検討する
const Root = dynamic(() => import('../src/Root'), { ssr: false });

export default function ClientApp() {
  return <Root />;
}
