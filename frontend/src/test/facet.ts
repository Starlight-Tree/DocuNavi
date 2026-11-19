import { screen, waitFor, within } from '@testing-library/react';
import type { UserEvent } from '@testing-library/user-event';

/** 資料一覧の絞り込みのチップ（ChipSelect）。ラベル（左の名前）で探す。値は data-value。 */
const chipButtons = (name: string) =>
  screen
    .queryAllByRole('button')
    .filter((n) => n.classList.contains('chip-select-button') && n.querySelector('.document-filter-label')?.textContent === name);

export const queryFacetButton = (name: string) => chipButtons(name)[0] ?? null;

export const facetButton = (name: string) => {
  const button = queryFacetButton(name);
  if (!button) throw new Error(`チップが無い: ${name}`);
  return button;
};

export const findFacetButton = (name: string) => waitFor(() => facetButton(name));

/** チップ全体（強調・絞り込み中の見た目を見るための要素）。 */
export const facetChip = (name: string) => facetButton(name).closest('.chip-select') as HTMLElement;

/** チップを開いて、選択肢を「表示名 (件数)」の文字で返し、閉じる。 */
export async function facetOptions(user: UserEvent, name: string): Promise<string[]> {
  const button = await findFacetButton(name);
  await user.click(button);
  const menu = screen.getByRole('menu', { name });
  const texts = within(menu)
    .getAllByRole('menuitemradio')
    .map((item) => {
      const label = item.querySelector('.chip-select-item-label')?.textContent ?? '';
      const count = item.querySelector('.chip-select-count')?.textContent;
      return count === undefined || count === null ? label : `${label} (${count})`;
    });
  await user.keyboard('{Escape}');
  return texts;
}

/** チップを開いて、値（data-value）の選択肢を選ぶ。 */
export async function chooseFacet(user: UserEvent, name: string, value: string): Promise<void> {
  await user.click(await findFacetButton(name));
  const menu = screen.getByRole('menu', { name });
  const item = within(menu)
    .getAllByRole('menuitemradio')
    .find((n) => n.getAttribute('data-value') === value);
  if (!item) throw new Error(`選択肢が無い: ${name} / ${value}`);
  await user.click(item);
}
