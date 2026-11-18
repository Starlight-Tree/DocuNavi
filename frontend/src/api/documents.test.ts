import { describe, expect, it } from 'vitest';
import { documentPdfUrl } from './documents';

describe('documentPdfUrl', () => {
  it('認証ありは同じオリジンの中継（保存名の元を ?name= で渡す）、認証なしは API をそのまま指す', () => {
    expect(documentPdfUrl(7, '規程 第1版.pdf', true)).toBe(`/api/documents/7/file?name=${encodeURIComponent('規程 第1版.pdf')}`);
    expect(documentPdfUrl(7, '規程.pdf', false)).toBe('/documents/7/file');
  });
});
