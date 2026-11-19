import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

export interface ChipSelectOption {
  /** 選択肢の値（空文字も可）。 */
  value: string;
  label: string;
  /** 右端に出す件数。無ければ出さない。 */
  count?: number | undefined;
}

interface ChipSelectProps {
  /** チップの左の名前（「対象」など）。メニューの名前にもなる。 */
  label: string;
  value: string;
  options: ChipSelectOption[];
  onSelect: (value: string) => void;
  /** 値を選んでいる（絞り込み中）ときの強調。 */
  active?: boolean;
}

/**
 * 「ラベル 値 ▾」のチップと、その真下に開く選択肢のメニュー（OS 標準の select は、位置・色を変えられないので置き換えた）。
 * 作りは入力欄の回答対象（ScopePicker）と同じ: メニューは menuitemradio。開いたら選択中の項目にフォーカス、↑↓ Home End で移動、Esc で閉じてチップへ戻る。
 */
export function ChipSelect({ label, value, options, onSelect, active = false }: ChipSelectProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const menuId = useId();
  const current = options.find((o) => o.value === value);
  const text = current ? (current.count !== undefined ? `${current.label} (${current.count})` : current.label) : '';

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[aria-checked="true"]')?.focus();
    const onPointer = (event: MouseEvent) => {
      if (!rootRef.current?.contains(event.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    return () => document.removeEventListener('mousedown', onPointer);
  }, [open]);

  const close = (restore: boolean) => {
    setOpen(false);
    if (restore) buttonRef.current?.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    const nodes = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]') ?? [])];
    const index = nodes.findIndex((n) => n === document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      close(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      nodes[(index + (event.key === 'ArrowDown' ? 1 : -1) + nodes.length) % nodes.length]?.focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      nodes[event.key === 'Home' ? 0 : nodes.length - 1]?.focus();
    } else if (event.key === 'Tab') {
      close(false);
    }
  };

  return (
    <div ref={rootRef} className="chip-select" data-active={active}>
      <button
        ref={buttonRef}
        type="button"
        className="chip-select-button"
        data-value={value}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        title={text || undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="document-filter-label">{label}</span>
        <span className="chip-select-value">{text}</span>
        <span className="chip-select-caret" aria-hidden="true">
          ▾
        </span>
      </button>
      {open && (
        <ul ref={menuRef} id={menuId} role="menu" aria-label={label} className="chip-select-menu" onKeyDown={handleKeyDown}>
          {options.map((option) => {
            const checked = option.value === value;
            return (
              <li key={option.value} role="none">
                <button
                  type="button"
                  role="menuitemradio"
                  aria-checked={checked}
                  data-value={option.value}
                  className="chip-select-item"
                  onClick={() => {
                    close(true);
                    if (!checked) onSelect(option.value);
                  }}
                >
                  <span className="chip-select-check" aria-hidden="true">
                    {checked ? '✓' : ''}
                  </span>
                  <span className="chip-select-item-label">{option.label}</span>
                  {option.count !== undefined && <span className="chip-select-count">{option.count}</span>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
