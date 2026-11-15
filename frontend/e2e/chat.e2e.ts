import { expect, test } from '@playwright/test';
import type { Page } from '@playwright/test';

// シナリオ 1: 実ブラウザー → Next.js の画面（別オリジン）→ FastAPI（CORS・/chat/stream）→ 偽の検索・偽の LLM → 画面。
// 確かめるのは配線（画面が開き、別オリジンの API から SSE のストリームが画面に届く）。回答の中身は pytest / vitest の役目。
test('質問を送ると、偽の検索の出典つきの回答が画面に出る', async ({ page }) => {
  await page.goto('/?tab=chat');

  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await expect(input).toBeVisible();

  await input.fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();

  // 偽の LLM の返答（tests/fakes.py の SearchingMockLLM）
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  // コピーの隣の「参照資料 N 件」を開くと、一覧に資料名とページが出る（FakeSearch が返した値）
  await page.getByRole('button', { name: /^参照資料 \d+ 件/ }).click();
  await expect(page.getByRole('region', { name: '参照資料' }).getByRole('button', { name: /ザーハード1\.pdf/ })).toBeVisible();
  // 回答が終わり、送信できる状態に戻る
  await expect(input).toBeEnabled();
});

// シナリオ 2〜: 回答対象（対象）。偽の DB（tests/e2e_server.py の _FakePool）が、対象 5 つ（ザーハード・みなとコーヒー・つばさ大学学生寮・
// あおば電機 AW-820・準備中の対象。資料が 1 件も見えない対象は出ない）と資料種別 4 つを返し、偽の検索はザーハード（zahard）だけを持つ。対象が違えば 0 件になる。
const targetButton = (page: Page) => page.getByRole('button', { name: /^回答対象/ });
// 上部に「チャット」「資料」の入口は無い。資料一覧へは左下のメニューの「ナレッジ」、チャットへはロゴで戻る
const docsLink = {
  click: async (page: Page) => {
    await page.locator('.ws-account-button').click();
    await page.getByRole('menuitem', { name: 'ナレッジ' }).click();
  },
};
const chatLink = (page: Page) => page.getByRole('link', { name: 'DocuNavi' });
const pickTarget = async (page: Page, name: RegExp) => {
  await targetButton(page).click();
  await page.getByRole('menuitemradio', { name }).click();
};

test('対象を選んで質問すると、その対象の出典が出る。会話の途中で別の対象を選ぶと確認が出る', async ({ page }) => {
  await page.goto('/?tab=chat');
  await expect(targetButton(page)).toBeVisible();
  await expect(targetButton(page)).toContainText('すべての資料');
  // 回答対象は、入力欄の中のチップ 1 か所（入力欄の上のチップ列も、チャットの上の帯も無い）
  await expect(page.locator('.composer-field').getByRole('button', { name: /^回答対象/ })).toBeVisible();
  await expect(page.getByRole('group', { name: '検索範囲' })).toHaveCount(0);

  await pickTarget(page, /^ザーハード/);
  await expect(targetButton(page)).toContainText('ザーハード');
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await input.fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();

  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  // 「参照資料 N 件」を開くと、資料名の一覧が出る（対象名は出さない）
  await page.getByRole('button', { name: /^参照資料 \d+ 件/ }).click();
  const references = page.getByRole('region', { name: '参照資料' });
  await expect(references.getByRole('button', { name: /ザーハード1\.pdf/ })).toBeVisible();
  await expect(input).toBeEnabled();

  // 会話の途中で別の対象を選ぶと、確認が出る。初期フォーカスは「いまの会話を続ける」
  await pickTarget(page, /^みなとコーヒー/);
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'いまの会話を続ける' })).toBeFocused();

  // Esc で閉じると、対象は変わらず、フォーカスはメニューのボタンに戻る。会話も残る
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(targetButton(page)).toBeFocused();
  await expect(targetButton(page)).toContainText('ザーハード');
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();

  // 「新しい会話にする」で、会話を閉じて新しい対象で始める。入力欄にフォーカスが移る
  await pickTarget(page, /^みなとコーヒー/);
  await dialog.getByRole('button', { name: '新しい会話にする' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeHidden();
  await expect(targetButton(page)).toContainText('みなとコーヒー');
  await expect(input).toBeFocused();
});

