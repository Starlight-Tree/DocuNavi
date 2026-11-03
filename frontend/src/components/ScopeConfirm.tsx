import { useEffect, useRef } from 'react';
import type { KeyboardEvent } from 'react';

interface ScopeConfirmProps {
  /** 切り替え先の表示名（「すべての資料」・対象の表示名）。 */
  targetLabel: string;
  /** 回答を作っている途中か。そうなら、生成を止めることも伝える。 */
  generating: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/**
 * 質問対象は会話ごとに固定なので、会話があるときに変えるには、新しい会話にする。その確認の帯。
 * 開いたら、安全な「いまの会話を続ける」にフォーカスを置く。Esc で閉じる（範囲は変えない）。Tab は帯の中を循環する。
 * 閉じたあとのフォーカスは、呼び出し側が開く前の位置へ戻す。
 */
export function ScopeConfirm({ targetLabel, generating, onConfirm, onCancel }: ScopeConfirmProps) {
  const keepRef = useRef<HTMLButtonElement>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    keepRef.current?.focus();
  }, []);

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'Escape') {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== 'Tab') return;
    // 帯の中で循環させる（先頭の Shift+Tab は末尾へ、末尾の Tab は先頭へ）
    const first = keepRef.current;
    const last = confirmRef.current;
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
    <div
      className="scope-confirm"
      role="alertdialog"
      aria-labelledby="scope-confirm-title"
      aria-describedby="scope-confirm-body"
      onKeyDown={handleKeyDown}
    >
      <div className="scope-confirm-text">
        <p id="scope-confirm-title" className="scope-confirm-title">
          質問対象を「{targetLabel}」に変えますか？
        </p>
        <p id="scope-confirm-body" className="scope-confirm-body">
          質問対象は会話ごとに固定なので、新しい会話に切り替えます。いまの会話は閉じます（ログイン中は、一覧から開き直せます）。
          {generating && '回答を作っている途中なので、生成も止めます。'}
        </p>
      </div>
      <div className="scope-confirm-actions">
        <button ref={keepRef} type="button" className="btn btn-secondary" onClick={onCancel}>
          いまの会話を続ける
        </button>
        <button ref={confirmRef} type="button" className="btn btn-primary" onClick={onConfirm}>
          新しい会話にする
        </button>
      </div>
    </div>
  );
}
