// 本番の会話（サイドバーの先頭）を開き、その会話の回答の出典のファイル名と、画面に出る「続けて聞く」候補を報告する。質問は送らない（実 API なし・課金なし）。
// 画面が会話を読むときの応答（/threads/…）の context から、`[n] ファイル名.pdf` の見出しを取り出す。本文・会話の内容は出力しない。
// args: { "title": "寮の門限は何時ですか？" }（開く会話の題名。既定はこの質問）。撮影はしない。
module.exports = async ({ page, WEB, log, args }) => {
  const title = args.title || '寮の門限は何時ですか？';
  const names = [];
  const seen = new Set();
  const NAME = /^\[\d+\] ([^\n]+\.(?:txt|pdf))$|^\[([^\]\n]+\.(?:txt|pdf))\]$/gm;
  const walk = (v) => {
    if (typeof v === 'string') {
      for (const m of v.replace(/\r\n?/g, '\n').matchAll(NAME)) {
        const n = m[1] ?? m[2];
        if (!seen.has(n)) {
          seen.add(n);
          names.push(n);
        }
      }
    } else if (Array.isArray(v)) v.forEach(walk);
    else if (v && typeof v === 'object') Object.values(v).forEach(walk);
  };
  page.on('response', async (res) => {
    try {
      if (!/\/threads\//.test(res.url()) || !(res.headers()['content-type'] || '').includes('json')) return;
      walk(await res.json());
    } catch {
      /* 本文が読めない応答は無視する */
    }
  });

  await page.goto(`${WEB}/?tab=chat`, { waitUntil: 'networkidle' });
  await page.getByText(title).first().click();
  await page.locator('article.message-assistant').first().waitFor({ timeout: 30000 });
  await page.waitForTimeout(2000);
  log('開いた会話の題名:', title);
  log('出典のファイル名:', JSON.stringify(names));
  log('ソースのボタン:', JSON.stringify(await page.getByRole('button', { name: /^\d+ 件のソース/ }).allTextContents()));
  const group = page.getByRole('group', { name: '続けて聞く' });
  log('続けて聞く候補:', JSON.stringify((await group.count()) ? await group.getByRole('button').allTextContents() : []));
  if (names.length === 0) log('（応答から出典の見出しが取れませんでした。応答の形が違う可能性があります）');
};
