import { describe, expect, it } from 'vitest';
import { fetchThreadMessages } from './threads';

const json = (body: unknown) => new Response(JSON.stringify(body), { status: 200, headers: { 'Content-Type': 'application/json' } });
const read = (messages: unknown[]) => fetchThreadMessages(async () => json({ messages }), 't1');

describe('fetchThreadMessages: 質問の時刻（sent_at）', () => {
  it('ユーザーの発言の読める時刻だけを sentAt として受け取る', async () => {
    const { messages } = await read([
      { role: 'user', text: 'q1', sent_at: '2026-11-16T12:34:56+00:00' },
      { role: 'user', text: 'q2' },
      { role: 'user', text: 'q3', sent_at: '昨日' },
      { role: 'user', text: 'q4', sent_at: 12345 },
    ]);

    expect(messages[0]).toEqual({ role: 'user', text: 'q1', sentAt: '2026-11-16T12:34:56+00:00' });
    // 無い・読めない・文字列でない時刻は、項目ごと付けない
    expect(messages.slice(1).every((m) => !('sentAt' in m))).toBe(true);
  });

  it('回答に付いていても使わない（時刻を出すのは質問だけ）', async () => {
    const { messages } = await read([{ role: 'assistant', text: 'a', sent_at: '2026-11-16T12:34:56+00:00' }]);

    expect(messages[0]).toEqual({ role: 'assistant', text: 'a' });
  });
});
