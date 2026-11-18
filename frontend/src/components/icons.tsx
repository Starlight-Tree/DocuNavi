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

/** 折り目のある書類（PDF を開く）。 */
export function FileIcon() {
  return (
    <svg {...BASE}>
      <path d="M5.5 2.5h6l3.5 3.5v10.5c0 .6-.4 1-1 1H5.5c-.6 0-1-.4-1-1v-13c0-.6.4-1 1-1zM11.5 2.5V6H15M7.5 10h5M7.5 13h5" />
    </svg>
  );
}

/** 行の並ぶ文書（抽出テキストを見る）。 */
export function TextIcon() {
  return (
    <svg {...BASE}>
      <path d="M3.5 5h13M3.5 8.5h13M3.5 12h13M3.5 15.5h8" />
    </svg>
  );
}

/** 枝分かれ（関係図を見る）。 */
export function GraphIcon() {
  return (
    <svg {...BASE}>
      <circle cx="10" cy="4.5" r="1.8" />
      <circle cx="4.5" cy="15" r="1.8" />
      <circle cx="15.5" cy="15" r="1.8" />
      <path d="M9.2 6.2L5.3 13.3M10.8 6.2l3.9 7.1" />
    </svg>
  );
}