test('対象に資料が無いと案内が出て、「すべての資料で聞き直す」で、対象なしの新しい会話として質問が送り直される', async ({ page }) => {
  await page.goto('/?tab=chat');
  await pickTarget(page, /^みなとコーヒー/);
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();

  await expect(page.getByText('「みなとコーヒー」の中には、該当する資料がありませんでした。')).toBeVisible();
  await page.getByRole('button', { name: 'すべての資料で聞き直す' }).click();

  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  await expect(page.getByText('ザーハードの大気について教えて')).toHaveCount(1);
  await expect(page.getByText(/の中には、該当する資料がありませんでした/)).toBeHidden();
  await expect(targetButton(page)).toContainText('すべての資料');
});

test('資料一覧の対象の選択は一覧を絞るだけ。資料ごとのチェックが入力欄の「回答対象」に反映され、質問は選んだ資料だけで探す', async ({ page }) => {
  await page.goto('/?tab=documents');
  const targetSelect = page.getByRole('combobox', { name: '対象' });
  await expect(targetSelect).toBeVisible();
  // ヒーローと大きなグループカードは出さない。一覧はテーブル。操作は 検索・対象・資料種別・並び替え だけ（先頭の列は回答に使う資料のチェック）
  await expect(page.getByRole('region', { name: '最近更新された資料' })).toHaveCount(0);
  await expect(page.getByRole('group', { name: /資料グループ/ })).toHaveCount(0);
  await expect(page.getByRole('columnheader')).toHaveText(['', '資料名', '対象', '資料種別', 'ページ数', '更新日時']);
  await expect(page.getByRole('combobox', { name: '資料種別' })).toBeVisible();
  await expect(page.getByRole('combobox', { name: '並び替え' })).toBeVisible();
  await expect(page.getByRole('button', { name: /^フィルター/ })).toHaveCount(0);
  await expect(page.getByRole('combobox', { name: '状態' })).toHaveCount(0);
  await expect(page.getByRole('button', { name: /回答に使う$/ })).toHaveCount(0); // 「全○資料を回答に使う」は無い

  // 既定は全資料が対象（検索できる 7 件。取り込み中の 1 件は選べない）
  const summary = page.locator('.docs-summary');
  await expect(summary).toHaveText('8件の資料 · すべて回答対象');
  await expect(page.getByRole('checkbox', { name: '準備中の資料 を回答に使う' })).toBeDisabled();
  await page.getByRole('button', { name: '選択', exact: true }).click();
  await expect(page.getByRole('menuitem', { name: '全資料に戻す' })).toHaveCount(0); // 一部選択のときだけ出る
  await page.keyboard.press('Escape');

  // 対象を選んでも、一覧が絞られるだけで、選択は変わらない。見出しのチェックはこのページだけに効く
  await targetSelect.selectOption('minato-coffee');
  await expect(page).toHaveURL(/target=minato-coffee/);
  await expect(page.getByRole('row')).toHaveCount(2); // 見出し + みなとコーヒーの 1 件
  await expect(page.getByRole('columnheader')).toHaveText(['', '資料名', '資料種別', 'ページ数', '更新日時']);
  await expect(summary).toHaveText('検索結果 1件 · 回答対象 7件');
  await page.getByRole('checkbox', { name: 'このページの 1 件を全選択・解除' }).uncheck();
  await expect(summary).toHaveText('検索結果 1件 · 回答対象 6件');
  await targetSelect.selectOption('');
  await expect(page.getByRole('checkbox', { name: '店舗案内 を回答に使う' })).not.toBeChecked();
  await expect(page.getByRole('checkbox', { name: 'ザーハード 1 を回答に使う' })).toBeChecked();

  // チャットに戻ると、入力欄の回答対象に反映されている（質問は送られていない）
  await chatLink(page).click();
  await expect(targetButton(page)).toContainText('選択した資料');
  await expect(targetButton(page)).toContainText('6件');
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toHaveCount(0);

  // 選んだ資料（ザーハード 1〜3 を含む）で質問すると、選んだ資料の ID が送られ、その資料から答える
  const request = page.waitForRequest((r) => r.url().includes('/chat/stream') && r.method() === 'POST');
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  expect(JSON.parse((await request).postData() ?? '{}').document_ids).toEqual([1, 2, 3, 5, 6, 7]);
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
});

