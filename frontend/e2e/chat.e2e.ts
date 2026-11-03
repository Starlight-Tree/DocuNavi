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

// シナリオ 2〜: 資料グループ（検索範囲）。偽の DB（tests/e2e_server.py の _FakePool）が、グループ 5 つ（設定資料・店舗案内・規程・取扱説明書・
// 準備中の資料・未登録の memo）を返し、偽の検索は設定資料（zahard）だけを持つ。範囲が違えば 0 件になる。
const scopeChips = (page: Page) => page.getByRole('group', { name: '検索範囲' });

test('範囲を選んで質問すると、その範囲の出典が出て、出典にグループ名が付く。会話の途中で別の範囲を選ぶと確認が出る', async ({ page }) => {
  await page.goto('/?tab=chat');
  await expect(scopeChips(page)).toBeVisible();
  await expect(scopeChips(page).getByRole('button', { name: 'すべて' })).toHaveAttribute('aria-pressed', 'true');

  await scopeChips(page).getByRole('button', { name: '設定資料' }).click();
  await expect(page.getByText('検索範囲: 設定資料')).toBeVisible();
  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await input.fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();

  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  // 回答の下の「参照資料」に、資料名のチップが出る（グループ名はチップに出さない）
  const references = page.getByRole('region', { name: '参照資料' });
  const source = references.getByRole('button', { name: /ザーハード1\.pdf/ });
  await expect(source).toBeVisible();
  await expect(input).toBeEnabled();

  // 会話の途中で別の範囲を選ぶと、確認が出る。初期フォーカスは「いまの会話を続ける」
  const cafe = scopeChips(page).getByRole('button', { name: '店舗案内' });
  await cafe.click();
  const dialog = page.getByRole('alertdialog');
  await expect(dialog).toBeVisible();
  await expect(dialog.getByRole('button', { name: 'いまの会話を続ける' })).toBeFocused();

  // Esc で閉じると、範囲は変わらず、フォーカスは押したチップに戻る。会話も残る
  await page.keyboard.press('Escape');
  await expect(dialog).toBeHidden();
  await expect(cafe).toBeFocused();
  await expect(scopeChips(page).getByRole('button', { name: '設定資料' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();

  // 「新しい会話にする」で、会話を閉じて新しい範囲で始める。入力欄にフォーカスが移る
  await cafe.click();
  await dialog.getByRole('button', { name: '新しい会話にする' }).click();
  await expect(dialog).toBeHidden();
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeHidden();
  await expect(cafe).toHaveAttribute('aria-pressed', 'true');
  await expect(input).toBeFocused();
});

test('範囲に資料が無いと案内が出て、「すべての資料で聞き直す」で、範囲なしの新しい会話として質問が送り直される', async ({ page }) => {
  await page.goto('/?tab=chat');
  await scopeChips(page).getByRole('button', { name: '店舗案内' }).click();
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();

  await expect(page.getByText('「店舗案内」の中には、該当する資料がありませんでした。')).toBeVisible();
  await page.getByRole('button', { name: 'すべての資料で聞き直す' }).click();

  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  await expect(page.getByText('ザーハードの大気について教えて')).toHaveCount(1);
  await expect(page.getByText(/の中には、該当する資料がありませんでした/)).toBeHidden();
  await expect(scopeChips(page).getByRole('button', { name: 'すべて' })).toHaveAttribute('aria-pressed', 'true');
});

test('資料一覧のグループ選択は一覧を絞るだけ。「このグループに質問」でチャットへ移り、範囲が切り替わって入力欄にフォーカスが置かれる', async ({ page }) => {
  await page.goto('/?tab=documents');
  const groupSelect = page.getByRole('combobox', { name: 'グループ' });
  await expect(groupSelect).toBeVisible();
  // ヒーローと大きなグループカードは出さない。一覧はテーブル
  await expect(page.getByRole('region', { name: '最近更新された資料' })).toHaveCount(0);
  await expect(page.getByRole('group', { name: /資料グループ/ })).toHaveCount(0);
  await expect(page.getByRole('columnheader')).toHaveText(['資料名', 'グループ', '状態', 'ページ数', '更新日時']);

  // グループを選んでも、チャットの検索範囲は変わらない
  await groupSelect.selectOption('cafe');
  await expect(page).toHaveURL(/collection=cafe/);
  await expect(page.getByText(/現在のチャットの質問対象: すべての資料/)).toBeVisible();
  await page.getByRole('navigation', { name: '画面' }).getByRole('link', { name: 'チャット' }).click();
  await expect(scopeChips(page).getByRole('button', { name: 'すべて' })).toHaveAttribute('aria-pressed', 'true');

  await page.getByRole('button', { name: '資料一覧へ' }).click();
  await page.getByRole('combobox', { name: 'グループ' }).selectOption('zahard');
  await page.getByRole('button', { name: '設定資料: このグループに質問' }).click();
  await expect(page.getByRole('alertdialog')).toBeHidden(); // 会話が無いので、確認なしで切り替わる
  await expect(page).toHaveURL(/tab=chat/);
  await expect(scopeChips(page).getByRole('button', { name: '設定資料' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('textbox', { name: 'メッセージ' })).toBeFocused();
});

test('会話があるときの「このグループに質問」は、資料一覧の上に確認を出す。Esc で閉じるとボタンにフォーカスが戻り、範囲は変わらない', async ({ page }) => {
  await page.goto('/?tab=chat');
  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();

  await page.getByRole('button', { name: '資料一覧へ' }).click();
  await page.getByRole('combobox', { name: 'グループ' }).selectOption('cafe');
  const ask = page.getByRole('button', { name: '店舗案内: このグループに質問' });
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
  await expect(scopeChips(page).getByRole('button', { name: '店舗案内' })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeHidden();
  await expect(page.getByRole('textbox', { name: 'メッセージ' })).toBeFocused();
});

test('グループの一覧を読めなくても、「すべて」での質問は妨げられない（範囲は選べず、資料一覧は識別名の絞り込みになる）', async ({ page }) => {
  await page.route('**/collections', (route) =>
    route.fulfill({
      status: 503,
      contentType: 'application/json',
      body: JSON.stringify({ detail: '資料グループの情報がまだ用意されていません。' }),
    }),
  );
  await page.goto('/?tab=chat');
  await expect(page.getByText('グループの一覧を読み込めませんでした。')).toBeVisible();
  await expect(scopeChips(page).getByRole('button', { name: '設定資料' })).toHaveCount(0);

  await page.getByRole('textbox', { name: 'メッセージ' }).fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();

  await page.getByRole('button', { name: '資料一覧へ' }).click();
  await expect(page.getByRole('combobox', { name: 'グループ' })).toBeVisible();
  await expect(page.getByRole('button', { name: /このグループに質問/ })).toHaveCount(0);
});

test('最初の画面は資料一覧で、上部にタブは無い。サイドバーの「チャット」でチャットへ、ロゴで資料一覧へ戻り、入力途中の文は残る', async ({ page }) => {
  await page.goto('/');
  await expect(page.getByRole('combobox', { name: 'グループ' })).toBeVisible();
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
  await expect(page.getByRole('combobox', { name: 'グループ' })).toBeVisible();
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
