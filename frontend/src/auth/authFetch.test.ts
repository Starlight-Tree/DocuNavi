import { describe, expect, it, vi } from 'vitest';
import { ChatApiError } from '../api/chat';
import type { chatFetch } from '../api/chat';
import { fakeAuth } from '../test/auth';
import { MSG_LOGIN_REQUIRED, createAuthedFetch } from './authFetch';

type Fetch = typeof chatFetch;

const OK = () => new Response('ok');
const unauthorized = () => new ChatApiError(401, 'ログインが必要です。ログインし直してください。');

function bearer(call: unknown[]): string | null {
  return new Headers((call[1] as RequestInit).headers).get('Authorization');
}

describe('createAuthedFetch', () => {
  it('ログインを使わない構成（client なし）では、何も付けずに送る', async () => {
    const base = vi.fn<Fetch>(async () => OK());
    const init = { method: 'POST', headers: { Accept: 'text/event-stream' } };

    await createAuthedFetch(() => null, vi.fn(), base)('/chat/stream', init);

    expect(base).toHaveBeenCalledWith('/chat/stream', init);
  });

  it('アクセストークンを Authorization に付け、ほかのヘッダと本文は保つ', async () => {
    const base = vi.fn<Fetch>(async () => OK());
    const auth = fakeAuth();

    await createAuthedFetch(() => auth, vi.fn(), base)('/chat/stream', {
      method: 'POST',
      headers: { Accept: 'text/event-stream' },
      body: '{"message":"こんにちは"}',
    });

    const call = base.mock.calls[0]!;
    const headers = new Headers((call[1] as RequestInit).headers);
    expect(headers.get('Authorization')).toBe('Bearer tok-1');
    expect(headers.get('Accept')).toBe('text/event-stream');
    expect((call[1] as RequestInit).body).toBe('{"message":"こんにちは"}');
    expect(auth.refreshAccessToken).not.toHaveBeenCalled();
  });

  it('Headers 形式のヘッダにも付けられる', async () => {
    const base = vi.fn<Fetch>(async () => OK());

    await createAuthedFetch(() => fakeAuth(), vi.fn(), base)('/x', { headers: new Headers({ A: 'b' }) });

    const headers = new Headers((base.mock.calls[0]![1] as RequestInit).headers);
    expect(headers.get('A')).toBe('b');
    expect(headers.get('Authorization')).toBe('Bearer tok-1');
  });

  it('401 なら 1 回だけ更新して、新しいトークンで送り直す（ログイン画面には戻さない）', async () => {
    const base = vi
      .fn<Fetch>()
      .mockRejectedValueOnce(unauthorized())
      .mockResolvedValueOnce(OK());
    const expired = vi.fn();
    const auth = fakeAuth();

    const response = await createAuthedFetch(() => auth, expired, base)('/x', { body: 'b' });

    expect(await response.text()).toBe('ok');
    expect(auth.refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(base).toHaveBeenCalledTimes(2);
    expect(bearer(base.mock.calls[0]!)).toBe('Bearer tok-1');
    expect(bearer(base.mock.calls[1]!)).toBe('Bearer tok-2');
    expect((base.mock.calls[1]![1] as RequestInit).body).toBe('b');
    expect(expired).not.toHaveBeenCalled();
  });

  it('更新できなければ、期限切れとして知らせる', async () => {
    const base = vi.fn<Fetch>().mockRejectedValue(unauthorized());
    const expired = vi.fn();

    await expect(
      createAuthedFetch(() => fakeAuth({ refreshed: null }), expired, base)('/x'),
    ).rejects.toMatchObject({ status: 401 });

    expect(base).toHaveBeenCalledTimes(1);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it('更新しても 401 なら、もう送り直さずに期限切れとして知らせる', async () => {
    const base = vi.fn<Fetch>().mockRejectedValue(unauthorized());
    const expired = vi.fn();
    const auth = fakeAuth();

    await expect(createAuthedFetch(() => auth, expired, base)('/x')).rejects.toMatchObject({
      status: 401,
    });

    expect(base).toHaveBeenCalledTimes(2);
    expect(auth.refreshAccessToken).toHaveBeenCalledTimes(1);
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it('トークンが無ければ、送らずに期限切れとして知らせる', async () => {
    const base = vi.fn<Fetch>(async () => OK());
    const expired = vi.fn();

    await expect(
      createAuthedFetch(() => fakeAuth({ token: null }), expired, base)('/x'),
    ).rejects.toMatchObject({ status: 401, message: MSG_LOGIN_REQUIRED });

    expect(base).not.toHaveBeenCalled();
    expect(expired).toHaveBeenCalledTimes(1);
  });

  it('401 以外の失敗では更新も期限切れの通知もしない', async () => {
    const base = vi.fn<Fetch>().mockRejectedValue(new ChatApiError(429, '混み合っています'));
    const expired = vi.fn();
    const auth = fakeAuth();

    await expect(createAuthedFetch(() => auth, expired, base)('/x')).rejects.toMatchObject({
      status: 429,
    });

    expect(auth.refreshAccessToken).not.toHaveBeenCalled();
    expect(expired).not.toHaveBeenCalled();
  });
});
