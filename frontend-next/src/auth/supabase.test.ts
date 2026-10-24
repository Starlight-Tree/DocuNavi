import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAuthClientFromEnv } from './supabase';

afterEach(() => {
  vi.unstubAllEnvs();
});

describe('createAuthClientFromEnv', () => {
  it('設定が無ければ null（ログインを使わない）', () => {
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', '');
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY', '');
    expect(createAuthClientFromEnv()).toBeNull();
  });

  it('URL とキーの片方だけでは null', () => {
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', 'https://example.supabase.co');
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY', '  ');
    expect(createAuthClientFromEnv()).toBeNull();
  });

  it('両方あれば AuthClient を作る（ネットワークには触れない）', () => {
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_URL', 'https://example.supabase.co');
    vi.stubEnv('NEXT_PUBLIC_SUPABASE_PUBLISHABLE_KEY', 'sb_publishable_dummy');
    const client = createAuthClientFromEnv();
    expect(client).not.toBeNull();
    expect(typeof client?.signInWithGoogle).toBe('function');
  });
});
