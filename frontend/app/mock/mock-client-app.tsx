'use client';

import dynamic from 'next/dynamic';

// UI モック（サンプルデータだけで動く）。本体の画面（Root）とは別の入口で、通信も認証も使わない
const MockApp = dynamic(() => import('../../src/mock/MockApp'), { ssr: false });

export default function MockClientApp() {
  return <MockApp />;
}
