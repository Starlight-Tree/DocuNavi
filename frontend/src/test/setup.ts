import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';

afterEach(() => {
  cleanup();
  try {
    window.sessionStorage.clear();
  } catch {
    // ストレージを塞ぐテストの後始末では例外になりうる
  }
});
