import { describe, expect, it } from 'vitest';
import { ChatApiError, MSG_BAD_BODY } from './chat';
import { fetchCollections } from './collections';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const fetcherOf = (response: Response) => async () => response;

const item = (extra: object = {}) => ({
  name: 'zahard',
  label: '設定資料',
  description: 'ザーハードに関する設定資料です。',
  sort_order: 10,
  registered: true,
  document_count: 3,
  ready_count: 3,
  ...extra,
});

describe('fetchCollections', () => {
  it('グループの一覧を読む（並びはサーバーの順のまま）', async () => {
    const result = await fetchCollections(fetcherOf(json({ configured: true, collections: [item(), item({ name: 'cafe', label: '店舗案内' })] })));

    expect(result.configured).toBe(true);
    expect(result.collections.map((c) => c.name)).toEqual(['zahard', 'cafe']);
    expect(result.collections[0]).toEqual({
      name: 'zahard',
      label: '設定資料',
      description: 'ザーハードに関する設定資料です。',
      sortOrder: 10,
      registered: true,
      documentCount: 3,
      readyCount: 3,
    });
  });

  it('未登録のグループ（label・description・sort_order が無い）は、名前を表示名にし、説明は空', async () => {
    const result = await fetchCollections(
      fetcherOf(json({ configured: true, collections: [item({ name: 'memo', label: null, description: null, sort_order: null, registered: false })] })),
    );

    expect(result.collections[0]).toMatchObject({ name: 'memo', label: 'memo', description: '', sortOrder: null, registered: false });
  });

  it('DB を使っていない（configured:false）サーバーは、空の一覧として読む', async () => {
    expect(await fetchCollections(fetcherOf(json({ configured: false, collections: [] })))).toEqual({
      configured: false,
      collections: [],
    });
  });

  it.each([
    ['本文が JSON でない', new Response('<html>', { status: 200 })],
    ['configured が無い', json({ collections: [] })],
    ['collections が配列でない', json({ configured: true, collections: {} })],
    ['名前の形が違う', json({ configured: true, collections: [item({ name: 'a b' })] })],
    ['名前が無い', json({ configured: true, collections: [item({ name: undefined })] })],
    ['registered が真偽値でない', json({ configured: true, collections: [item({ registered: 'yes' })] })],
    ['件数が負', json({ configured: true, collections: [item({ ready_count: -1 })] })],
    ['件数が整数でない', json({ configured: true, collections: [item({ document_count: 1.5 })] })],
    ['label が文字列でない', json({ configured: true, collections: [item({ label: 3 })] })],
  ])('形が合わなければ MSG_BAD_BODY: %s', async (_name, response) => {
    await expect(fetchCollections(fetcherOf(response))).rejects.toThrow(new ChatApiError(200, MSG_BAD_BODY));
  });
});
