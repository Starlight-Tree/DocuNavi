import { screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

/**
 * 回答の下の「参照資料 N 件」ボタンを押して、一覧（ポップオーバー）を開き、その領域を返す。
 * 開いているときは、押し直さない（閉じてしまうため）。
 */
export async function openSources(): Promise<HTMLElement> {
  const toggle = await screen.findByRole('button', { name: /^参照資料 \d+ 件/ });
  if (toggle.getAttribute('aria-expanded') !== 'true') await userEvent.setup().click(toggle);
  return screen.findByRole('region', { name: '参照資料' });
}
