import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { THEME_KEY } from '../lib/theme';
import { ThemeToggle } from './ThemeToggle';

function stubSystem(dark: boolean) {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: dark && query === '(prefers-color-scheme: dark)',
    media: query,
    addEventListener: () => {},
    removeEventListener: () => {},
  }));
}

beforeEach(() => {
  window.localStorage.clear();
  delete document.documentElement.dataset.theme;
});
afterEach(() => vi.unstubAllGlobals());

describe('ThemeToggle', () => {
  it('ラベルは、切り替えた先のモードを案内する（OS がライトなら「ダークモードに切り替え」）', () => {
    stubSystem(false);
    render(<ThemeToggle />);
    expect(screen.getByRole('button', { name: 'ダークモードに切り替え' })).toBeInTheDocument();
  });

  it('OS がダークなら、はじめから「ライトモードに切り替え」', () => {
    stubSystem(true);
    render(<ThemeToggle />);
    expect(screen.getByRole('button', { name: 'ライトモードに切り替え' })).toBeInTheDocument();
  });

  it('押すと反転し、data-theme と localStorage に残り、ラベルも変わる。押す前は何も保存しない', async () => {
    stubSystem(false);
    const user = userEvent.setup();
    render(<ThemeToggle />);
    expect(window.localStorage.getItem(THEME_KEY)).toBeNull();

    await user.click(screen.getByRole('button', { name: 'ダークモードに切り替え' }));
    expect(document.documentElement.dataset.theme).toBe('dark');
    expect(window.localStorage.getItem(THEME_KEY)).toBe('dark');

    await user.click(screen.getByRole('button', { name: 'ライトモードに切り替え' }));
    expect(document.documentElement.dataset.theme).toBe('light');
    expect(window.localStorage.getItem(THEME_KEY)).toBe('light');
  });

  it('キーボード（Tab で届き、Enter と Space で押せる）', async () => {
    stubSystem(false);
    const user = userEvent.setup();
    render(<ThemeToggle />);
    await user.tab();
    const button = screen.getByRole('button', { name: 'ダークモードに切り替え' });
    expect(button).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(document.documentElement.dataset.theme).toBe('dark');
    await user.keyboard(' ');
    expect(document.documentElement.dataset.theme).toBe('light');
  });

  it('保存済みの選択があれば、OS と違っても、その選択で始まる', () => {
    stubSystem(false);
    document.documentElement.dataset.theme = 'dark'; // 描画前のスクリプトが付けた状態
    render(<ThemeToggle />);
    expect(screen.getByRole('button', { name: 'ライトモードに切り替え' })).toBeInTheDocument();
  });
});
