import { ChatApiError, MEMBERSHIP_REQUIRED_CODE, chatFetch } from '../api/chat';
import { TokenUnavailableError, type AuthClient } from './types';

export const MSG_LOGIN_REQUIRED = 'ログインが必要です。ログインし直してください。';
/** トークンを取れなかったのは、通信やサーバーの一時的な不調で、ログアウトではない。ログイン画面へは戻さない。 */
export const MSG_AUTH_UNAVAILABLE = 'ログイン状態を確認できませんでした。通信を確認して、もう一度お試しください。';

function withToken(init: RequestInit | undefined, token: string): RequestInit {
  const headers = new Headers(init?.headers);
  headers.set('Authorization', `Bearer ${token}`);
  return { ...init, headers };
}

function rethrowUnavailable(error: unknown): never {
  if (error instanceof TokenUnavailableError) throw new ChatApiError(0, MSG_AUTH_UNAVAILABLE);
  throw error;
}

function isUnauthorized(error: unknown): boolean {
  return error instanceof ChatApiError && error.status === 401;
}

function isMembershipRefusal(error: unknown): boolean {
  return error instanceof ChatApiError && error.status === 403 && error.code === MEMBERSHIP_REQUIRED_CODE;
}

/**
 * チャットの fetch に、ログイン中のアクセストークンを付ける。
 *
 * - トークンは送る直前に取る（getAccessToken が、期限が近ければ更新してから返す）
 * - 401 が返ったら、リフレッシュトークンで 1 回だけ更新して送り直す。期限切れだけでログイン画面には戻さない
 * - 更新できない・更新しても 401 のときだけ、onSessionExpired を呼ぶ（ログイン画面へ戻す）。
 *   トークンの取得が通信の失敗・サーバーの不調で失敗したとき（TokenUnavailableError）は、ログイン画面へ戻さず、再試行を促すエラーにする
 * client が null（ログインを使わない構成）のときは、何も付けずに通常の fetch を使う。
 * 本文は文字列なので、送り直しても同じ内容になる。
 */
export function createAuthedFetch(
  getClient: () => AuthClient | null,
  onSessionExpired: () => void,
  baseFetch: typeof chatFetch = chatFetch,
  onMembershipRequired: () => void = () => undefined,
): typeof chatFetch {
  const authed: typeof chatFetch = async (input, init) => {
    const client = getClient();
    if (!client) return baseFetch(input, init);

    const token = await client.getAccessToken().catch(rethrowUnavailable);
    if (!token) {
      onSessionExpired();
      throw new ChatApiError(401, MSG_LOGIN_REQUIRED);
    }
    try {
      return await baseFetch(input, withToken(init, token));
    } catch (error) {
      if (!isUnauthorized(error)) throw error;
      const fresh = await client.refreshAccessToken().catch(rethrowUnavailable);
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
  return async (input, init) => {
    try {
      return await authed(input, init);
    } catch (error) {
      if (isMembershipRefusal(error)) onMembershipRequired();
      throw error;
    }
  };
}