test('選んだ資料にザーハードが無ければ、その質問は該当なしになる。0 件にすると送れず、「全資料に戻す」で戻る', async ({ page }) => {
  await page.goto('/?tab=documents');
  await page.getByRole('checkbox', { name: 'このページの 7 件を全選択・解除' }).uncheck();
  await expect(page.locator('.docs-summary')).toHaveText('8件の資料 · 0件を回答対象');
  await expect(page.getByRole('group', { name: '回答に使う資料' }).getByRole('status')).toContainText('1 件も選ばれていない');

  // 0 件では送れない
  await chatLink(page).click();
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await expect(page.getByRole('button', { name: '送信' })).toBeDisabled();

  // 店舗案内だけ選ぶ。ザーハードの資料は選ばれていないので、大気の質問は該当なし
  await docsLink.click(page);
  await page.getByRole('checkbox', { name: '店舗案内 を回答に使う' }).check();
  await expect(page.locator('.docs-summary')).toHaveText('8件の資料 · 1件を回答対象');
  await chatLink(page).click();
  await expect(page.getByRole('button', { name: '送信' })).toBeEnabled();
  await page.getByRole('button', { name: '送信' }).click();
  await expect(page.getByText('質問に該当する設定資料は見つかりませんでした。')).toBeVisible();

  // 全資料に戻す（会話は選んだ資料の質問ごとの適用なので、確認は出ない）
  await docsLink.click(page);
  await page.getByRole('button', { name: '選択', exact: true }).click();
  await page.getByRole('menuitem', { name: '全資料に戻す' }).click();
  await expect(page.getByRole('alertdialog')).toBeHidden();
  await expect(page.locator('.docs-summary')).toHaveText('8件の資料 · すべて回答対象');
  await chatLink(page).click();
  await expect(targetButton(page)).toContainText('すべての資料');
});

test('旧 URL（?collection=）は旧フィルターとして知らせ、解除できる。対象と同時なら AND で重ねる', async ({ page }) => {
  await page.goto('/?tab=documents&collection=guides');
  const banner = page.getByText('旧フィルターが有効です').locator('..');
  await expect(banner).toBeVisible();
  await expect(page.getByRole('row')).toHaveCount(3); // 見出し + 規程・取扱説明書の 2 件

  await page.getByRole('combobox', { name: '対象' }).selectOption('aoba-aw820');
  await expect(page).toHaveURL(/collection=guides/);
  await expect(page).toHaveURL(/target=aoba-aw820/);
  await expect(page.getByRole('row')).toHaveCount(2);

  await page.getByRole('button', { name: '旧フィルターを解除' }).click();
  await expect(page.getByText('旧フィルターが有効です')).toHaveCount(0);
  await expect(page).not.toHaveURL(/collection=/);
  await expect(page).toHaveURL(/target=aoba-aw820/);
});

test('対象を選んだ会話で「全資料に戻す」を押すと、資料一覧の上に確認が出る。Esc で閉じるとボタンにフォーカスが戻り、会話は変わらない', async ({ page }) => {
  await page.goto('/?tab=chat');
  await pickTarget(page, /^ザーハード/);
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();

  await docsLink.click(page);
  // 対象を選んだ会話では、その対象の資料にチェックが付く
  await expect(page.locator('.docs-summary')).toHaveText('8件の資料 · 3件を回答対象');
  const reset = page.getByRole('button', { name: '選択', exact: true }); // 確認を閉じたあと、フォーカスはこのメニューのボタンに戻る
  await reset.click();
  await page.getByRole('menuitem', { name: '全資料に戻す' }).click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog.getByRole('button', { name: 'いまの会話を続ける' })).toBeFocused();

  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(reset).toBeFocused();
  await expect(page).toHaveURL(/tab=documents/);

  await reset.click();
  await page.getByRole('menuitem', { name: '全資料に戻す' }).click();
  await dialog.getByRole('button', { name: '新しい会話にする' }).click();
  await expect(page).toHaveURL(/tab=documents/); // 資料一覧に留まる
  await expect(page.locator('.docs-summary')).toHaveText('8件の資料 · すべて回答対象');
  await chatLink(page).click();
  await expect(targetButton(page)).toContainText('すべての資料');
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeHidden();
  await expect(page.getByRole('textbox', { name: 'メッセージ' })).toBeFocused();
});

