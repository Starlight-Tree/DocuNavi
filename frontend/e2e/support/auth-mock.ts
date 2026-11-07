import type { BrowserContext, Request } from '@playwright/test';

// 認証テスト用の、Better Auth（データ用 Worker の /api/auth/*）の偽物。ブラウザーが出す通信を page.route で受けて返す
// （バックエンドは AUTH_MODE=off のまま）。偽の認証サーバーは存在しないホスト（.invalid）なので、取りこぼしがあっても実サービスには届かず、DNS で失敗する。
// 127.0.0.1 以外への通信は、すべて止めて記録する（leaks）。テストの最後に空であることを確かめる。
export const FAKE_AUTH_ORIGIN = 'https://auth.e2e.invalid';
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost']);

export const E2E_EMAIL = 'e2e-user@example.com';
const E2E_USER_ID = 'e2e-user-0000000001';

export interface AuthCall {
  method: string;
  path: string;
}

export interface AuthMock {
  /** 偽の認証サーバーに届いた通信（メソッドとパス）。 */
  calls: AuthCall[];
  /** 127.0.0.1 以外への通信（止めたもの）。空でなければ、モックの取りこぼし。 */
  leaks: string[];
  /** いまログイン中か（Google のログインを終えたあと true。ログアウトで false）。 */
  signedIn: boolean;
  /** /api/auth/token への応答。'ok' = 新しい JWT を返す / 'rejected' = 401（セッションが無効）。いつでも切り替えられる。 */
  token: 'ok' | 'rejected';
  /** 発行したアクセストークン（発行順）。偽の値で、署名もない。 */
  accessTokens: string[];
}

export interface AuthMockOptions {
  /** 発行するアクセストークンの有効秒数（既定 900 = 15 分）。 */
  expiresIn?: number;
}

function base64url(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

let issued = 0;

/** 署名のない、形だけの JWT（exp・sub を持つ）。バックエンドは検証しない（AUTH_MODE=off）。 */
function fakeJwt(expiresAt: number): string {
  const header = base64url({ alg: 'none', typ: 'JWT' });
  const payload = base64url({
    sub: E2E_USER_ID,
    aud: 'docunavi',
    iat: expiresAt - 900,
    exp: expiresAt,
    // 発行のたびに別の値にする（取り直しの前後でトークンが変わったことを、テストが見分けられるように）
    jti: `e2e-${(issued += 1)}`,
  });
  return `${header}.${payload}.e2e-not-a-real-signature`;
}

function corsHeaders(request: Request): Record<string, string> {
  return {
    'access-control-allow-origin': request.headers()['origin'] ?? '*',
    'access-control-allow-credentials': 'true',
    'access-control-allow-headers': request.headers()['access-control-request-headers'] ?? '*',
    'access-control-allow-methods': 'GET,POST,OPTIONS',
  };
}

/** 偽の Better Auth を、このブラウザーコンテキストに取り付ける。 */
export async function installAuthMock(context: BrowserContext, options: AuthMockOptions = {}): Promise<AuthMock> {
  const mock: AuthMock = { calls: [], leaks: [], signedIn: false, token: 'ok', accessTokens: [] };

  // 先に登録したものが後回しになる: まず 127.0.0.1 以外をすべて止め、その上に偽の認証サーバーを重ねる
  await context.route(
    (url) => !LOCAL_HOSTS.has(url.hostname) && url.protocol.startsWith('http'),
    (route) => {
      mock.leaks.push(route.request().url());
      return route.abort();
    },
  );

  await context.route(
    (url) => url.origin === FAKE_AUTH_ORIGIN,
    async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      const headers = corsHeaders(request);
      if (request.method() === 'OPTIONS') return route.fulfill({ status: 204, headers });
      mock.calls.push({ method: request.method(), path: url.pathname });

      // セッション。未ログインは null（Better Auth の応答と同じ）
      if (url.pathname === '/api/auth/get-session') {
        const body = mock.signedIn
          ? {
              session: { id: 'e2e-session', userId: E2E_USER_ID },
              user: { id: E2E_USER_ID, email: E2E_EMAIL, name: 'E2E', emailVerified: true },
            }
          : null;
        return route.fulfill({ status: 200, headers, contentType: 'application/json', body: JSON.stringify(body) });
      }
      // Google ログインの開始。Google と往復する代わりに、ログインを終えたことにして、戻り先（callbackURL）を返す
      if (url.pathname === '/api/auth/sign-in/social') {
        const { callbackURL } = JSON.parse(request.postData() ?? '{}') as { callbackURL?: string };
        if (!callbackURL) return route.fulfill({ status: 400, headers, json: { message: 'callbackURL がありません' } });
        mock.signedIn = true;
        return route.fulfill({ status: 200, headers, json: { url: callbackURL, redirect: true } });
      }
      // バックエンドへ送る短命の JWT
      if (url.pathname === '/api/auth/token') {
        if (!mock.signedIn || mock.token === 'rejected') {
          return route.fulfill({ status: 401, headers, json: { message: 'Unauthorized' } });
        }
        const token = fakeJwt(Math.floor(Date.now() / 1000) + (options.expiresIn ?? 900));
        mock.accessTokens.push(token);
        return route.fulfill({ status: 200, headers, json: { token } });
      }
      if (url.pathname === '/api/auth/sign-out') {
        mock.signedIn = false;
        return route.fulfill({ status: 200, headers, json: { success: true } });
      }
      // 想定外の通信は、通さずに失敗させて記録に残す
      mock.leaks.push(`${request.method()} ${url.origin}${url.pathname}`);
      return route.fulfill({ status: 501, headers, json: { error: 'e2e mock: 未対応' } });
    },
  );

  return mock;
}
