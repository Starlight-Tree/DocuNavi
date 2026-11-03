import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';
import { E2E_EMAIL, installSupabaseMock } from './support/supabase-mock';

// 認証のシナリオ。実ブラウザー + 本物の supabase-js（PKCE・セッションの保存）+ 偽の Supabase（page.route）。
// バックエンドは AUTH_MODE=off の偽のアプリ（トークンは検証しない）。実 Supabase・実 Google は呼ばない。
// 画面（:3101）と API（:8100）は別オリジン。page.route で返す偽の応答には、CORS のヘッダーを付ける必要がある。
// 本物の Supabase との相性（リフレッシュの応答の形・期限）は、ここでは確かめられない。
const DOCUMENTS = [
  { id: 1, source: 'cafe_menu.pdf', title: 'カフェのメニュー', collection: 'cafe' },
  { id: 2, source: 'zahard_1.pdf', title: 'ザーハード設定資料 1', collection: 'zahard' },
].map((d) => ({
  ...d,
  status: 'ready',
  failure_reason: null,
  pages: 1,
  updated_at: null,
  chunk_count: 1,
  embedded_count: 1,
}));

/** 別オリジンの API に見せかけた偽の応答。プリフライト（OPTIONS）には、要求されたヘッダーを許す応答を返す。 */
async function fulfillCors(route: Route, json: unknown) {
  const request = route.request();
  const headers = { 'access-control-allow-origin': request.headers()['origin'] ?? '*' };
  if (request.method() === 'OPTIONS') {
    return route.fulfill({
      status: 204,
      headers: {
        ...headers,
        'access-control-allow-methods': 'GET,POST,DELETE,OPTIONS',
        'access-control-allow-headers': request.headers()['access-control-request-headers'] ?? '',
      },
    });
  }
  return route.fulfill({ headers, json });
}

/** 本物の API が返すはずのエラーを、CORS のヘッダー付きで返す。 */
function fulfillStatusCors(route: Route, status: number, json: unknown) {
  const origin = route.request().headers()['origin'] ?? '*';
  return route.fulfill({ status, headers: { 'access-control-allow-origin': origin }, json });
}

/** 資料と会話の一覧は DB が要るので、形だけの応答にする。 */
async function mockListApis(page: Page) {
  await page.route(
    (url) => url.pathname === '/documents',
    (route) => fulfillCors(route, { configured: true, documents: DOCUMENTS }),
  );
  await page.route(
    (url) => url.pathname === '/threads',
    (route) => fulfillCors(route, { threads: [] }),
  );
}

/** 未ログインで開いて、偽の Google ログインを通り、ログイン済みの画面になるまで待つ。 */
async function signIn(page: Page) {
  await page.goto('/?tab=chat');
  await page.getByRole('button', { name: 'Google でログイン' }).click();
  await expect(page.locator('.ws-user')).toHaveText(E2E_EMAIL);
}

async function ask(page: Page, text: string) {
  await page.getByRole('textbox', { name: 'メッセージ' }).fill(text);
  await page.getByRole('button', { name: '送信' }).click();
}

const SESSION_KEY = /^sb-.+-auth-token$/;
const ANSWER = '資料から該当箇所が見つかりました。';
const EXPIRED_NOTICE = 'ログインの有効期限が切れました。もう一度ログインしてください。';

test('ログインして戻ると、ログイン前の画面の状態（資料一覧の絞り込み）が URL に戻る', async ({ page, context }) => {
  const supabase = await installSupabaseMock(context);
  await mockListApis(page);

  // 共有された URL を、未ログインで開く
  await page.goto('/?tab=documents&collection=cafe');
  const signIn = page.getByRole('button', { name: 'Google でログイン' });
  await expect(signIn).toBeVisible();

  await signIn.click();

  // ログインして戻ると、メールアドレスが出て、絞り込みの付いた資料一覧が開く
  await expect(page.locator('.ws-user')).toHaveText(E2E_EMAIL);
  await expect(page.getByRole('main', { name: '資料一覧' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'カフェのメニュー を開く' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'ザーハード設定資料 1 を開く' })).toHaveCount(0);
  // ログインの code は URL に残らず、ログイン前の状態に戻っている（最終的な安定状態で判定する）
  await expect.poll(() => new URL(page.url()).search).toBe('?tab=documents&collection=cafe');

  // セッションは SDK が localStorage に保存している（キー名だけ見る。値は見ない）
  const keys = await page.evaluate(() => Object.keys(window.localStorage));
  expect(keys.some((key) => /^sb-.+-auth-token$/.test(key))).toBe(true);

  // 偽の Supabase に届いた通信は、ログインの開始と認可コードの交換だけ。127.0.0.1 以外への実通信は無い
  expect(supabase.calls.map((c) => c.method + ' ' + c.path.split('?')[0])).toEqual(
    expect.arrayContaining(['GET /auth/v1/authorize', 'POST /auth/v1/token']),
  );
  expect(supabase.leaks).toEqual([]);
});

