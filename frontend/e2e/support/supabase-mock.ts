import type { BrowserContext, Request } from '@playwright/test';

// 認証テスト用の、Supabase 認証 API の偽物。ブラウザーが出す通信を page.route で受けて返す（バックエンドは AUTH_MODE=off のまま）。
// 偽の Supabase は存在しないホスト（.invalid）なので、モックの取りこぼしがあっても実サービスには届かず、DNS で失敗する。
// 127.0.0.1 以外への通信は、すべて止めて記録する（leaks）。テストの最後に空であることを確かめる。
export const FAKE_SUPABASE_ORIGIN = 'https://auth.e2e.invalid';
const LOCAL_HOSTS = new Set(['127.0.0.1', 'localhost']);

export const E2E_EMAIL = 'e2e-user@example.com';
const E2E_USER_ID = '00000000-0000-4000-8000-0000000000e2';

export interface AuthCall {
  method: string;
  path: string;
  /** /auth/v1/token の grant_type（pkce = ログインの認可コードの交換 / refresh_token = 更新）。それ以外は undefined。 */
  grantType?: string;
}

export interface SupabaseMock {
  /** 偽の Supabase に届いた通信（メソッドとパス）。 */
  calls: AuthCall[];
  /** 127.0.0.1 以外への通信（止めたもの）。空でなければ、モックの取りこぼし。 */
  leaks: string[];
  /** 更新（refresh_token）への応答。'ok' = 新しいセッションを返す / 'rejected' = 400（リフレッシュトークンが無効）。いつでも切り替えられる。 */
  refresh: 'ok' | 'rejected';
  /** 発行したアクセストークン（発行順。ログイン → 更新）。偽の値で、署名もない。 */
  accessTokens: string[];
}

export interface SupabaseMockOptions {
  /** ログインで返すアクセストークンの有効秒数（既定 3600）。 */
  loginExpiresIn?: number;
}

function base64url(value: unknown): string {
  return Buffer.from(JSON.stringify(value)).toString('base64url');
}

/** 署名のない、形だけの JWT（exp・sub・email を持つ）。バックエンドは検証しない（AUTH_MODE=off）。 */
let issued = 0;

function fakeJwt(expiresAt: number): string {
  const header = base64url({ alg: 'none', typ: 'JWT' });
  const payload = base64url({
    sub: E2E_USER_ID,
    email: E2E_EMAIL,
    aud: 'authenticated',
    role: 'authenticated',
    iat: expiresAt - 3600,
    exp: expiresAt,
    // 発行のたびに別の値にする（更新の前後でトークンが変わったことを、テストが見分けられるように）
    jti: `e2e-${(issued += 1)}`,
  });
  return `${header}.${payload}.e2e-not-a-real-signature`;
}

function sessionBody(expiresInSeconds: number, accessTokens: string[]) {
  const expiresAt = Math.floor(Date.now() / 1000) + expiresInSeconds;
  const accessToken = fakeJwt(expiresAt);
  accessTokens.push(accessToken);
  return {
    access_token: accessToken,
    token_type: 'bearer',
    expires_in: expiresInSeconds,
    expires_at: expiresAt,
    refresh_token: 'e2e-refresh-token',
    user: {
      id: E2E_USER_ID,
      aud: 'authenticated',
      role: 'authenticated',
      email: E2E_EMAIL,
      app_metadata: { provider: 'google', providers: ['google'] },
      user_metadata: {},
      created_at: '2026-01-01T00:00:00Z',
    },
  };
}

const CORS = {
  'access-control-allow-origin': '*',
  'access-control-allow-headers': '*',
  'access-control-allow-methods': 'GET,POST,PUT,DELETE,OPTIONS',
};

/** 偽の Supabase を、このブラウザーコンテキストに取り付ける。 */
export async function installSupabaseMock(
  context: BrowserContext,
  options: SupabaseMockOptions = {},
): Promise<SupabaseMock> {
  const mock: SupabaseMock = { calls: [], leaks: [], refresh: 'ok', accessTokens: [] };

  // 先に登録したものが後回しになる: まず 127.0.0.1 以外をすべて止め、その上に偽の Supabase を重ねる
  await context.route(
    (url) => !LOCAL_HOSTS.has(url.hostname) && url.protocol.startsWith('http'),
    (route) => {
      mock.leaks.push(route.request().url());
      return route.abort();
    },
  );

  await context.route(
    (url) => url.origin === FAKE_SUPABASE_ORIGIN,
    async (route) => {
      const request: Request = route.request();
      const url = new URL(request.url());
      const grantType = url.searchParams.get('grant_type') ?? undefined;
      mock.calls.push({
        method: request.method(),
        path: `${url.pathname}${url.search ? '?…' : ''}`,
        ...(grantType ? { grantType } : {}),
      });

      if (request.method() === 'OPTIONS') {
        return route.fulfill({ status: 204, headers: CORS });
      }
      // Google と Supabase の往復の代わり: ログインの開始を、そのまま戻り先（?code= 付き）へ返す
      if (url.pathname === '/auth/v1/authorize') {
        const redirectTo = url.searchParams.get('redirect_to');
        if (!redirectTo) return route.fulfill({ status: 400, body: 'redirect_to がありません' });
        const back = new URL(redirectTo);
        back.searchParams.set('code', 'e2e-auth-code');
        return route.fulfill({ status: 302, headers: { location: back.toString() } });
      }
      // 認可コードの交換（PKCE）
      if (url.pathname === '/auth/v1/token' && grantType === 'pkce') {
        const body = sessionBody(options.loginExpiresIn ?? 3600, mock.accessTokens);
        return route.fulfill({ status: 200, headers: CORS, contentType: 'application/json', json: body });
      }
      // リフレッシュトークンでの更新。エラーの本文の形は、SDK のソースに合わせた推定（本物の Supabase では未確認）
      if (url.pathname === '/auth/v1/token' && grantType === 'refresh_token') {
        if (mock.refresh === 'rejected') {
          return route.fulfill({
            status: 400,
            headers: CORS,
            contentType: 'application/json',
            json: { error: 'invalid_grant', error_description: 'Invalid Refresh Token: Refresh Token Not Found' },
          });
        }
        return route.fulfill({
          status: 200,
          headers: CORS,
          contentType: 'application/json',
          json: sessionBody(3600, mock.accessTokens),
        });
      }
      // ログアウト（SDK の signOut が、この端末のセッションを消す前に呼ぶ）
      if (url.pathname === '/auth/v1/logout') {
        return route.fulfill({ status: 204, headers: CORS });
      }
      // 想定外の通信は、通さずに失敗させて記録に残す
      mock.leaks.push(`${request.method()} ${url.origin}${url.pathname}`);
      return route.fulfill({ status: 501, headers: CORS, contentType: 'application/json', json: { error: 'e2e mock: 未対応' } });
    },
  );

  return mock;
}
