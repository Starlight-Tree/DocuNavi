import { fireEvent, screen, waitFor } from '@testing-library/react';
import { expect } from 'vitest';

/** PDF ビューアのツールバーの、ページ番号の欄（入力できる）。 */
export const pageBox = (options: { hidden?: boolean } = {}) => screen.findByRole('textbox', { name: 'ページ番号', ...options });

/** ビューアが、n ページ目（総数 `total`）を見ている。欄の値が n になり、「/ 総数」が出る。欄を返す。 */
export async function atPdfPage(n: number, total = 3, options: { hidden?: boolean } = {}) {
  const box = await pageBox(options);
  await waitFor(() => expect(box).toHaveValue(String(n)));
  expect(screen.getByText(`/ ${total}`)).toBeInTheDocument();
  // 欄の値（初期値は 1）と総数は、ページの枠を並べる前から出る。枠が並ぶ前に scrollHeight を読んでスクロールすると位置が空振りし、欄が 1 のままになる
  // （「いちばん下までスクロールしたら…」が、約 3 回に 1 回落ちた。CI でも 2026-11-22 に落ちた）。枠が並ぶまで待つ
  await waitFor(() => expect(document.querySelectorAll('.pdf-page')).toHaveLength(total));
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
