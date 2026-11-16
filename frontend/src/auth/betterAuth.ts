import { createAuthClient } from 'better-auth/client';
import type { AuthClient, AuthUser } from './types';

/** アクセストークン（JWT）の exp（秒）。読めなければ 0（毎回取り直す）。 */
function expiresAt(token: string): number {
  try {
    const payload = token.split('.')[1] ?? '';
    const json = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
    const exp = (JSON.parse(json) as { exp?: unknown }).exp;
    return typeof exp === 'number' ? exp : 0;
  } catch {
    return 0;
  }
}

/** 期限のこの秒数前になったら、使う前に取り直す。 */
const REFRESH_MARGIN_SECONDS = 60;

/**
 * Better Auth（Google ログイン。データ用 Worker の /api/auth/*）の AuthClient。
 *
 * - セッションは Cookie（同じオリジン。画面の Worker が /api/auth/* をデータ用 Worker へ中継する）。
 * - バックエンド（Cloud Run）へ送るアクセストークンは、`/api/auth/token` が返す短命の JWT（ES256、15 分）。期限の少し前に取り直す。
 */
export function createBetterAuth(baseURL: string): AuthClient {
  const client = createAuthClient({ baseURL, fetchOptions: { credentials: 'include' } });
  let cached: { token: string; exp: number } | null = null;
  // 自分でログアウト（または期限切れで破棄）した直後は、サーバーが消す前の古い「ログイン中」の通知を捨てる
  let signingOut = false;

  const toUser = (data: { user?: { email?: string | null; name?: string | null; image?: string | null } } | null | undefined): AuthUser | null =>
    data?.user ? { email: data.user.email ?? null, name: data.user.name ?? null, image: data.user.image ?? null } : null;

  const requestToken = async (): Promise<string | null> => {
    const { data } = await client.$fetch<{ token?: string }>('/token', { method: 'GET' });
    const token = data?.token ?? null;
    cached = token ? { token, exp: expiresAt(token) } : null;
    return token;
  };

  // 同時に何本もの通信が要求しても、取りに行くのは 1 回（同じ結果を共有する）
  let inflight: Promise<string | null> | null = null;
  const fetchToken = (): Promise<string | null> => {
    inflight ??= requestToken().finally(() => {
      inflight = null;
    });
    return inflight;
  };

  return {
    getUser: async () => toUser((await client.getSession()).data),
    getAccessToken: async () => {
      const now = Math.floor(Date.now() / 1000);
      if (cached && cached.exp - now > REFRESH_MARGIN_SECONDS) return cached.token;
      return fetchToken();
    },
    refreshAccessToken: fetchToken,
    onChange: (listener) => {
      // 読み込み中の通知は、ログアウトと区別できないので捨てる（最初の状態は getUser が決める）
      return client.useSession.subscribe((state) => {
        if (state.isPending) return;
        if (!state.data) {
          cached = null;
          signingOut = false;
        } else if (signingOut) {
          return;
        }
        listener(toUser(state.data));
      });
    },
    signInWithGoogle: async () => {
      const { error } = await client.signIn.social({ provider: 'google', callbackURL: window.location.origin });
      if (error) throw new Error('sign-in failed');
    },
    signOut: async () => {
      cached = null;
      signingOut = true;
      await client.signOut();
    },
  };
}

/**
 * NEXT_PUBLIC_AUTH_ENABLED が真（1 / true）なら AuthClient を作る。無ければ null: ログイン画面を出さず、従来どおり認証なしで動く
 * （バックエンドが AUTH_MODE=required だと 401 になる）。
 * 接続先は、既定でこの画面と同じオリジン（NEXT_PUBLIC_AUTH_BASE_URL で変えられる。テスト用）。
 */
export function createAuthClientFromEnv(): AuthClient | null {
  const enabled = (process.env.NEXT_PUBLIC_AUTH_ENABLED ?? '').trim().toLowerCase();
  if (enabled !== '1' && enabled !== 'true') return null;
  const base = (process.env.NEXT_PUBLIC_AUTH_BASE_URL ?? '').trim();
  return createBetterAuth(base || window.location.origin);
}
