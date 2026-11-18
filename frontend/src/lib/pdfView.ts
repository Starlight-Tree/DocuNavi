/** PDF ビューアの、ページ送りと倍率の計算（画面に依存しない）。 */

/** 「幅に合わせる」を 1 とした倍率の範囲と、1 回の拡大・縮小の刻み。 */
export const ZOOM_MIN = 0.5;
export const ZOOM_MAX = 3;
const ZOOM_STEP = 1.25;

/** ページの左右の余白（px）。表示の幅から、この 2 倍を引いた幅にページを合わせる。 */
export const PAGE_GUTTER = 16;

/** 1 枚の canvas の画素数の上限。ブラウザー（特に Safari）には上限があり、超えると何も描けない。 */
const MAX_CANVAS_PIXELS = 16_000_000;

/** ページ番号を 1〜総数に収める。総数が 0 以下（まだ分からない）ときは 1。 */
export function clampPage(page: number, total: number): number {
  if (!Number.isFinite(page) || total < 1) return 1;
  return Math.min(Math.max(Math.trunc(page), 1), Math.trunc(total));
}

/** 倍率を 1 段階、上げる（1）か下げる（-1）。範囲の外へは出ない。 */
export function stepZoom(zoom: number, direction: 1 | -1): number {
  const next = direction === 1 ? zoom * ZOOM_STEP : zoom / ZOOM_STEP;
  return Math.round(Math.min(Math.max(next, ZOOM_MIN), ZOOM_MAX) * 1000) / 1000;
}

/** 表示の幅（`containerWidth`）にページ（倍率 1 のときの幅 `pageWidth`）の幅を合わせる倍率。幅が分からない（0 以下）ときは null。 */
export function fitScale(containerWidth: number, pageWidth: number): number | null {
  const room = containerWidth - PAGE_GUTTER * 2;
  if (!(room > 0) || !(pageWidth > 0)) return null;
  return Math.round((room / pageWidth) * 1000) / 1000;
}

/**
 * 高精細な画面で canvas の解像度を上げる比率。ただし、画素数が上限を超えるなら、収まるまで下げる（1 未満にはしない）。
 */
export function pixelRatioFor(devicePixelRatio: number, cssWidth: number, cssHeight: number): number {
  const wanted = devicePixelRatio >= 1 ? devicePixelRatio : 1;
  const area = cssWidth * cssHeight;
  if (area <= 0) return 1;
  return Math.max(1, Math.min(wanted, Math.sqrt(MAX_CANVAS_PIXELS / area)));
}
