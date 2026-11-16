// 本番の画面を、テスト専用ユーザー（e2e-test）としてログインして操作・撮影する共通の実行部。
// scripts/prod_check.sh --browse <シナリオ名> が、開発コンテナの frontend/ で実行する（直接は実行しない）。
// 出力に、メール・パスワード・トークン・Cookie は出さない。
//
// シナリオ: scripts/prod_scenarios/<名前>.cjs。`module.exports = async ({ page, ctx, WEB, shot, log, args }) => { … }`。
//   page = ログイン済みで、ビューポート 1280×800・ライトテーマ・ja-JP・Asia/Tokyo。
//   shot(名前) = フォーカスを外してから、ビューポートを SHOT_DIR/<名前>.png に撮る（名前は小文字・数字・ハイフンだけ。fullPage にしない = 画像のサイズがそろう）。
//   log(...) = 報告用の出力。args = SCENARIO_ARGS（JSON）をパースしたもの（既定 {}）。
//   例外を投げると、終了コード 1（NG の報告は、シナリオが log して `process.exitCode = 2` にする）。
//
// 環境変数: TEST_LOGIN_EMAIL / TEST_LOGIN_PASSWORD / TEST_LOGIN_TOKEN（必須。Infisical から渡す）、SCENARIO（必須）、
//   SHOT_NAME（画像の名前。シナリオの args.shot になる）、SHOT_DIR（既定 /workspaces/docunavi/.playwright-mcp）、WEB（既定: 本番の画面の URL）、
//   VIEWPORT（既定 1280x800）、THEME（light | dark。既定 light）、SCENARIO_ARGS（JSON）、QUESTION（シナリオが使う質問）
const fs = require('fs');
const path = require('path');
const { chromium } = require('playwright-core');

const WEB = (process.env.WEB || 'https://docunavi-web.wataru-apps.workers.dev').replace(/\/$/, '');
const SHOT_DIR = process.env.SHOT_DIR || '/workspaces/docunavi/.playwright-mcp';
const SCENARIO = process.env.SCENARIO || '';
const THEME = process.env.THEME === 'dark' ? 'dark' : 'light';
const vp = /^(\d{3,4})x(\d{3,4})$/.exec(process.env.VIEWPORT || '1280x800');

for (const k of ['TEST_LOGIN_EMAIL', 'TEST_LOGIN_PASSWORD', 'TEST_LOGIN_TOKEN']) {
  if (!process.env[k]) {
    console.error(`${k} が未設定です（値は表示しません）。`);
    process.exit(1);
  }
}
if (!/^[a-z0-9-]+$/.test(SCENARIO)) {
  console.error('SCENARIO は小文字・数字・ハイフンの名前だけです。');
  process.exit(1);
}
if (!vp) {
  console.error('VIEWPORT は 1280x800 の形です。');
  process.exit(1);
}
const scenarioFile = path.join(__dirname, 'prod_scenarios', `${SCENARIO}.cjs`);
if (!fs.existsSync(scenarioFile)) {
  console.error(`シナリオがありません: scripts/prod_scenarios/${SCENARIO}.cjs`);
  process.exit(1);
}
let args = {};
try {
  args = JSON.parse(process.env.SCENARIO_ARGS || '{}');
} catch {
  console.error('SCENARIO_ARGS が JSON ではありません。');
  process.exit(1);
}

if (process.env.SHOT_NAME) {
  if (!/^[a-z0-9-]+$/.test(process.env.SHOT_NAME)) {
    console.error('SHOT_NAME は小文字・数字・ハイフンだけです。');
    process.exit(1);
  }
  args.shot = process.env.SHOT_NAME;
}

(async () => {
  const run = require(scenarioFile);
  const browser = await chromium.launch();
  try {
    const ctx = await browser.newContext({
      viewport: { width: Number(vp[1]), height: Number(vp[2]) },
      colorScheme: THEME,
      locale: 'ja-JP',
      timezoneId: 'Asia/Tokyo',
    });

    // サインイン（400 = テスト用ログインがまだ有効になっていない。Secret の反映待ち）
    let res;
    for (let n = 0; n < 6; n++) {
      res = await ctx.request.post(`${WEB}/api/auth/sign-in/email`, {
        headers: {
          origin: WEB,
          'content-type': 'application/json',
          'x-test-login-token': process.env.TEST_LOGIN_TOKEN,
          'user-agent': 'docunavi-prod-browse/1.0',
        },
        data: { email: process.env.TEST_LOGIN_EMAIL, password: process.env.TEST_LOGIN_PASSWORD },
      });
      if (res.status() !== 400) break;
      await new Promise((r) => setTimeout(r, 10000));
    }
    console.log('sign-in:', res.status());
    if (res.status() !== 200) process.exit(1);

    const page = await ctx.newPage();
    fs.mkdirSync(SHOT_DIR, { recursive: true });
    const shot = async (name) => {
      if (!/^[a-z0-9-]+$/.test(name)) throw new Error('shot の名前は小文字・数字・ハイフンだけです');
      const file = path.join(SHOT_DIR, `${name}.png`);
      // 入力欄などのフォーカス枠（送信後の自動フォーカス）が写らないように、撮る前にフォーカスを外す
      await page.evaluate(() => document.activeElement && document.activeElement.blur && document.activeElement.blur());
      await page.waitForTimeout(200);
      await page.screenshot({ path: file });
      console.log('screenshot:', file.replace('/workspaces/docunavi/', ''));
    };
    const log = (...a) => console.log(...a);

    await run({ page, ctx, WEB, shot, log, args });
  } finally {
    await browser.close();
  }
})().catch((e) => {
  console.error('失敗:', String(e.message || e).split('\n')[0]);
  process.exit(1);
});
