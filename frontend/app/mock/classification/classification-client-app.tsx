'use client';

import dynamic from 'next/dynamic';

// 「対象」「資料種別」の UI モック（架空データだけで動く）。本体の画面（Root）とは別の入口で、通信も認証も使わない
const ClassificationMock = dynamic(() => import('../../../src/mock/classification/ClassificationMock'), { ssr: false });

export default function ClassificationClientApp() {
  return <ClassificationMock />;
}
