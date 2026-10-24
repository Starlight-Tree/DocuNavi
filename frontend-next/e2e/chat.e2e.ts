import { expect, test } from '@playwright/test';

// シナリオ 1: 実ブラウザー → Next.js の画面（別オリジン）→ FastAPI（CORS・/chat/stream）→ 偽の検索・偽の LLM → 画面。
// 確かめるのは配線（画面が開き、別オリジンの API から SSE のストリームが画面に届く）。回答の中身は pytest / vitest の役目。
test('質問を送ると、偽の検索の出典つきの回答が画面に出る', async ({ page }) => {
  await page.goto('/');

  const input = page.getByRole('textbox', { name: 'メッセージ' });
  await expect(input).toBeVisible();

  await input.fill('ザーハードの大気について教えて');
  await page.getByRole('button', { name: '送信' }).click();

  // 偽の LLM の返答（tests/fakes.py の SearchingMockLLM）
  await expect(page.getByText('資料から該当箇所が見つかりました。')).toBeVisible();
  // 出典のカード（FakeSearch が返した資料名とページ）
  await expect(page.getByRole('button', { name: /ザーハード1\.pdf/ })).toBeVisible();
  // 回答が終わり、送信できる状態に戻る
  await expect(input).toBeEnabled();
});
