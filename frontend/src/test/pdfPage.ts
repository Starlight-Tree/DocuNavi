import { fireEvent, screen, waitFor } from '@testing-library/react';
import { expect } from 'vitest';

/** PDF ビューアのツールバーの、ページ番号の欄（入力できる）。 */
export const pageBox = (options: { hidden?: boolean } = {}) => screen.findByRole('textbox', { name: 'ページ番号', ...options });

/** ビューアが、n ページ目（総数 `total`）を見ている。欄の値が n になり、「/ 総数」が出る。欄を返す。 */
export async function atPdfPage(n: number, total = 3, options: { hidden?: boolean } = {}) {
  const box = await pageBox(options);
  await waitFor(() => expect(box).toHaveValue(String(n)));
  expect(screen.getByText(`/ ${total}`)).toBeInTheDocument();
  return box;
}

/** ビューアの表示領域（スクロールする領域）。 */
export const pdfStage = () => screen.getByRole('region', { name: 'PDF のページ' });

/** 利用者がスクロールした（位置を決めて、scroll を起こす）。 */
export function scrollPdfTo(top: number) {
  const stage = pdfStage();
  stage.scrollTop = top;
  fireEvent.scroll(stage);
}
