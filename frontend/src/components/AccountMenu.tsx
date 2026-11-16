import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';

interface AccountMenuProps {
  /** ログイン中の利用者の表示名（メールアドレスなど）。ログインを使わない構成では渡さない（ボタンは「メニュー」になる）。 */
  label?: string | undefined;
  /** プロフィール画像の URL。https のときだけ表示し、読めなければ人型アイコンに戻す。 */
  image?: string | null | undefined;
  /** メニューの先頭に出すメールアドレス。渡さなければ `label` を出す。 */
  email?: string | null | undefined;
  /** ログアウト。ログインを使わない構成では渡さない（項目を出さない）。 */
  onSignOut?: (() => void) | undefined;
  /** 「ナレッジ」（資料一覧）を開く。 */
  onOpenKnowledge: () => void;
  /** いま資料一覧を開いているか。 */
  knowledgeCurrent: boolean;
  /** アイコンだけのレール用。名前と矢印は出さず、ツールチップ（`data-tip`）を付ける。 */
  compact?: boolean | undefined;
}

/** 線画の人型。`SidebarIcon` と同じく、アイコンのライブラリは使わず、線だけの SVG を直に置く。 */
function PersonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="10" cy="7" r="3.2" />
      <path d="M3.5 17c0-3.3 2.9-5.5 6.5-5.5s6.5 2.2 6.5 5.5" />
    </svg>
  );
}

/**
 * サイドバー下部のアカウント。表示名のボタンを押すと、メニュー（ナレッジ・ログアウト）が上に開く。
 * ログインを使わない構成でも、ナレッジへ行く入口として出す（表示名とログアウトは出さない）。
 * 開いたら先頭の項目にフォーカスを置く。↑↓ で移動、Esc で閉じてボタンへ戻る。メニューの外を押しても閉じる。
 */
export function AccountMenu({ label, image, email, onSignOut, onOpenKnowledge, knowledgeCurrent, compact }: AccountMenuProps) {
  const signedIn = onSignOut !== undefined && label !== undefined;
  // 読み込みに失敗した URL は覚えて、人型アイコンに戻す（URL が変わったら、また試す）
  const [failedImage, setFailedImage] = useState<string | null>(null);
  const avatarUrl = signedIn && image && image.startsWith('https://') && image !== failedImage ? image : null;
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
    <div className={compact ? 'ws-account ws-account-compact' : 'ws-account'}>
      {open && (
        <div ref={menuRef} id={menuId} role="menu" aria-label={signedIn ? 'アカウント' : 'メニュー'} className="ws-account-menu" onKeyDown={handleKeyDown}>
          {signedIn && <p className="ws-account-who">{email || label}</p>}
          <button
            type="button"
            role="menuitem"
            className="ws-account-item"
            aria-current={knowledgeCurrent ? 'page' : undefined}
            onClick={() => {
              setOpen(false);
              onOpenKnowledge();
            }}
          >
            ナレッジ
          </button>
          {signedIn && (
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
          )}
        </div>
      )}
      <button
        ref={buttonRef}
        type="button"
        className="ws-account-button"
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        aria-label={signedIn ? 'アカウントメニュー' : compact ? 'メニュー' : undefined}
        title={compact ? undefined : signedIn ? 'アカウントメニュー' : undefined}
        data-tip={compact ? (signedIn ? 'アカウントメニュー' : 'メニュー') : undefined}
        onClick={() => setOpen((v) => !v)}
      >
        <span className="ws-account-avatar" aria-hidden="true">
          {avatarUrl ? (
            <img src={avatarUrl} alt="" referrerPolicy="no-referrer" onError={() => setFailedImage(avatarUrl)} />
          ) : signedIn ? (
            <PersonIcon />
          ) : (
            '≡'
          )}
        </span>
        <span className="ws-account-name">{signedIn ? label : 'メニュー'}</span>
        <span aria-hidden="true">▴</span>
      </button>
    </div>
  );
}
