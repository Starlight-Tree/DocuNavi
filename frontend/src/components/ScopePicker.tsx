import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import type { ChatScope } from '../hooks/useChat';

/** 資料（書類）の線画。アイコンのライブラリは使わず、線だけの SVG を直に置く。 */
function DocsIcon() {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M5 2.5h6.5L15 6v11.5H5z" />
      <path d="M11.5 2.5V6H15" />
      <path d="M7.5 10h5M7.5 13h5" />
    </svg>
  );
}

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
  /** 資料を選んでいる（資料一覧のチェック）。この間の回答対象は、会話に固定されず、質問ごとに送る。 */
  selecting?: boolean;
  /** 回答を作っている間は変えられない。 */
  disabled: boolean;
  /** 回答対象を選んだ。`trigger` は、確認を閉じたあとにフォーカスを戻す先（チップのボタン）。 */
  onSelect: (scope: ChatScope, trigger: HTMLElement | null) => void;
}

/**
 * 入力欄の中に置く「資料アイコン ○○ · ○件」の控えめなチップ（「回答対象：」は読み上げだけ）。いま何を回答に使うか（回答対象）と、変更する操作を、ここ 1 か所に置く。
 * 変更はメニュー（入力欄の上に開く。対象が増えても、横長の列に頼らない）。会話があるときに選ぶと、呼び出し側が確認を出して、新しい会話にする。
 * 開いたら、選ばれている項目にフォーカスを置く。↑↓ で移動、Esc で閉じてチップへ戻る。
 */
export function ScopePicker({
  targetLabel,
  documentCount,
  items,
  failed = false,
  onRetry,
  fixed,
  selecting = false,
  disabled,
  onSelect,
}: ScopePickerProps) {
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLUListElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    // 選ばれている項目へ。無ければ（選べない項目だけが選ばれている場合を含む）、最初の選べる項目へ。
    // セレクターを「,」でつなぐと、文書順で先に一致した項目になり、選ばれている項目が 2 番目以降でも先頭に行く。
    (
      menuRef.current?.querySelector<HTMLElement>('[role="menuitemradio"][aria-checked="true"]:not([disabled])') ??
      menuRef.current?.querySelector<HTMLElement>('[role="menuitemradio"]:not([disabled])')
    )?.focus();
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
      <DocsIcon />
      <span>
        <span className="visually-hidden">回答対象：</span>
        <strong>{targetLabel}</strong>
        {documentCount !== undefined && ` · ${documentCount}件`}
      </span>
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
            {value}
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
              <li role="none" className="scope-menu-hint">
                {selecting ? '資料一覧で選択中（質問ごとに適用）' : fixed ? 'この会話で固定' : '最初の質問で固定'}
              </li>
            </ul>
          )}
        </div>
      ) : (
        <span className="scope-chip scope-chip-static">{value}</span>
      )}
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
