import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { THEME_KEY } from '../lib/theme';
import { AccountMenu } from './AccountMenu';

function stubSystem(dark: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: dark && query === '(prefers-color-scheme: dark)',
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

async function openMenu(user: ReturnType<typeof userEvent.setup>) {
  render(<AccountMenu onOpenKnowledge={() => {}} knowledgeCurrent={false} />);
  await user.click(screen.getByRole('button', { name: 'メニュー' }));
}

beforeEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
  stubSystem(false);
});
afterEach(() => vi.unstubAllGlobals());

describe('AccountMenu のテーマ', () => {
  it('選んでいないときは「システム」が選択中。開いただけでは何も保存しない', async () => {
    const user = userEvent.setup();
    await openMenu(user);
    expect(screen.getByRole('menuitemradio', { name: 'システム' })).toBeChecked();
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();
  });

  it('ダーク・ライトを選ぶと、data-theme と localStorage に残り、選択中の表示も変わる', async () => {
    const user = userEvent.setup();
    await openMenu(user);
    await user.click(screen.getByRole('menuitemradio', { name: 'ダーク' }));
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark');
    expect(screen.getByRole('menuitemradio', { name: 'ダーク' })).toBeChecked();

    await user.click(screen.getByRole('menuitemradio', { name: 'ライト' }));
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light');
    expect(screen.getByRole('menuitemradio', { name: 'ダーク' })).not.toBeChecked();
  });

  it('「システム」を選ぶと、保存と data-theme を消して OS に従う', async () => {
    const user = userEvent.setup();
    await openMenu(user);
    await user.click(screen.getByRole('menuitemradio', { name: 'ダーク' }));
    await user.click(screen.getByRole('menuitemradio', { name: 'システム' }));
    expect(document.documentElement.dataset.theme).toBeUndefined();
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();
    expect(screen.getByRole('menuitemradio', { name: 'システム' })).toBeChecked();
  });

  it('保存済みの選択があれば、開いたとき、その選択が選択中', async () => {
    window.localStorage.setItem(THEME_KEY, 'dark');
    const user = userEvent.setup();
    await openMenu(user);
    expect(screen.getByRole('menuitemradio', { name: 'ダーク' })).toBeChecked();
  });

  it('キーボード: ↓ でテーマの項目へ移れ、Enter で選べる', async () => {
    const user = userEvent.setup();
    await openMenu(user);
    await user.keyboard('{ArrowDown}'); // ナレッジ → ライト
    expect(screen.getByRole('menuitemradio', { name: 'ライト' })).toHaveFocus();
    await user.keyboard('{ArrowDown}{Enter}');
    expect(document.documentElement.dataset.theme).toBe('dark');
  });
});
