import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode, RefObject } from 'react';

interface ModalProps {
  title: string;
  /** 本文の説明（読み上げ用）。画面には出さない。 */
  description?: string;
  /** 開いたときにフォーカスを置く要素。無ければ、ダイアログの最初の操作できる要素。 */
  initialFocus?: RefObject<HTMLElement | null>;
  /** 背景のクリックで閉じるか。取り消せない操作の確認は false。 */
  closeOnBackdrop?: boolean;
  width?: 'narrow' | 'wide';
  onClose: () => void;
  children: ReactNode;
}

const FOCUSABLE = 'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

/**
 * 共通のダイアログ。Esc で閉じ、Tab は中で循環し、閉じたら開く前の要素へフォーカスを戻す。
 * 背景の画面は、呼び出し側（MockApp）が `inert` にして、操作と読み上げから外す。
 */
export function Modal({ title, description, initialFocus, closeOnBackdrop = true, width = 'narrow', onClose, children }: ModalProps) {
  const titleId = useId();
  const descId = useId();
  const rootRef = useRef<HTMLDivElement>(null);

  // 開く前にフォーカスがあった要素。背景を inert にすると、その要素のフォーカスは外れるので、描画のうちに控える
  const [opener] = useState(() => (document.activeElement instanceof HTMLElement && document.activeElement !== document.body ? document.activeElement : null));

  useEffect(() => {
    const target = initialFocus?.current ?? rootRef.current?.querySelector<HTMLElement>(FOCUSABLE) ?? rootRef.current;
    target?.focus();
    return () => {
      if (opener?.isConnected) opener.focus();
    };
    // 開いた一度だけ
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      onClose();
      return;
    }
    if (event.key !== 'Tab') return;
    const items = [...(rootRef.current?.querySelectorAll<HTMLElement>(FOCUSABLE) ?? [])];
    const first = items[0];
    const last = items[items.length - 1];
    if (!first || !last) return;
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return (
    <div className="mk-backdrop" onMouseDown={(event) => closeOnBackdrop && event.target === event.currentTarget && onClose()}>
      <div
        ref={rootRef}
        className={`mk-modal mk-modal-${width}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-describedby={description ? descId : undefined}
        tabIndex={-1}
        onKeyDown={handleKeyDown}
      >
        <header className="mk-modal-head">
          <h2 id={titleId} className="mk-modal-title">
            {title}
          </h2>
          <button type="button" className="mk-icon-btn" aria-label="閉じる" onClick={onClose}>
            ×
          </button>
        </header>
        {description && (
          <p id={descId} className="visually-hidden">
            {description}
          </p>
        )}
        {children}
      </div>
    </div>
  );
}
