import { afterEach, describe, expect, it, vi } from 'vitest';
import { fetchDocumentChunks, fetchDocuments } from './documents';
import { fetchCollections } from './collections';
import { fetchDocTypes, fetchTargets } from './classifications';
import { fetchThreads as fetchThreadHistoryList } from './threads';

afterEach(() => vi.unstubAllEnvs());

// 読み取り API（Go）へ向けるのは、一覧と資料の本文の GET だけ。呼び先の URL を確かめる
function recorder(body: unknown) {
  const urls: string[] = [];
  const fetcher = vi.fn(async (input: RequestInfo | URL) => {
    urls.push(String(input));
    return new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
  });
  return { urls, fetcher: fetcher as never };
}

describe('読み取りの経路は NEXT_PUBLIC_READ_BASE_URL を使う', () => {
  it('一覧と本文の GET は、読み取り API の URL を呼ぶ', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE_URL', 'https://api.example.test');
    vi.stubEnv('NEXT_PUBLIC_READ_BASE_URL', 'https://read.example.test');
    const a = recorder({ configured: true, collections: [] });
    await fetchCollections(a.fetcher);
    const b = recorder({ configured: true, targets: [] });
    await fetchTargets(b.fetcher);
    const c = recorder({ configured: true, doc_types: [] });
    await fetchDocTypes(c.fetcher);
    const d = recorder({ configured: true, documents: [] });
    await fetchDocuments(d.fetcher);
    const e = recorder({ document: { id: 3, source: 'a.pdf', status: 'ready', chunk_count: 0, embedded_count: 0 }, chunks: [], original_pdf: false });
    await fetchDocumentChunks(e.fetcher, 3);
    expect([a.urls[0], b.urls[0], c.urls[0], d.urls[0], e.urls[0]]).toEqual([
      'https://read.example.test/collections',
      'https://read.example.test/targets',
      'https://read.example.test/doc-types',
      'https://read.example.test/documents',
      'https://read.example.test/documents/3/chunks',
    ]);
  });

  it('会話の一覧は NEXT_PUBLIC_READ_THREADS_BASE_URL だけで切り替わる（READ_BASE_URL だけでは API のまま）', async () => {
    vi.stubEnv('NEXT_PUBLIC_API_BASE_URL', 'https://api.example.test');
    vi.stubEnv('NEXT_PUBLIC_READ_BASE_URL', 'https://read.example.test');
    const before = recorder({ threads: [] });
    await fetchThreadHistoryList(before.fetcher);
    expect(before.urls[0]).toBe('https://api.example.test/threads');
    vi.stubEnv('NEXT_PUBLIC_READ_THREADS_BASE_URL', 'https://read.example.test');
    const after = recorder({ threads: [] });
    await fetchThreadHistoryList(after.fetcher);
    expect(after.urls[0]).toBe('https://read.example.test/threads');
  });
});
