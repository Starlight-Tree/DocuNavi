import { useEffect, useState } from 'react';
import { chooseTheme, currentTheme, storedTheme } from '../lib/theme';
import type { Theme } from '../lib/theme';

/** 線画の太陽（ライトへ切り替える。いまダークのとき）。`SidebarIcon` と同じく、線だけの SVG を直に置く。 */
function SunIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <circle cx="10" cy="10" r="3.2" />
      <path d="M10 2.5v2M10 15.5v2M2.5 10h2M15.5 10h2M4.7 4.7l1.4 1.4M13.9 13.9l1.4 1.4M4.7 15.3l1.4-1.4M13.9 6.1l1.4-1.4" />
    </svg>
  );
}

/** 線画の月（ダークへ切り替える。いまライトのとき）。 */
function MoonIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M16.5 11.6A6.8 6.8 0 0 1 8.4 3.5a6.8 6.8 0 1 0 8.1 8.1z" />
    </svg>
  );
}

/**
 * 現在のモードを反転させるボタン。ラベルは「切り替えた先」を案内する。選んだら localStorage に保存し、以後は OS より優先する。
 * 選んでいない間は、OS の設定が変わったら表示（アイコンとラベル）も追従する。
 */
export function ThemeToggle() {
  const [theme, setTheme] = useState<Theme>(currentTheme);

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return;
    const query = window.matchMedia('(prefers-color-scheme: dark)');
    const onChange = () => {
      if (storedTheme() === null) setTheme(currentTheme());
    };
    query.addEventListener('change', onChange);
    return () => query.removeEventListener('change', onChange);
  }, []);

  const next: Theme = theme === 'dark' ? 'light' : 'dark';
  const label = next === 'dark' ? 'ダークモードに切り替え' : 'ライトモードに切り替え';
  return (
    <button
      type="button"
      className="ws-icon-btn ws-theme-toggle"
      aria-label={label}
      title={label}
      onClick={() => {
        chooseTheme(next);
        setTheme(next);
      }}
    >
      {theme === 'dark' ? <SunIcon /> : <MoonIcon />}
    </button>
  );
}
