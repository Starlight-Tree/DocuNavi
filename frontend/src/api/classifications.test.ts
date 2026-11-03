import { describe, expect, it } from 'vitest';
import { ChatApiError, MSG_BAD_BODY } from './chat';
import { fetchDocTypes, fetchTargets } from './classifications';

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

const fetcherOf = (response: Response) => async () => response;

const item = (extra: object = {}) => ({
  name: 'zahard',
  label: 'ザーハード',
  description: '架空の惑星ザーハードの設定資料の対象です。',
  sort_order: 10,
  document_count: 3,
  ready_count: 2,
  ...extra,
});

describe('fetchTargets / fetchDocTypes', () => {
  it('対象の一覧を読む（並びはサーバーの順のまま）。呼ぶのは /targets', async () => {
    let url = '';
    const result = await fetchTargets(async (input) => {
      url = String(input);
      return json({ configured: true, targets: [item(), item({ name: 'minato-coffee', label: 'みなとコーヒー' })] });
    });

    expect(url).toMatch(/\/targets$/);
    expect(result.configured).toBe(true);
    expect(result.items.map((c) => c.name)).toEqual(['zahard', 'minato-coffee']);
    expect(result.items[0]).toEqual({
      name: 'zahard',
      label: 'ザーハード',
      description: '架空の惑星ザーハードの設定資料の対象です。',
      sortOrder: 10,
      documentCount: 3,
      readyCount: 2,
    });
  });

  it('資料種別の一覧は /doc-types の doc_types から読む', async () => {
    let url = '';
    const result = await fetchDocTypes(async (input) => {
      url = String(input);
      return json({ configured: true, doc_types: [item({ name: 'setting', label: '設定資料' })] });
    });

    expect(url).toMatch(/\/doc-types$/);
    expect(result.items[0]).toMatchObject({ name: 'setting', label: '設定資料' });
  });

  it('label・description・sort_order が無ければ、名前を表示名にし、説明は空、並び順は 0', async () => {
    const result = await fetchTargets(fetcherOf(json({ configured: true, targets: [item({ label: null, description: null, sort_order: null })] })));

    expect(result.items[0]).toMatchObject({ label: 'zahard', description: '', sortOrder: 0 });
  });

  it('DB を使っていない（configured:false）サーバーは、空の一覧として読む', async () => {
    expect(await fetchTargets(fetcherOf(json({ configured: false, targets: [] })))).toEqual({ configured: false, items: [] });
  });

  it.each([
    ['本文が JSON でない', new Response('<html>', { status: 200 })],
    ['configured が無い', json({ targets: [] })],
    ['targets が配列でない', json({ configured: true, targets: {} })],
    ['別のキー（doc_types）で返ってきた', json({ configured: true, doc_types: [] })],
    ['識別名の形が違う（大文字）', json({ configured: true, targets: [item({ name: 'Zahard' })] })],
    ['識別名の形が違う（空白）', json({ configured: true, targets: [item({ name: 'a b' })] })],
    ['識別名が無い', json({ configured: true, targets: [item({ name: undefined })] })],
    ['件数が負', json({ configured: true, targets: [item({ ready_count: -1 })] })],
    ['件数が整数でない', json({ configured: true, targets: [item({ document_count: 1.5 })] })],
    ['label が文字列でない', json({ configured: true, targets: [item({ label: 3 })] })],
  ])('形が合わなければ MSG_BAD_BODY: %s', async (_name, response) => {
    await expect(fetchTargets(fetcherOf(response))).rejects.toThrow(new ChatApiError(200, MSG_BAD_BODY));
  });
});
