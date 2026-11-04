import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { ChatScope } from '../hooks/useChat';

/** メニューの 1 項目。 */
export interface TargetMenuItem {
  key: string;
  scope: ChatScope;
  title: string;
  /** 見える資料の数。分からないときは渡さない。 */
  count?: number | undefined;
  note?: string | undefined;
  checked: boolean;
  /** 旧グループの会話の項目（いまの範囲を示すだけで、選べない）。 */
  disabled?: boolean | undefined;
}

interface ScopePickerProps {
  /** 回答対象の表示名（「すべての資料」・対象の表示名・「旧グループ: …」）。 */
  targetLabel: string;
  /** 対象の資料の数。分からないとき（一覧を読めなかった、会話の範囲が不明）は渡さない。 */
  documentCount?: number | undefined;
  /** 選べる項目。null は、対象の一覧を使えない（旧 API・読み込み中・対象が 0 件）ので、変更の操作を出さない。 */
  items: TargetMenuItem[] | null;
  /** 対象の一覧を読めなかった（再試行を添える）。 */
  failed?: boolean;
  onRetry?: (() => void) | undefined;
  /** 会話がある（回答対象は会話ごとに固定）。 */
  fixed: boolean;
  /** 回答を作っている間は変えられない。 */
  disabled: boolean;
  /** 回答対象を選んだ。`trigger` は、確認を閉じたあとにフォーカスを戻す先（チップのボタン）。 */
  onSelect: (scope: ChatScope, trigger: HTMLElement | null) => void;
}

/**
 * 入力欄の中に置く「回答対象：○○・○ 件」のチップ。いま何を回答に使うか（回答対象）と、変更する操作を、ここ 1 か所に置く。
 * 変更はメニュー（入力欄の上に開く。対象が増えても、横長の列に頼らない）。会話があるときに選ぶと、呼び出し側が確認を出して、新しい会話にする。
 * 開いたら、選ばれている項目にフォーカスを置く。↑↓ で移動、Esc で閉じてチップへ戻る。
 */
export function ScopePicker({ targetLabel, documentCount, items, failed = false, onRetry, fixed, disabled, onSelect }: ScopePickerProps) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    menuRef.current
      ?.querySelector<HTMLElement>('[aria-checked="true"]:not([disabled]), [role="menuitemradio"]:not([disabled])')
      ?.focus();
    const onPointer = (event: MouseEvent) => {
      const node = event.target as Node;
      if (!menuRef.current?.contains(node) && !buttonRef.current?.contains(node)) setOpen(false);
    };
    document.addEventListener('mousedown', onPointer);
    return () => document.removeEventListener('mousedown', onPointer);
  }, [open]);

  // 生成が始まる・変更できなくなったら、開いたままにしない
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  const close = (restore: boolean) => {
    setOpen(false);
    if (restore) buttonRef.current?.focus();
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLUListElement>) => {
    const nodes = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitemradio"]:not([disabled])') ?? [])];
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

  const value = (
    <>
      回答対象：<strong>{targetLabel}</strong>
      {documentCount !== undefined && ` ・ ${documentCount} 件`}
    </>
  );

  return (
    <div className="scope-picker">
      {items ? (
        <div className="scope-picker-wrap">
          <button
            ref={buttonRef}
            type="button"
            className="scope-chip"
            aria-haspopup="menu"
            aria-expanded={open}
            aria-controls={open ? menuId : undefined}
            disabled={disabled}
            title={
              disabled
                ? '回答の生成中は変えられません'
                : '回答対象は会話ごとに固定です。会話があるときに変えると、確認のあとで新しい会話になります。'
            }
            onClick={() => setOpen((v) => !v)}
          >
            <span>{value}</span>
            <span aria-hidden="true">▾</span>
          </button>
          {open && (
            <ul ref={menuRef} id={menuId} role="menu" aria-label="回答対象を選ぶ" className="scope-menu" onKeyDown={handleKeyDown}>
              {items.map((item) => (
                <li key={item.key} role="none">
                  <button
                    type="button"
                    role="menuitemradio"
                    aria-checked={item.checked}
                    className="scope-menu-item"
                    disabled={item.disabled}
                    onClick={() => {
                      close(true);
                      if (!item.checked) onSelect(item.scope, buttonRef.current);
                    }}
                  >
                    <span className="scope-menu-check" aria-hidden="true">
                      {item.checked ? '✓' : ''}
                    </span>
                    <span className="scope-menu-text">
                      <span>{item.title}</span>
                      {item.note && <span className="scope-menu-note">{item.note}</span>}
                    </span>
                    {item.count !== undefined && <span className="scope-menu-count">{item.count}件</span>}
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : (
        <span className="scope-chip scope-chip-static">{value}</span>
      )}
      {items && <span className="scope-picker-hint">{disabled ? '回答の生成中は変えられません' : fixed ? 'この会話で固定' : '最初の質問で固定'}</span>}
      {failed && (
        <span className="scope-picker-failure" role="status">
          対象の一覧を読み込めませんでした。
          {onRetry && (
            <button type="button" className="btn-link" onClick={onRetry}>
              再試行
            </button>
          )}
        </span>
      )}
    </div>
  );
}
