import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';
import { ScopePicker } from './ScopePicker';
import type { TargetMenuItem } from './ScopePicker';

function items(checkedKey: string | null): TargetMenuItem[] {
  return [
    { key: 'all', scope: { kind: 'all' }, title: 'すべての資料', checked: checkedKey === 'all' },
    { key: 'zahard', scope: { kind: 'target', name: 'zahard' }, title: 'ザーハード', checked: checkedKey === 'zahard' },
    { key: 'lune', scope: { kind: 'target', name: 'lune' }, title: 'リューネ工房', checked: checkedKey === 'lune' },
  ];
}

function setup(checkedKey: string | null) {
  const user = userEvent.setup();
  const view = render(
    <ScopePicker targetLabel="対象" items={items(checkedKey)} fixed={false} disabled={false} onSelect={vi.fn()} />,
  );
  return { user, view };
}

describe('ScopePicker', () => {
  it('開くと、選ばれている項目にフォーカスが移る（先頭でなくても）。どれも選ばれていなければ最初の項目', async () => {
    const first = setup('lune');
    await first.user.click(screen.getByRole('button', { name: /^回答対象/ }));
    expect(screen.getByRole('menuitemradio', { name: /^リューネ工房/ })).toHaveFocus();
    first.view.unmount();

    const second = setup(null);
    await second.user.click(screen.getByRole('button', { name: /^回答対象/ }));
    expect(screen.getByRole('menuitemradio', { name: /^すべての資料/ })).toHaveFocus();
  });
});
