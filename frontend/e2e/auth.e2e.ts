import { expect, test } from '@playwright/test';
import type { Page, Route } from '@playwright/test';
import { E2E_NAME, installAuthMock } from './support/auth-mock';

// 認証のシナリオ。実ブラウザー + 本物の Better Auth クライアント + 偽の認証サーバー（page.route）。
// バックエンドは AUTH_MODE=off の偽のアプリ（トークンは検証しない）。実 Worker・実 Google は呼ばない。
// 画面（:3101）と API（:8100）は別オリジン。page.route で返す偽の応答には、CORS のヘッダーを付ける必要がある。
// 本物の Better Auth との相性（応答の形・Cookie・JWT の期限）は、ここでは確かめられない。
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

let THREADS: Array<{ thread_id: string; title: string; created_at: string; updated_at: string }> = [];

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
    (route) => fulfillCors(route, { threads: THREADS }),
  );
}

/** 未ログインで開いて、偽の Google ログインを通り、ログイン済みの画面になるまで待つ。 */
async function signIn(page: Page) {
  await page.goto('/?tab=chat');
  await page.getByRole('button', { name: 'Google でログイン' }).click();
  await expect(page.locator('.ws-account-name')).toHaveText(E2E_NAME);
}

async function ask(page: Page, text: string) {
  await page.getByRole('textbox', { name: 'メッセージ' }).fill(text);
  await page.getByRole('button', { name: '送信' }).click();
}

const ANSWER = '資料から該当箇所が見つかりました。';
const EXPIRED_NOTICE = 'ログインの有効期限が切れました。もう一度ログインしてください。';

test('ログインして戻ると、ログイン前の画面の状態（資料一覧の絞り込み）が URL に戻る', async ({ page, context }) => {
  const auth = await installAuthMock(context);
  await mockListApis(page);

  // 共有された URL を、未ログインで開く
  await page.goto('/?tab=documents&collection=cafe');
  const signIn = page.getByRole('button', { name: 'Google でログイン' });
  await expect(signIn).toBeVisible();

  await signIn.click();

  // ログインして戻ると、メールアドレスが出て、絞り込みの付いた資料一覧が開く
  await expect(page.locator('.ws-account-name')).toHaveText(E2E_NAME);
  await expect(page.getByRole('main', { name: 'ナレッジ' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'カフェのメニュー を開く' })).toBeVisible();
  await expect(page.getByRole('button', { name: 'ザーハード設定資料 1 を開く' })).toHaveCount(0);
  // ログイン前の状態に戻っている（最終的な安定状態で判定する）
  await expect.poll(() => new URL(page.url()).search).toBe('?tab=documents&collection=cafe');

  // 偽の認証サーバーに届いた通信は、セッションの確認とログインの開始など。127.0.0.1 以外への実通信は無い
  expect(auth.calls.map((c) => c.method + ' ' + c.path)).toEqual(
    expect.arrayContaining(['GET /api/auth/get-session', 'POST /api/auth/sign-in/social']),
  );
  expect(auth.leaks).toEqual([]);
});

test('チャットが 401 を返したら、トークンを 1 回だけ取り直して送り直し、ログインしたまま回答が出る', async ({ page, context }) => {
  const auth = await installAuthMock(context);
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
  await expect(page.locator('.ws-account-name')).toHaveText(E2E_NAME);
  await expect(page.getByText(EXPIRED_NOTICE)).toHaveCount(0);

  // 取り直しは 1 回だけ。送り直しには、取り直して発行された新しいトークンが付いている
  expect(auth.calls.filter((c) => c.path === '/api/auth/token')).toHaveLength(2);
  expect(auth.accessTokens).toHaveLength(2);
  expect(sent).toEqual([`Bearer ${auth.accessTokens[0]}`, `Bearer ${auth.accessTokens[1]}`]);
  expect(auth.leaks).toEqual([]);
});

test('トークンを取り直せないと、ログイン画面に戻り、送り直しや更新を繰り返さない', async ({ page, context }) => {
  const auth = await installAuthMock(context);
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
  const tokenCallsAfterSignIn = auth.calls.filter((c) => c.path === '/api/auth/token').length;
  auth.token = 'rejected'; // ログインのあと、セッションが無効になった
  await ask(page, 'ザーハードの大気について教えて');

  // ログイン画面に戻る
  await expect(page.getByRole('button', { name: 'Google でログイン' })).toBeVisible();
  await expect(page.locator('.ws-account-name')).toHaveCount(0);
  await expect(page.getByText(ANSWER)).toHaveCount(0);
  await expect(page.getByText(EXPIRED_NOTICE)).toBeVisible();

  // 繰り返さない: 送ったのは 1 回、トークンの取り直しも 1 回のまま。少し待っても増えない
  await page.waitForTimeout(2000);
  expect(chatRequests).toBe(1);
  expect(auth.calls.filter((c) => c.path === '/api/auth/token')).toHaveLength(tokenCallsAfterSignIn + 1);
  // 無効なセッションは破棄される（ログアウトが呼ばれる）
  expect(auth.calls.map((c) => c.method + ' ' + c.path)).toContain('POST /api/auth/sign-out');
  expect(auth.leaks).toEqual([]);
});

test('ログインすると、会話の履歴がサイドバーに出る。0 件と取得の失敗は、別の表示になり、再読み込みで取り直せる', async ({ page, context }) => {
  await installAuthMock(context);
  await mockListApis(page);
  const stamp = '2026-10-19T05:30:00+00:00';
  THREADS = [
    { thread_id: 'th-1', title: 'ザーハードの大気について', created_at: stamp, updated_at: stamp },
    { thread_id: 'th-2', title: '入寮の申込みについて', created_at: stamp, updated_at: stamp },
  ];
  await signIn(page);

  // 履歴あり: サイドバーの中に、題名が並ぶ（高さや CSS で見えなくならない）
  const history = page.getByRole('complementary', { name: '会話の一覧' });
  await expect(history.getByRole('heading', { name: '会話の履歴' })).toBeVisible();
  await expect(history.getByTitle('ザーハードの大気について')).toBeVisible();
  await expect(history.getByTitle('入寮の申込みについて')).toBeVisible();

  // 0 件: 案内を出す（読み込み中でも失敗でもない）
  THREADS = [];
  await page.reload();
  await expect(page.getByRole('complementary', { name: '会話の一覧' }).getByText('まだ会話がありません。質問を送ると、ここに並びます。')).toBeVisible();

  // 取得の失敗: 固定のメッセージと「再読み込み」。押すと取り直し、履歴が出る
  let failing = true;
  await page.route(
    (url) => url.pathname === '/threads',
    (route) => {
      if (route.request().method() === 'OPTIONS') return fulfillCors(route, {});
      return failing ? fulfillStatusCors(route, 500, { detail: 'x' }) : fulfillCors(route, { threads: [{ thread_id: 'th-3', title: '取り直した会話', created_at: stamp, updated_at: stamp }] });
    },
  );
  await page.reload();
  const failed = page.getByRole('complementary', { name: '会話の一覧' });
  await expect(failed.getByRole('alert')).toBeVisible();
  failing = false;
  await failed.getByRole('button', { name: '再読み込み' }).click();
  await expect(failed.getByTitle('取り直した会話')).toBeVisible();
});
