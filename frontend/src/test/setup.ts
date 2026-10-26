import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
  // 画面の状態は URL が持つので、テストごとに先頭の URL に戻す（前のテストの画面を引き継がない）
  window.history.replaceState(null, '', '/');
  try {
    window.sessionStorage.clear();
  } catch {
    // ストレージを塞ぐテストの後始末では例外になりうる
  }
});
