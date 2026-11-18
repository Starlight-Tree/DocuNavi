import { useEffect, useId, useRef, useState } from 'react';
import type { KeyboardEvent, ReactNode } from 'react';
import { chooseTheme, clearTheme, themeChoice } from '../lib/theme';
import type { Theme } from '../lib/theme';

interface AccountMenuProps {
  /** ログイン中の利用者の表示名（メールアドレスなど）。ログインを使わない構成では渡さない（ボタンは「メニュー」になる）。 */
  label?: string | undefined;
  /** プロフィール画像の URL。https のときだけ表示し、読めなければ人型アイコンに戻す。 */
  image?: string | null | undefined;
  /** メニューの先頭に出すメールアドレス。渡さなければ `label` を出す。 */
  email?: string | null | undefined;
  /** 表示名を変更するダイアログを開く。渡さなければ項目を出さない。 */
  onChangeName?: (() => void) | undefined;
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

/** テーマの 3 択のアイコン（線画。`PersonIcon` と同じ線の太さ・端の丸め）。名前は、ボタンの `aria-label` と `title`。 */
function ThemeSvg({ children }: { children: ReactNode }) {
  return (
    <svg width="16" height="16" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {children}
    </svg>
  );
}

const THEME_OPTIONS: ReadonlyArray<{ value: Theme | 'system'; label: string; icon: ReactNode }> = [
  {
    value: 'light',
    label: 'ライト',
    icon: (
      <ThemeSvg>
        <circle cx="10" cy="10" r="3.2" />
        <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4" />
      </ThemeSvg>
    ),
  },
  {
    value: 'dark',
    label: 'ダーク',
    icon: (
      <ThemeSvg>
        <path d="M16.5 11.6A6.8 6.8 0 0 1 8.4 3.5a6.8 6.8 0 1 0 8.1 8.1z" />
      </ThemeSvg>
    ),
  },
  {
    value: 'system',
    label: 'システム',
    icon: (
      <ThemeSvg>
        <rect x="2.5" y="3.5" width="15" height="10" rx="1.5" />
        <path d="M7 17h6M10 13.5V17" />
      </ThemeSvg>
    ),
  },
];

/**
 * サイドバー下部のアカウント。表示名のボタンを押すと、メニュー（ナレッジ・テーマ・ログアウト）が上に開く。テーマ（ライト / ダーク / システム）は、ここで選ぶ（選ぶと localStorage に保存し、以後は OS より優先する。「システム」で OS に従う状態へ戻す）。
 * ログインを使わない構成でも、ナレッジへ行く入口として出す（表示名とログアウトは出さない）。
 * 開いたら先頭の項目にフォーカスを置く。↑↓ で移動、Esc で閉じてボタンへ戻る。メニューの外を押しても閉じる。
 */
export function AccountMenu({ label, image, email, onChangeName, onSignOut, onOpenKnowledge, knowledgeCurrent, compact }: AccountMenuProps) {
  const signedIn = onSignOut !== undefined && label !== undefined;
  // 読み込みに失敗した URL は覚えて、人型アイコンに戻す（URL が変わったら、また試す）
  const [failedImage, setFailedImage] = useState<string | null>(null);
  const avatarUrl = signedIn && image && image.startsWith('https://') && image !== failedImage ? image : null;
  const [open, setOpen] = useState(false);
  const [theme, setTheme] = useState<Theme | 'system'>(themeChoice);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[role^="menuitem"]')?.focus();
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
    const items = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role^="menuitem"]') ?? [])];
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
          <div role="group" aria-label="テーマ" className="ws-account-theme">
            <span className="ws-account-theme-label" aria-hidden="true">
              テーマ
            </span>
            <span className="ws-account-theme-options">
              {THEME_OPTIONS.map((option) => (
                <button
                  key={option.value}
                  type="button"
                  role="menuitemradio"
                  aria-checked={theme === option.value}
                  aria-label={option.label}
                  title={option.label}
                  className="ws-account-theme-option"
                  onClick={() => {
                    if (option.value === 'system') clearTheme();
                    else chooseTheme(option.value);
                    setTheme(option.value);
                  }}
                >
                  {option.icon}
                </button>
              ))}
            </span>
          </div>
          {signedIn && onChangeName && (
            <button
              type="button"
              role="menuitem"
              className="ws-account-item"
              onClick={() => {
                setOpen(false);
                onChangeName();
              }}
            >
              表示名を変更
            </button>
          )}
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