test('対象の一覧を読めなくても、「すべて」での質問は妨げられない（対象は選べず、資料一覧は識別名の絞り込みになる）', async ({ page }) => {
  await page.route('**/targets', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '資料の分類の情報がまだ用意されていません。' }),
    }),
  );
  await page.goto('/?tab=chat');
  await expect(page.getByText('対象の一覧を読み込めませんでした。')).toBeVisible();
  await expect(targetButton(page)).toHaveCount(0);

  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();

  await docsLink.click(page);
  await expect(page.getByRole('combobox', { name: '対象' })).toBeVisible();
  await expect(page.getByRole('button', { name: /回答に使う$/ })).toHaveCount(0);
});

test('最初の画面はチャットで、上部にタブは無い。左下のメニューの「ナレッジ」で資料一覧へ、ロゴでチャットへ戻り、入力途中の文は残る', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('textbox', { name: 'メッセージ' })).toBeVisible();
  await expect(page).toHaveURL(/\/$/); // URL は書き換えない
  await expect(page.getByRole('navigation', { name: '表示する画面' })).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: '画面' })).toHaveCount(0); // 上部に「チャット」「資料」の入口は無い
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await input.fill('書きかけ');

  await docsLink.click(page);
  await expect(page).toHaveURL(/tab=documents/);
  await expect(page.getByRole('combobox', { name: '対象' })).toBeVisible();
  await expect(page.getByRole('heading', { name: 'ナレッジ' })).toBeVisible();

  await page.getByRole('link', { name: 'DocuNavi' }).click();
  await expect(page).toHaveURL(/tab=chat/);
  await expect(input).toHaveValue('書きかけ');
  await docsLink.click(page);
  await page.getByRole('main', { name: 'ナレッジ' }).getByRole('button', { name: '閉じる' }).click(); // 右上の「× 閉じる」
  await expect(page).toHaveURL(/tab=chat/);
  await expect(input).toHaveValue('書きかけ');
  await expect(input).toBeFocused();
});

test('PC: 左サイドバーを閉じると、アイコンだけの細いレールが残る。履歴と文字は隠れ、会話・入力途中の文・回答対象は変わらず、資料一覧でも同じ', async ({ page }) => {
  await page.goto('/?tab=chat');
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await pickTarget(page, /^ザーハード/);
  await input.fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  await input.fill('書きかけ');

  const sidebar = page.locator('.ws-sidebar');
  const rail = page.getByRole('navigation', { name: 'サイドバー（縮小表示）' });
  const chat = page.locator('.chat-pane');
  const openWidth = (await chat.boundingBox())!.width;
  const openSideWidth = (await sidebar.boundingBox())!.width;
  await expect(rail).toBeHidden(); // 開いている間は、レールを出さない

  await page.getByRole('button', { name: 'サイドバーを閉じる' }).click();
  await expect(rail).toBeVisible();
  const railWidth = (await sidebar.boundingBox())!.width;
  expect(railWidth).toBeLessThan(80);
  expect(railWidth).toBeLessThan(openSideWidth);
  await expect(page.getByRole('heading', { name: '会話の履歴' })).toBeHidden(); // 履歴と文字は、操作・読み上げからも外れる
  await expect(page.getByText('新しい会話', { exact: true })).toBeHidden();
  await expect(page.getByRole('button', { name: 'サイドバーを開く' })).toBeFocused();
  await expect.poll(async () => (await chat.boundingBox())!.width).toBeGreaterThan(openWidth);
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  await expect(input).toHaveValue('書きかけ');
  await expect(targetButton(page)).toContainText('ザーハード');

  // 各アイコンに読み上げ名とツールチップがあり、選択中の画面が分かる
  const chatIcon = rail.getByRole('link', { name: 'チャット' });
  const knowledgeIcon = rail.getByRole('link', { name: 'ナレッジ' });
  await expect(rail.getByRole('button', { name: '新しい会話' })).toBeVisible();
  await expect(chatIcon).toHaveAttribute('aria-current', 'page');
  await expect(knowledgeIcon).not.toHaveAttribute('aria-current', 'page');
  await chatIcon.hover();
  await expect.poll(() => chatIcon.evaluate((el) => getComputedStyle(el, '::after').content)).toBe('"チャット"');
  await expect.poll(() => chatIcon.evaluate((el) => getComputedStyle(el, '::after').opacity)).toBe('1');

  // レールのアイコンで資料一覧へ移り、ロゴでチャットへ戻る。入力途中の文は残る
  await knowledgeIcon.click();
  await expect(page).toHaveURL(/tab=documents/);
  await expect(page.getByRole('combobox', { name: '対象' })).toBeVisible();
  await expect(knowledgeIcon).toHaveAttribute('aria-current', 'page');
  await expect(chatIcon).not.toHaveAttribute('aria-current', 'page');
  await chatIcon.click();
  await expect(page).toHaveURL(/tab=chat/);
  await expect(input).toHaveValue('書きかけ');

  // 閉じているときのロゴの位置は、開くボタン。カーソルで展開の絵に変わり、ツールチップは「サイドバーを開く」
  const logoButton = rail.getByRole('button', { name: 'サイドバーを開く' });
  await logoButton.hover();
  await expect(logoButton.locator('.ws-rail-logo-open')).toBeVisible();
  await expect(logoButton.locator('.ws-brand-icon')).toBeHidden();
  await expect.poll(() => logoButton.evaluate((el) => getComputedStyle(el, '::after').content)).toBe('"サイドバーを開く"');

  // 開くのは、ロゴの位置のボタンと、レールの空き部分のクリック（左の余白。項目・アイコンの外）。項目のクリックは開閉しない
  await rail.getByRole('link', { name: 'チャット' }).click();
  await expect(rail).toBeVisible();
  const centerOf = async (selector: string) => {
    const box = (await page.locator(selector).first().boundingBox())!;
    return [Math.round(box.x + box.width / 2), Math.round(box.y + box.height / 2)];
  };
  const logoClosed = await centerOf('.ws-rail-logo');
  await sidebar.click({ position: { x: 3, y: 300 } });
  await expect(rail).toBeHidden();
  // 開閉でロゴの位置は動かない
  expect(await centerOf('.ws-brand-icon:visible')).toEqual(logoClosed);
  await expect(page.getByRole('heading', { name: '会話の履歴' })).toBeVisible();
  expect((await sidebar.boundingBox())!.width).toBeGreaterThan(railWidth);
  await expect(input).toHaveValue('書きかけ');
});

