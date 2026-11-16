// 本番のチャットで 2 往復する（実 API・課金が 2 回）。1 往復目の質問を送り、回答のあとに出る「続けて聞く」候補の 1 つを押して 2 往復目を送る。
// 2 往復目の回答が出た時点で撮る。撮影: chat-2turns（SHOT 名は args.shot で変えられる）。
// args: { "shot": "chat-2turns" }。1 往復目の質問は環境変数 QUESTION（既定: 寮の門限は何時ですか？）。
module.exports = async ({ page, WEB, shot, log, args }) => {
  const question = process.env.QUESTION || '寮の門限は何時ですか？';
  await page.goto(`${WEB}/?tab=chat`, { waitUntil: 'networkidle' });

  const send = async (text, turn) => {
    const input = page.getByRole('textbox', { name: 'メッセージ' });
    await input.waitFor({ timeout: 30000 });
    await input.fill(text);
    await page.getByRole('button', { name: '送信' }).click();
    // 回答が最後まで届く = turn 個目の「N 件のソース」が出て、待機の表示が消える
    await page.locator('article.message-assistant').nth(turn - 1).waitFor({ timeout: 60000 });
    await page.getByRole('button', { name: /^\d+ 件のソース/ }).nth(turn - 1).waitFor({ timeout: 90000 });
    await page.locator('.message-pending').waitFor({ state: 'detached', timeout: 30000 });
    await page.waitForTimeout(1500);
  };

  await send(question, 1);

  const candidate = page.getByRole('group', { name: '続けて聞く' }).getByRole('button').first();
  try {
    await candidate.waitFor({ timeout: 15000 });
  } catch (e) {
    // 1 往復目の回答は出たが、候補が出ない（回答の根拠の資料名が候補の表に無い、など）。原因の調査用に撮って止める（2 往復目は送らない）
    log('「続けて聞く」候補が出ませんでした。出典のボタン:', await page.getByRole('button', { name: /^\d+ 件のソース/ }).allTextContents());
    await shot(`${args.shot || 'chat-2turns'}-failed`);
    throw e;
  }
  const secondQuestion = (await candidate.textContent()).trim();
  await send(secondQuestion, 2);

  // 回答の位置までスクロールする（候補が見える位置。ビューポートは変えない）
  await page.getByRole('group', { name: '続けて聞く' }).scrollIntoViewIfNeeded();
  await page.waitForTimeout(500);

  const first = await page.evaluate(() => {
    const a = [...document.querySelectorAll('article.message-assistant')].pop();
    return (a ? a.innerText : '').split('\n').map((s) => s.trim()).find(Boolean) || '';
  });
  log('2往復目の質問:', secondQuestion);
  log('2往復目の回答の冒頭:', first);
  log('時刻:', await page.locator('time.message-time').allTextContents());
  log('太字:', await page.locator('article.message-assistant strong').last().allTextContents());
  await shot(args.shot || 'chat-2turns');
};
