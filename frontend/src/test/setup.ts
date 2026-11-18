import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeEach, vi } from 'vitest';
import { installStageWidth, resetFakePdf } from './fakePdf';

// PDF.js（canvas・Worker が要る）との境界は、全テストで差し替える。実際の描画は、実ブラウザーで確かめる
vi.mock('../api/pdfjs', async () => ({ openPdf: (await import('./fakePdf')).openPdf }));
installStageWidth();

// 既定の画面は資料一覧だが、テストの大半はチャットを扱うので、先頭の URL はチャットにする（既定を確かめるテストは '/' に置き直す）
const START_URL = '/?tab=chat';

beforeEach(() => {
  window.history.replaceState(null, '', START_URL);
  resetFakePdf();
});

afterEach(() => {
  cleanup();
  // 画面の状態は URL が持つので、テストごとに先頭の URL に戻す（前のテストの画面を引き継がない）
  window.history.replaceState(null, '', START_URL);
  try {
    window.sessionStorage.clear();
  } catch {
    // ストレージを塞ぐテストの後始末では例外になりうる
  }
});
