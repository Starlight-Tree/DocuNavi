import { describe, expect, it } from 'vitest';
import { fetchThreadMessages } from './threads';

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
const read = (body: unknown) => fetchThreadMessages(async () => json(body), 't1');

describe('fetchThreadMessages: 質問の範囲', () => {
  it('対象・旧グループ・利用できない旨を読む', async () => {
    expect(await read({ messages: [], collection: null, target: 'zahard' })).toMatchObject({ collection: null, target: 'zahard', scopeUnavailable: false });
    expect(await read({ messages: [], collection: 'cafe', target: null })).toMatchObject({ collection: 'cafe', target: null });
    expect(await read({ messages: [], collection: null, target: null, scope_unavailable: true })).toMatchObject({
      collection: null,
      target: null,
      scopeUnavailable: true,
    });
  });

  it('項目が無い応答（古い API・古い会話）は、絞らない・利用できる、として読む', async () => {
    expect(await read({ messages: [] })).toEqual({ messages: [], collection: null, target: null, scopeUnavailable: false });
  });

  it('形が合わない対象・旧グループは、主張しない（null）。scope_unavailable は true のときだけ真', async () => {
    expect(await read({ messages: [], target: 'Zahard', collection: 'a b', scope_unavailable: 'yes' })).toMatchObject({
      target: null,
      collection: null,
      scopeUnavailable: false,
    });
  });
});
