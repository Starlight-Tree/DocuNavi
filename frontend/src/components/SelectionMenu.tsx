import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

/** メニューの 1 項目。`onSelect` の引数は、確認を閉じたあとにフォーカスを戻す先（メニューのボタン）。 */
export interface SelectionMenuItem {
  key: string;
  label: string;
  disabled?: boolean;
  onSelect: (trigger: HTMLElement) => void;
}

interface SelectionMenuProps {
  items: SelectionMenuItem[];
}

/**
 * 資料一覧の「選択 ▾」。回答に使う資料のまとめ操作（このページ・検索結果すべて・解除・全資料に戻す）を 1 か所に置く。
 * 開いたら先頭の押せる項目にフォーカス。↑↓ で移動、Esc で閉じてボタンへ戻る。選んだら閉じてボタンへ戻る。
 */
export function SelectionMenu({ items }: SelectionMenuProps) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus();
    const onPointer = (event: MouseEvent) => {
      const node = event.target as Node;
      if (!menuRef.current?.contains(node) && !buttonRef.current?.contains(node)) setOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    return () => document.removeEventListener('mousedown', onPointer);
  }, [open]);

  const close = (restore: boolean) => {
    setOpen(false);
    if (restore) buttonRef.current?.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    const nodes = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? [])];
    const index = nodes.findIndex((n) => n === document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      nodes[(index + (event.key === 'ArrowDown' ? 1 : -1) + nodes.length) % nodes.length]?.focus();
    } else if (event.key === 'Tab') {
      close(false);
    }
  };

  return (
    <div className="selection-menu-wrap">
      <button
        ref={buttonRef}
        type="button"
        className="btn btn-secondary btn-small"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        選択 <span aria-hidden="true">▾</span>
      </button>
      {open && (
        <ul ref={menuRef} id={menuId} role="menu" aria-label="回答に使う資料の選択" className="selection-menu" onKeyDown={handleKeyDown}>
          {items.map((item) => (
            <li key={item.key} role="none">
              <button
                type="button"
                role="menuitem"
                className="selection-menu-item"
                disabled={item.disabled}
                onClick={() => {
                  close(true);
                  if (buttonRef.current) item.onSelect(buttonRef.current);
                }}
              >
                {item.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
