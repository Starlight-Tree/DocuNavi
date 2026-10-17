import { ChatApiError, chatFetch } from '../api/chat';
import type { AuthClient } from './types';

export const MSG_LOGIN_REQUIRED = 'ログインが必要です。ログインし直してください。';

function withToken(init: RequestInit | undefined, token: string): RequestInit {
  const headers = new Headers(init?.headers);
  headers.set('Authorization', `Bearer ${token}`);
  return { ...init, headers };
}

function isUnauthorized(error: unknown): boolean {
  return error instanceof ChatApiError && error.status === 401;
}

/**
 * チャットの fetch に、ログイン中のアクセストークンを付ける。
 *
 * - トークンは送る直前に取る（getAccessToken が、期限が近ければ更新してから返す）
 * - 401 が返ったら、リフレッシュトークンで 1 回だけ更新して送り直す。期限切れだけでログイン画面には戻さない
 * - 更新できない・更新しても 401 のときだけ、onSessionExpired を呼ぶ（ログイン画面へ戻す）
 * client が null（ログインを使わない構成）のときは、何も付けずに通常の fetch を使う。
 * 本文は文字列なので、送り直しても同じ内容になる。
 */
export function createAuthedFetch(
  getClient: () => AuthClient | null,
  onSessionExpired: () => void,
  baseFetch: typeof chatFetch = chatFetch,
): typeof chatFetch {
  return async (input, init) => {
    const client = getClient();
    if (!client) return baseFetch(input, init);

    const token = await client.getAccessToken();
    if (!token) {
      onSessionExpired();
      throw new ChatApiError(401, MSG_LOGIN_REQUIRED);
    }
    try {
      return await baseFetch(input, withToken(init, token));
    } catch (error) {
      if (!isUnauthorized(error)) throw error;
      const fresh = await client.refreshAccessToken();
      if (!fresh) {
        onSessionExpired();
        throw error;
      }
      try {
        return await baseFetch(input, withToken(init, fresh));
      } catch (retryError) {
        if (isUnauthorized(retryError)) onSessionExpired();
        throw retryError;
      }
    }
  };
}
