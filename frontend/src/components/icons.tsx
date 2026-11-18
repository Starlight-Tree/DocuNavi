/**
 * ボタンに使う線画のアイコン（18px。ThemeToggle と同じ線の太さ・端の丸め）。色は currentColor、読み上げは親のボタンの aria-label が担う。
 * 新しいアイコンの部品は入れず、ここに足す。
 */

const BASE = {
  width: 18,
  height: 18,
  viewBox: '0 0 20 20',
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.6,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
  'aria-hidden': true,
} as const;

/** ×（閉じる）。 */
export function CloseIcon() {
  return (
    <svg {...BASE}>
      <path d="M5 5l10 10M15 5L5 15" />
    </svg>
  );
}

/** 下向きの矢印と受け皿（ダウンロード）。 */
export function DownloadIcon() {
  return (
    <svg {...BASE}>
      <path d="M10 3.5v9M6.5 9.5L10 13l3.5-3.5M4 16.5h12" />
    </svg>
  );
}

/** 外向きの矢印（拡大）。 */
export function ExpandIcon() {
  return (
    <svg {...BASE}>
      <path d="M11.5 3.5h5v5M16.5 3.5l-5.5 5.5M8.5 16.5h-5v-5M3.5 16.5l5.5-5.5" />
    </svg>
  );
}

/** 内向きの矢印（縮小）。 */
export function CollapseIcon() {
  return (
    <svg {...BASE}>
      <path d="M16.5 8.5h-5v-5M11.5 8.5l5-5M3.5 11.5h5v5M8.5 11.5l-5 5" />
    </svg>
  );
}

/** ゴミ箱（削除）。 */
export function TrashIcon() {
  return (
    <svg {...BASE}>
      <path d="M3.5 5.5h13M8 5.5V3.8c0-.7.5-1.3 1.2-1.3h1.6c.7 0 1.2.6 1.2 1.3v1.7M5.2 5.5l.7 10.2c0 .6.5 1.1 1.1 1.1h6c.6 0 1.1-.5 1.1-1.1l.7-10.2M8.3 9v4.5M11.7 9v4.5" />
    </svg>
  );
}
