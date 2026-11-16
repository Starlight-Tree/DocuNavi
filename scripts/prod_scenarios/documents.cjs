// 本番の資料一覧（ナレッジ）を開いて撮る。実 API は呼ばない（課金なし）。撮影: documents（args.shot で変えられる）。
// 件数は、画面の「N件の資料」の表示と、表の行数（見出しを除く）を報告する。
module.exports = async ({ page, WEB, shot, log, args }) => {
  await page.goto(`${WEB}/?tab=documents`, { waitUntil: 'networkidle' });
  await page.getByRole('columnheader', { name: '資料名' }).waitFor({ timeout: 30000 });
  await page.waitForTimeout(1000);
  const rows = (await page.getByRole('row').count()) - 1;
  const summary = await page.locator('.docs-summary').first().textContent().catch(() => null);
  log('表の行数:', rows, '／ 画面の表示:', summary && summary.trim());
  await shot(args.shot || 'documents');
};