test('チャットが 401 を返したら、トークンを 1 回だけ更新して送り直し、ログインしたまま回答が出る', async ({ page, context }) => {
  const supabase = await installSupabaseMock(context);
  await mockListApis(page);
  // 1 回目だけ 401（期限切れのトークンを受けたサーバーの代わり）。2 回目からは本物の偽アプリに通す
  const sent: (string | undefined)[] = [];
  await page.route(
    (url) => url.pathname === '/chat/stream',
    (route) => {
      if (route.request().method() === 'OPTIONS') return route.continue(); // プリフライトは本物の API が答える
      sent.push(route.request().headers()['authorization']);
      if (sent.length === 1) return fulfillStatusCors(route, 401, { detail: 'ログインが必要です。' });
      return route.continue();
    },
  );

  await signIn(page);
  await ask(page, 'ザーハードの大気について教えて');

  await expect(page.getByText(ANSWER)).toBeVisible();
  // ログインしたまま（ログイン画面にも、期限切れの案内にも戻らない）
  await expect(page.locator('.ws-user')).toHaveText(E2E_EMAIL);
  await expect(page.getByText(EXPIRED_NOTICE)).toHaveCount(0);

  // 更新は 1 回だけ。送り直しには、更新で発行された新しいトークンが付いている
  expect(supabase.calls.filter((c) => c.grantType === 'refresh_token')).toHaveLength(1);
  expect(supabase.accessTokens).toHaveLength(2);
  expect(sent).toEqual([`Bearer ${supabase.accessTokens[0]}`, `Bearer ${supabase.accessTokens[1]}`]);
  expect(supabase.leaks).toEqual([]);
});

test('トークンを更新できないと、ログイン画面に戻り、送り直しや更新を繰り返さない', async ({ page, context }) => {
  const supabase = await installSupabaseMock(context);
  await mockListApis(page);
  // サーバーは何度送っても 401（トークンが無効になった状態）
  let chatRequests = 0;
  await page.route(
    (url) => url.pathname === '/chat/stream',
    (route) => {
      if (route.request().method() === 'OPTIONS') return route.continue(); // プリフライトは本物の API が答える
      chatRequests += 1;
      return fulfillStatusCors(route, 401, { detail: 'ログインが必要です。' });
    },
  );

  await signIn(page);
  supabase.refresh = 'rejected'; // ログインのあと、リフレッシュトークンが無効になった
  await ask(page, 'ザーハードの大気について教えて');

  // ログイン画面に戻る
  await expect(page.getByRole('button', { name: 'Google でログイン' })).toBeVisible();
  await expect(page.locator('.ws-user')).toHaveCount(0);
  await expect(page.getByText(ANSWER)).toHaveCount(0);
  await expect(page.getByText(EXPIRED_NOTICE)).toBeVisible();

  // 繰り返さない: 送ったのは 1 回、更新も 1 回のまま。少し待っても増えない
  await page.waitForTimeout(2000);
  expect(chatRequests).toBe(1);
  expect(supabase.calls.filter((c) => c.grantType === 'refresh_token')).toHaveLength(1);
  // 無効なセッションは端末に残らない（キー名だけ見る）
  const keys = await page.evaluate(() => Object.keys(window.localStorage));
  expect(keys.some((key) => SESSION_KEY.test(key))).toBe(false);
  expect(supabase.leaks).toEqual([]);
});