test('スマホ幅: 左サイドバーを閉じると完全に隠れ（レールは出ない）、開くボタンはメインの左上に残る', async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 844 });
  await page.goto('/?tab=chat');
  await page.getByRole('button', { name: 'サイドバーを閉じる' }).click();
  await expect(page.locator('.ws-sidebar')).toBeHidden();
  await expect(page.getByRole('navigation', { name: 'サイドバー（縮小表示）' })).toHaveCount(0);
  const open = page.getByRole('button', { name: 'サイドバーを開く' });
  await expect(open).toBeVisible();
  await expect(open).toBeFocused();
  await open.click();
  await expect(page.locator('.ws-sidebar')).toBeVisible();
  await expect(page.getByRole('heading', { name: '会話の履歴' })).toBeVisible();
});

test('ログインを使わない構成では、サイドバーの「会話の履歴」に、一覧を出せない理由を示す（欄は消さない）', async ({ page }) => {
  await page.goto('/?tab=chat');
  const history = page.getByRole('complementary', { name: '会話の一覧' });
  await expect(history.getByRole('heading', { name: '会話の履歴' })).toBeVisible();
  await expect(history.getByText('ログインを使わない構成では、会話の一覧を表示できません。')).toBeVisible();
});

test('参照資料の出典を押すと、チャットのまま右のパネルが開く（URL は抽出テキスト）。Esc で閉じると、パネルの列は残らず、フォーカスは「参照資料 N 件」のボタンへ戻る', async ({ page }) => {
  await page.goto('/?tab=chat');
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  const toggle = page.getByRole('button', { name: /^参照資料 \d+ 件/ });
  await toggle.click();
  await page.getByRole('region', { name: '参照資料' }).getByRole('button', { name: /ザーハード1\.pdf/ }).click();

  const panel = page.getByRole('complementary', { name: /^資料:/ });
  await expect(panel).toBeVisible();
  await expect(page).toHaveURL(/doc=\d+&view=text/);
  await expect(page.getByRole('log', { name: '会話' })).toBeVisible();

  const shellColumns = () => page.locator('.ws-shell').evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  expect(await shellColumns()).toBe(3);
  await page.keyboard.press('Escape'); // パネルにフォーカスがあるとき、通常表示の Esc は閉じる
  await expect(panel).toBeHidden();
  expect(await shellColumns()).toBe(2); // 閉じたら、パネルのための空白列を残さない
  await expect(toggle).toBeFocused();
});
