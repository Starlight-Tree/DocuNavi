import { describe, expect, it, vi } from 'vitest';
import { fetchDocuments } from './documents';

const summary = { id: 3, source: 'a.pdf', status: 'ready', chunk_count: 1, embedded_count: 1 };

function respond(body: BodyInit | null, contentType: string | null): typeof fetch {
  return vi.fn(async () => new Response(body, { status: 200, headers: contentType ? { 'Content-Type': contentType } : {} })) as never;
}

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
