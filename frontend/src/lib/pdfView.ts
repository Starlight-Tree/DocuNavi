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

/** ページの間隔と、先頭のページの上の余白（px）。`styles.css` の `.pdf-stage` の `gap` と `padding-top` と同じ値。 */
export const PAGE_GAP = 16;

/** 描くページは、見えている範囲の前後に、この枚数だけ先読みする。それより遠いページの絵は捨てる（全ページの canvas を持たない）。 */
export const RENDER_BUFFER = 1;

/** ページの枠（スクロールする領域の先頭からの位置と高さ。px）。 */
export interface PageBox {
  top: number;
  height: number;
}

/** 位置 `line`（スクロールする領域の先頭から。px）にあるページ（1 始まり）。ページの間は手前のページ。範囲の外は端のページ。ページが無ければ 1。`boxes` は上から順。 */
export function pageAtLine(boxes: readonly PageBox[], line: number): number {
  let low = 0;
  let high = boxes.length - 1;
  let found = 0;
  while (low <= high) {
    const mid = (low + high) >> 1;
    if (boxes[mid]!.top <= line) {
      found = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return found + 1;
}

/**
 * ツールバーに出す「いま見ているページ」。表示の上から 1/3 の線にあるページ。
 * ただし、いちばん下までスクロールしたときは最終ページ（短い最終ページでも、番号が最後に届く）。
 */
export function currentPageOf(boxes: readonly PageBox[], scrollTop: number, clientHeight: number, scrollHeight: number): number {
  if (boxes.length === 0) return 1;
  if (scrollHeight > clientHeight && scrollTop + clientHeight >= scrollHeight - 1) return boxes.length;
  return pageAtLine(boxes, scrollTop + clientHeight / 3);
}

/** 見えている範囲（`scrollTop` から `scrollTop + clientHeight`）にかかるページ（1 始まり。両端を含む）。 */
export function visibleRange(boxes: readonly PageBox[], scrollTop: number, clientHeight: number): { first: number; last: number } {
  return { first: pageAtLine(boxes, scrollTop), last: pageAtLine(boxes, scrollTop + clientHeight) };
}

/** 描くページの範囲。見えている範囲の前後に `buffer` 枚を足し、1〜総数に収める。 */
export function renderWindow(range: { first: number; last: number }, total: number, buffer: number = RENDER_BUFFER): { from: number; to: number } {
  return { from: Math.max(1, range.first - buffer), to: Math.min(Math.max(total, 1), range.last + buffer) };
}
