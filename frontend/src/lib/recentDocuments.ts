/**
 * 資料一覧の「最近更新された資料」。React にも通信にも依存しない（`GET /documents` の結果から選ぶだけ）。
 *
 * - 更新日（updated_at）が新しい順に、最大 `RECENT_LIMIT` 件。更新日が無い・読めない資料は入れない。
 * - 種別（カード）や検索・絞り込みの条件は見ない。全資料から選ぶ。
 * - 同じ更新日は、入力の順（サーバーのファイル名順）を保つ。
 */
import type { DocumentSummary } from '../api/documents';

export const RECENT_LIMIT = 5;

function timeOf(value: string | null): number | null {
  if (value === null) return null;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? null : time;
}

export function recentDocuments(documents: readonly DocumentSummary[], limit: number = RECENT_LIMIT): DocumentSummary[] {
  return documents
    .map((document) => ({ document, time: timeOf(document.updatedAt) }))
    .filter((entry): entry is { document: DocumentSummary; time: number } => entry.time !== null)
    .sort((a, b) => b.time - a.time)
    .slice(0, limit)
    .map((entry) => entry.document);
}

/** カードの位置（左端と幅）と、横スクロールの枠から、いま見えているカードの範囲（0 始まり、両端を含む）を求める。 */
export function visibleRange(
  cards: ReadonlyArray<{ left: number; width: number }>,
  scrollLeft: number,
  viewWidth: number,
): { first: number; last: number } | null {
  if (cards.length === 0 || viewWidth <= 0) return null;
  // 端数の誤差を許す
  const tolerance = 2;
  let first = -1;
  let last = -1;
  cards.forEach((card, index) => {
    const fits = card.left >= scrollLeft - tolerance && card.left + card.width <= scrollLeft + viewWidth + tolerance;
    if (fits) {
      if (first === -1) first = index;
      last = index;
    }
  });
  if (first === -1) {
    // どのカードも収まりきらないとき（幅が狭い）は、左端に最も近いカード 1 枚
    let nearest = 0;
    cards.forEach((card, index) => {
      if (Math.abs(card.left - scrollLeft) < Math.abs((cards[nearest]?.left ?? 0) - scrollLeft)) nearest = index;
    });
    return { first: nearest, last: nearest };
  }
  return { first, last };
}
