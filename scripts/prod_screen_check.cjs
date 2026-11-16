// 本番の画面の確認（テスト専用ユーザーのセッション）。scripts/prod_check.sh --screen が、開発コンテナの frontend/ で実行する。
// 質問は 1 回だけ（実 API・課金）。出力に、メール・パスワード・トークン・Cookie は出さない。
//
// 環境変数: TEST_LOGIN_EMAIL / TEST_LOGIN_PASSWORD / TEST_LOGIN_TOKEN（必須。Infisical から渡す）、
//   QUESTION（既定: 寮の門限は何時ですか？）、SHOT（スクリーンショットの保存先）、WEB（既定: 本番の画面の URL）
//
// 確かめるもの: 質問の上の時刻（「今日 HH:MM」）、回答の太字（strong / weight 600 以上）、本文と太字の色の差、質問の吹き出しの背景と枠線。
// 終了コード: 0 = 画面が開いて回答が出た、1 = サインイン・回答の表示に失敗、2 = 時刻が出ていない。
const { chromium } = require('playwright-core');

const WEB = (process.env.WEB || 'https://docunavi-web.wataru-apps.workers.dev').replace(/\/$/, '');
const QUESTION = process.env.QUESTION || '寮の門限は何時ですか？';
const SHOT = process.env.SHOT || '/tmp/prod-screen-check.png';
for (const k of ['TEST_LOGIN_EMAIL', 'TEST_LOGIN_PASSWORD', 'TEST_LOGIN_TOKEN']) {
  if (!process.env[k]) {
    console.error(`${k} が未設定です（値は表示しません）。`);
    process.exit(1);
  }
}

(async () => {
  const browser = await chromium.launch();
  const ctx = await browser.newContext({ viewport: { width: 1280, height: 800 }, locale: 'ja-JP', timezoneId: 'Asia/Tokyo' });

  let res;
  for (let n = 0; n < 6; n++) {
    res = await ctx.request.post(`${WEB}/api/auth/sign-in/email`, {
      headers: {
        origin: WEB,
        'content-type': 'application/json',
        'x-test-login-token': process.env.TEST_LOGIN_TOKEN,
        'user-agent': 'docunavi-prod-screen-check/1.0',
      },
      data: { email: process.env.TEST_LOGIN_EMAIL, password: process.env.TEST_LOGIN_PASSWORD },
    });
    if (res.status() !== 400) break; // 400 = テスト用ログインがまだ有効になっていない（Secret の反映待ち）
    await new Promise((r) => setTimeout(r, 10000));
  }
  console.log('sign-in:', res.status());
  if (res.status() !== 200) {
    await browser.close();
    process.exit(1);
  }

  const page = await ctx.newPage();
  await page.goto(`${WEB}/?tab=chat`, { waitUntil: 'networkidle' });
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await input.waitFor({ timeout: 30000 });
  await input.fill(QUESTION);
  await page.getByRole('button', { name: '送信' }).click();
  // 回答が最後まで届き、出典の欄が出るまで待つ
  await page.locator('article.message-assistant').first().waitFor({ timeout: 60000 });
  await page.getByRole('button', { name: /^\d+ 件のソース/ }).waitFor({ timeout: 90000 });
  await page.waitForTimeout(1500);

  const info = await page.evaluate(() => {
    const cs = (e) => getComputedStyle(e);
    const time = document.querySelector('time.message-time');
    const answer = document.querySelector('article.message-assistant');
    const user = document.querySelector('article.message-user');
    const bold = answer
      ? [...answer.querySelectorAll('*')]
          .filter((e) => e.children.length === 0 && e.textContent.trim() && (['STRONG', 'B'].includes(e.tagName) || parseInt(cs(e).fontWeight, 10) >= 600))
          .map((e) => `${e.textContent.trim()} (${e.tagName.toLowerCase()}, weight ${cs(e).fontWeight})`)
      : [];
    const para = answer ? answer.querySelector('p') : null;
    const strong = answer ? answer.querySelector('strong') : null;
    // 吹き出し = 質問の article の中で、背景が透明でない最初の要素（無ければ article 自身）
    const bubble = user ? [user, ...user.querySelectorAll('*')].find((e) => cs(e).backgroundColor !== 'rgba(0, 0, 0, 0)') || user : null;
    return {
      time: time ? time.textContent.trim() : null,
      bold,
      bodyColor: para ? cs(para).color : null,
      strongColor: strong ? cs(strong).color : null,
      bubble: bubble ? { background: cs(bubble).backgroundColor, border: `${cs(bubble).borderTopWidth} ${cs(bubble).borderTopStyle}` } : null,
    };
  });
  console.log(JSON.stringify(info, null, 1));
  await page.screenshot({ path: SHOT });
  console.log('screenshot:', SHOT);
  await browser.close();
  if (!info.time || !/^今日 \d\d:\d\d$/.test(info.time)) {
    console.log('NG: 質問の上に「今日 HH:MM」の時刻が出ていません');
    process.exit(2);
  }
})().catch((e) => {
  console.error('失敗:', String(e.message || e).split('\n')[0]);
  process.exit(1);
});
