import { vi } from 'vitest';
import type { AuthClient, AuthUser } from '../auth/types';

export interface FakeAuth extends AuthClient {
  /** 外からログイン状態を変える（別タブでのログアウトなど）。 */
  emit: (user: AuthUser | null) => void;
  getUser: ReturnType<typeof vi.fn<() => Promise<AuthUser | null>>>;
  getAccessToken: ReturnType<typeof vi.fn<() => Promise<string | null>>>;
  refreshAccessToken: ReturnType<typeof vi.fn<() => Promise<string | null>>>;
  signInWithGoogle: ReturnType<typeof vi.fn<() => Promise<void>>>;
  signOut: ReturnType<typeof vi.fn<() => Promise<void>>>;
}

export interface FakeAuthOptions {
  user?: AuthUser | null;
  token?: string | null;
  refreshed?: string | null;
}

/** AuthClient の偽物。既定は「a@example.com でログイン中・トークン tok-1・更新すると tok-2」。 */
export function fakeAuth(options: FakeAuthOptions = {}): FakeAuth {
  const listeners = new Set<(user: AuthUser | null) => void>();
  let user = options.user === undefined ? { email: 'a@example.com' } : options.user;
  const emit = (next: AuthUser | null) => {
    user = next;
    listeners.forEach((listener) => listener(next));
  };
  return {
    emit,
    getUser: vi.fn(async () => user),
    getAccessToken: vi.fn(async () => (options.token === undefined ? 'tok-1' : options.token)),
    refreshAccessToken: vi.fn(async () => (options.refreshed === undefined ? 'tok-2' : options.refreshed)),
    onChange: (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    signInWithGoogle: vi.fn(async () => undefined),
    signOut: vi.fn(async () => {
      emit(null);
    }),
  };
}
