import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

interface AccountMenuProps {
  /** ログイン中の利用者の表示名（メールアドレスなど）。 */
  label: string;
  onSignOut: () => void;
}

/**
 * サイドバー下部のアカウント。表示名のボタンを押すと、メニュー（ログアウト）が上に開く。
 * 開いたら先頭の項目にフォーカスを置く。↑↓ で移動、Esc で閉じてボタンへ戻る。メニューの外を押しても閉じる。
 */
export function AccountMenu({ label, onSignOut }: AccountMenuProps) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]')?.focus();
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

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]') ?? [])];
    const index = items.findIndex((n) => n === document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      items[(index + (event.key === 'ArrowDown' ? 1 : -1) + items.length) % items.length]?.focus();
    } else if (event.key === 'Tab') {
      close(false);
    }
  };

  return (
    <div className="ws-account">
      {open && (
        <div ref={menuRef} id={menuId} role="menu" aria-label="アカウント" className="ws-account-menu" onKeyDown={handleKeyDown}>
          <p className="ws-account-who">{label}</p>
          <button
            type="button"
            role="menuitem"
            className="ws-account-item"
            onClick={() => {
              setOpen(false);
              onSignOut();
            }}
          >
            ログアウト
          </button>
        </div>
      )}
      <button
        ref={buttonRef}
        type="button"
        className="ws-account-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="ws-account-avatar" aria-hidden="true">
          {[...label][0]?.toUpperCase() ?? ''}
        </span>
        <span className="ws-account-name">{label}</span>
        <span aria-hidden="true">▴</span>
      </button>
    </div>
  );
}
