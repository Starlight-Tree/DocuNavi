// 本番の画面の確認: 質問を 1 回送り（実 API・課金が 1 回）、時刻・太字・色・吹き出しを検査して撮る。
// `prod_check.sh --screen` と同じ内容を、共通の実行部で動かす版。撮影: screen-check。
// 終了コード: 0 = 回答が出て時刻もある、2 = 時刻が出ていない。
module.exports = async ({ page, WEB, shot, log, args }) => {
  const question = process.env.QUESTION || '寮の門限は何時ですか？';
  await page.goto(`${WEB}/?tab=chat`, { waitUntil: 'networkidle' });
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await input.waitFor({ timeout: 30000 });
  await input.fill(question);
  await page.getByRole('button', { name: '送信' }).click();
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
    const bubble = user ? [user, ...user.querySelectorAll('*')].find((e) => cs(e).backgroundColor !== 'rgba(0, 0, 0, 0)') || user : null;
    return {
      time: time ? time.textContent.trim() : null,
      bold,
      bodyColor: para ? cs(para).color : null,
      strongColor: strong ? cs(strong).color : null,
      bubble: bubble ? { background: cs(bubble).backgroundColor, border: `${cs(bubble).borderTopWidth} ${cs(bubble).borderTopStyle}` } : null,
    };
  });
  log(JSON.stringify(info, null, 1));
  await shot(args.shot || 'screen-check');
  if (!info.time || !/^今日 \d\d:\d\d$/.test(info.time)) {
    log('NG: 質問の上に「今日 HH:MM」の時刻が出ていません');
    process.exitCode = 2;
  }
};
