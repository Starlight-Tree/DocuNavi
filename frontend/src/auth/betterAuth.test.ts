import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAuthClientFromEnv, createBetterAuth, isSessionRejected } from './betterAuth';
import { TokenUnavailableError } from './types';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('createAuthClientFromEnv', () => {
  it('設定が無ければ null（ログインを使わない）', () => {
    vi.stubEnv('NEXT_PUBLIC_AUTH_ENABLED', '');
    expect(createAuthClientFromEnv()).toBeNull();
  });

  it('偽や 0 では null', () => {
    vi.stubEnv('NEXT_PUBLIC_AUTH_ENABLED', '0');
    expect(createAuthClientFromEnv()).toBeNull();
    vi.stubEnv('NEXT_PUBLIC_AUTH_ENABLED', 'false');
    expect(createAuthClientFromEnv()).toBeNull();
  });

  it('1 / true なら AuthClient を作る（ネットワークには触れない）', () => {
    vi.stubEnv('NEXT_PUBLIC_AUTH_ENABLED', '1');
    const client = createAuthClientFromEnv();
    expect(client).not.toBeNull();
    expect(typeof client?.signInWithGoogle).toBe('function');
  });
});

describe('isSessionRejected', () => {
  it('401・403 などは「セッションが無い」。通信の失敗・5xx・408・429 は一時的な失敗', () => {
    expect(isSessionRejected(401)).toBe(true);
    expect(isSessionRejected(403)).toBe(true);
    expect(isSessionRejected(500)).toBe(false);
    expect(isSessionRejected(503)).toBe(false);
    expect(isSessionRejected(408)).toBe(false);
    expect(isSessionRejected(429)).toBe(false);
    expect(isSessionRejected(undefined)).toBe(false);
  });
});

describe('createBetterAuth: トークンの取得', () => {
  const json = (status: number, body: unknown) =>
    new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

  it('通信が失敗したら TokenUnavailableError（null＝未ログイン、とは区別する）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
    await expect(createBetterAuth('http://localhost').getAccessToken()).rejects.toBeInstanceOf(TokenUnavailableError);
  });

  it('503 でも TokenUnavailableError', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(503, { message: 'unavailable' })));
    await expect(createBetterAuth('http://localhost').getAccessToken()).rejects.toBeInstanceOf(TokenUnavailableError);
  });

  it('401 なら null（セッションが無い）', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json(401, { message: 'Unauthorized' })));
    await expect(createBetterAuth('http://localhost').getAccessToken()).resolves.toBeNull();
  });
});
