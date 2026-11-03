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
  // 回答の下の「参照資料」（FakeSearch が返した資料名とページ）
  await expect(page.getByRole('region', { name: '参照資料' }).getByRole('button', { name: /ザーハード1\.pdf/ })).toBeVisible();
  // 回答が終わり、送信できる状態に戻る
  await expect(input).toBeEnabled();
});

// シナリオ 2〜: 質問対象（対象）。偽の DB（tests/e2e_server.py の _FakePool）が、対象 5 つ（ザーハード・みなとコーヒー・つばさ大学学生寮・
// あおば電機 AW-820・準備中の対象。資料が 1 件も見えない対象は出ない）と資料種別 4 つを返し、偽の検索はザーハード（zahard）だけを持つ。対象が違えば 0 件になる。
const targetButton = (page: Page) => page.getByRole('button', { name: /^質問対象/ });
const pickTarget = async (page: Page, name: RegExp) => {
  await targetButton(page).click();
  await page.getByRole('menuitemradio', { name }).click();
};

test('対象を選んで質問すると、その対象の出典が出る。会話の途中で別の対象を選ぶと確認が出る', async ({ page }) => {
  await page.goto('/?tab=chat');
  await expect(targetButton(page)).toBeVisible();
  await expect(targetButton(page)).toContainText('すべての資料');
  // 入力欄の上のチップ列は無い（質問対象の変更は、上の帯のメニュー 1 か所）
  await expect(page.getByRole('group', { name: '検索範囲' })).toHaveCount(0);

  await pickTarget(page, /^ザーハード/);
  await expect(page.getByText('質問対象: ザーハード')).toBeVisible();
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await input.fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();

  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  // 回答の下の「参照資料」に、資料名のチップが出る（対象名はチップに出さない）
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

test('資料一覧の対象の選択は一覧を絞るだけ。「この対象について質問」でチャットへ移り、対象が切り替わって入力欄にフォーカスが置かれる', async ({ page }) => {
  await page.goto('/?tab=documents');
  const targetSelect = page.getByRole('combobox', { name: '対象' });
  await expect(targetSelect).toBeVisible();
  // ヒーローと大きなグループカードは出さない。一覧はテーブル。主な操作は 検索・対象・資料種別 で、そのほかは「詳細フィルター」
  await expect(page.getByRole('region', { name: '最近更新された資料' })).toHaveCount(0);
  await expect(page.getByRole('group', { name: /資料グループ/ })).toHaveCount(0);
  await expect(page.getByRole('columnheader')).toHaveText(['資料名', '対象', '資料種別', '状態', 'ページ数', '更新日時']);
  await expect(page.getByRole('combobox', { name: '資料種別' })).toBeVisible();
  await expect(page.getByRole('button', { name: /詳細フィルター/ })).toHaveAttribute('aria-expanded', 'false');
  await expect(page.getByRole('combobox', { name: '状態' })).toBeHidden();

  // 対象を選んでも、チャットの質問対象は変わらない
  await targetSelect.selectOption('minato-coffee');
  await expect(page).toHaveURL(/target=minato-coffee/);
  await expect(page.getByRole('row')).toHaveCount(2); // 見出し + みなとコーヒーの 1 件
  await expect(page.getByText(/現在のチャットの質問対象: すべての資料/)).toBeVisible();
  await page.getByRole('navigation', { name: '画面' }).getByRole('link', { name: 'チャット' }).click();
  await expect(targetButton(page)).toContainText('すべての資料');

  await page.getByRole('button', { name: '資料一覧へ' }).click();
  await page.getByRole('combobox', { name: '対象' }).selectOption('zahard');
  await page.getByRole('button', { name: 'ザーハード: この対象について質問' }).click();
  await expect(page.getByRole('alertdialog')).toBeHidden(); // 会話が無いので、確認なしで切り替わる
  await expect(page).toHaveURL(/tab=chat/);
  await expect(targetButton(page)).toContainText('ザーハード');
  await expect(page.getByRole('textbox', { name: 'メッセージ' })).toBeFocused();
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

test('会話があるときの「この対象について質問」は、資料一覧の上に確認を出す。Esc で閉じるとボタンにフォーカスが戻り、対象は変わらない', async ({ page }) => {
  await page.goto('/?tab=chat');
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();

  await page.getByRole('button', { name: '資料一覧へ' }).click();
  await page.getByRole('combobox', { name: '対象' }).selectOption('minato-coffee');
  const ask = page.getByRole('button', { name: 'みなとコーヒー: この対象について質問' });
  await ask.click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog.getByRole('button', { name: 'いまの会話を続ける' })).toBeFocused();

  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(ask).toBeFocused();
  await expect(page).toHaveURL(/tab=documents/);

  await ask.click();
  await dialog.getByRole('button', { name: '新しい会話にする' }).click();
  await expect(page).toHaveURL(/tab=chat/);
  await expect(targetButton(page)).toContainText('みなとコーヒー');
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

  await page.getByRole('button', { name: '資料一覧へ' }).click();
  await expect(page.getByRole('combobox', { name: '対象' })).toBeVisible();
  await expect(page.getByRole('button', { name: /この対象について質問/ })).toHaveCount(0);
});

test('最初の画面は資料一覧で、上部にタブは無い。サイドバーの「チャット」でチャットへ、ロゴで資料一覧へ戻り、入力途中の文は残る', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('combobox', { name: '対象' })).toBeVisible();
  await expect(page).toHaveURL(/\/$/); // URL は書き換えない
  await expect(page.getByRole('navigation', { name: '表示する画面' })).toHaveCount(0);
  await expect(page.getByRole('navigation', { name: '画面' }).getByRole('link', { name: '資料' })).toHaveAttribute('aria-current', 'page');

  await page.getByRole('navigation', { name: '画面' }).getByRole('link', { name: 'チャット' }).click();
  await expect(page).toHaveURL(/tab=chat/);
  await expect(page.getByRole('navigation', { name: '画面' }).getByRole('link', { name: 'チャット' })).toHaveAttribute('aria-current', 'page');
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await expect(input).toBeFocused();
  await input.fill('書きかけ');

  await page.getByRole('link', { name: 'ドキュナビ' }).click();
  await expect(page).toHaveURL(/tab=documents/);
  await expect(page.getByRole('combobox', { name: '対象' })).toBeVisible();
  await page.getByRole('navigation', { name: '画面' }).getByRole('link', { name: 'チャット' }).click();
  await expect(input).toHaveValue('書きかけ');
});

test('ログインを使わない構成では、サイドバーの「会話の履歴」に、一覧を出せない理由を示す（欄は消さない）', async ({ page }) => {
  await page.goto('/?tab=chat');
  const history = page.getByRole('complementary', { name: '会話の一覧' });
  await expect(history.getByRole('heading', { name: '会話の履歴' })).toBeVisible();
  await expect(history.getByText('ログインを使わない構成では、会話の一覧を表示できません。')).toBeVisible();
});

test('参照資料の出典を押すと、チャットのまま右のパネルが開く（URL は抽出テキスト）。Esc で閉じると、パネルの列は残らず、フォーカスは出典へ戻る', async ({ page }) => {
  await page.goto('/?tab=chat');
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  const source = page.getByRole('region', { name: '参照資料' }).getByRole('button', { name: /ザーハード1\.pdf/ });
  await source.click();

  const panel = page.getByRole('complementary', { name: /^資料:/ });
  await expect(panel).toBeVisible();
  await expect(page).toHaveURL(/doc=\d+&view=text/);
  await expect(page.getByRole('log', { name: '会話' })).toBeVisible();

  const shellColumns = () => page.locator('.ws-shell').evaluate((el) => getComputedStyle(el).gridTemplateColumns.split(' ').length);
  expect(await shellColumns()).toBe(3);
  await page.keyboard.press('Escape'); // パネルにフォーカスがあるとき、通常表示の Esc は閉じる
  await expect(panel).toBeHidden();
  expect(await shellColumns()).toBe(2); // 閉じたら、パネルのための空白列を残さない
  await expect(source).toBeFocused();
});
