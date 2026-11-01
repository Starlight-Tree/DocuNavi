import { describe, expect, it, vi } from 'vitest';
import { fetchDocumentHero, fetchDocuments } from './documents';

const summary = { id: 3, source: 'a.pdf', status: 'ready', chunk_count: 1, embedded_count: 1 };

function respond(body: BodyInit | null, contentType: string | null): typeof fetch {
  return vi.fn(async () => new Response(body, { status: 200, headers: contentType ? { 'Content-Type': contentType } : {} })) as never;
}

describe('fetchDocumentHero', () => {
  it('画像の応答を Blob で返す（GET /documents/{id}/hero）', async () => {
    const fetcher = respond(new Uint8Array([1, 2, 3]), 'image/png');

    const blob = await fetchDocumentHero(fetcher as never, 3);

    expect(blob.size).toBe(3);
    expect(vi.mocked(fetcher).mock.calls[0]?.[0]).toMatch(/\/documents\/3\/hero$/);
  });

  it.each(['text/html', 'application/json', 'image/svg+xml', null])('画像でない応答（%s）は読めない応答として扱う', async (type) => {
    await expect(fetchDocumentHero(respond('x', type) as never, 3)).rejects.toThrow();
  });
});

describe('fetchDocuments の heroImage', () => {
  it('hero_image が true の資料だけ true。項目が無い（古いサーバー）・true 以外は false', async () => {
    const body = JSON.stringify({
      configured: true,
      documents: [{ ...summary, hero_image: true }, { ...summary, id: 4 }, { ...summary, id: 5, hero_image: 'yes' }],
    });

    const list = await fetchDocuments(respond(body, 'application/json') as never);

    expect(list.documents.map((d) => d.heroImage)).toEqual([true, false, false]);
  });
});
