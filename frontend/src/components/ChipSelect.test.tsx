import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ChipSelect } from './ChipSelect';

const OPTIONS = [
  { value: 'a', label: 'ファイル名順' },
  { value: 'b', label: '文書名順' },
  { value: 'c', label: '更新が新しい順' },
];

function setup(onSelect = vi.fn()) {
  const user = userEvent.setup();
  render(<ChipSelect label="並び替え" value="a" options={OPTIONS} onSelect={onSelect} />);
  return { user, onSelect };
}

describe('ChipSelect', () => {
  it('いまの値を出し、押すと選択肢が開く。選ぶと値を渡して閉じ、フォーカスはチップへ戻る', async () => {
    const { user, onSelect } = setup();
    const button = screen.getByRole('button', { name: /並び替え/ });
    expect(button).toHaveTextContent('ファイル名順');
    expect(screen.queryByRole('menu')).toBeNull();

    await user.click(button);
    expect(screen.getByRole('menuitemradio', { name: /ファイル名順/ })).toHaveAttribute('aria-checked', 'true');
    await user.click(screen.getByRole('menuitemradio', { name: /文書名順/ }));

    expect(onSelect).toHaveBeenCalledWith('b');
    expect(screen.queryByRole('menu')).toBeNull();
    expect(button).toHaveFocus();
  });

  it('開くと選択中の項目にフォーカスが移り、↑↓ で動き、Esc で値を変えずに閉じる', async () => {
    const { user, onSelect } = setup();
    await user.click(screen.getByRole('button', { name: /並び替え/ }));
    expect(screen.getByRole('menuitemradio', { name: /ファイル名順/ })).toHaveFocus();

    await user.keyboard('{ArrowDown}');
    expect(screen.getByRole('menuitemradio', { name: /文書名順/ })).toHaveFocus();
    await user.keyboard('{ArrowUp}{ArrowUp}');
    expect(screen.getByRole('menuitemradio', { name: /更新が新しい順/ })).toHaveFocus();

    await user.keyboard('{Escape}');
    expect(screen.queryByRole('menu')).toBeNull();
    expect(screen.getByRole('button', { name: /並び替え/ })).toHaveFocus();
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('外を押すと閉じる。選択中の項目を選び直しても値は変えない', async () => {
    const { user, onSelect } = setup();
    await user.click(screen.getByRole('button', { name: /並び替え/ }));
    await user.click(document.body);
    expect(screen.queryByRole('menu')).toBeNull();

    await user.click(screen.getByRole('button', { name: /並び替え/ }));
    await user.click(screen.getByRole('menuitemradio', { name: /ファイル名順/ }));
    expect(onSelect).not.toHaveBeenCalled();
  });
});
