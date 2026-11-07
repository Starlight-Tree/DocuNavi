import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAuthClientFromEnv } from './betterAuth';

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
