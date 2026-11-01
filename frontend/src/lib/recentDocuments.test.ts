import { describe, expect, it } from 'vitest';
import type { DocumentSummary } from '../api/documents';
import { RECENT_LIMIT, recentDocuments, visibleRange } from './recentDocuments';

function doc(id: number, updatedAt: string | null): DocumentSummary {
  return {
    id,
    source: `doc${id}.pdf`,
    title: null,
    collection: 'guides',
    status: 'ready',
    failureReason: null,
    pages: 1,
    updatedAt,
    chunkCount: 1,
    embeddedCount: 1,
  };
}

describe('recentDocuments', () => {
  it('更新が新しい順に、最大 5 件を返す', () => {
    const docs = [
      doc(1, '2026-10-01T00:00:00Z'),
      doc(2, '2026-10-07T00:00:00Z'),
      doc(3, '2026-10-03T00:00:00Z'),
      doc(4, '2026-10-06T00:00:00Z'),
      doc(5, '2026-10-02T00:00:00Z'),
      doc(6, '2026-10-05T00:00:00Z'),
      doc(7, '2026-10-04T00:00:00Z'),
    ];
    expect(RECENT_LIMIT).toBe(5);
    expect(recentDocuments(docs).map((d) => d.id)).toEqual([2, 4, 6, 7, 3]);
  });

  it('更新日が無い・読めない資料は入れない', () => {
    const docs = [doc(1, null), doc(2, 'not a date'), doc(3, '2026-10-01T00:00:00Z')];
    expect(recentDocuments(docs).map((d) => d.id)).toEqual([3]);
  });

  it('同じ更新日は入力の順を保つ', () => {
    const docs = [doc(1, '2026-10-01T00:00:00Z'), doc(2, '2026-10-01T00:00:00Z'), doc(3, '2026-10-01T00:00:00Z')];
    expect(recentDocuments(docs).map((d) => d.id)).toEqual([1, 2, 3]);
  });

  it('資料が少なければあるだけ返し、空なら空を返す。入力は変えない', () => {
    const docs = [doc(1, '2026-10-01T00:00:00Z'), doc(2, '2026-10-02T00:00:00Z')];
    expect(recentDocuments(docs).map((d) => d.id)).toEqual([2, 1]);
    expect(docs.map((d) => d.id)).toEqual([1, 2]);
    expect(recentDocuments([])).toEqual([]);
  });

  it('上限は引数で変えられる', () => {
    const docs = [doc(1, '2026-10-01T00:00:00Z'), doc(2, '2026-10-02T00:00:00Z'), doc(3, '2026-10-03T00:00:00Z')];
    expect(recentDocuments(docs, 2).map((d) => d.id)).toEqual([3, 2]);
  });
});

describe('visibleRange', () => {
  // 幅 300 のカードが 12 の間隔で 5 枚。枠の幅は 3 枚ぶん（924）
  const cards = Array.from({ length: 5 }, (_, i) => ({ left: i * 312, width: 300 }));

  it('左端では先頭から 3 枚が見える', () => {
    expect(visibleRange(cards, 0, 924)).toEqual({ first: 0, last: 2 });
  });

  it('1 枚ぶん送ると 2〜4 枚目、右端では 3〜5 枚目', () => {
    expect(visibleRange(cards, 312, 924)).toEqual({ first: 1, last: 3 });
    expect(visibleRange(cards, 624, 924)).toEqual({ first: 2, last: 4 });
  });

  it('枠の幅が分からない・カードが無いときは null', () => {
    expect(visibleRange(cards, 0, 0)).toBeNull();
    expect(visibleRange([], 0, 924)).toBeNull();
  });

  it('1 枚も収まりきらない幅では、左端に近い 1 枚', () => {
    expect(visibleRange(cards, 300, 200)).toEqual({ first: 1, last: 1 });
  });
});
