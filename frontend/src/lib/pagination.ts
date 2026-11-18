/** 一覧のページ分け。React にも通信にも依存しない。 */

/** 1 ページの件数。切り替えは持たない（表は中でスクロールし、80 件前後なら 25 件で足りる）。 */
export const PAGE_SIZE = 25;

export interface Page<T> {
  /** このページの行。 */
  rows: T[];
  /** 1 始まり。範囲外の指定は、最初・最後のページに丸める。 */
  page: number;
  pageCount: number;
  /** 表示範囲（1 始まり。0 件のときは両方 0）。 */
  from: number;
  to: number;
  total: number;
}

export function paginate<T>(items: readonly T[], page: number, size: number): Page<T> {
  const total = items.length;
  const pageCount = Math.max(1, Math.ceil(total / size));
  const current = Math.min(Math.max(1, Math.trunc(page) || 1), pageCount);
  const start = (current - 1) * size;
  const rows = items.slice(start, start + size);
  return { rows, page: current, pageCount, from: rows.length === 0 ? 0 : start + 1, to: start + rows.length, total };
}
