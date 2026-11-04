import { describe, expect, it } from 'vitest';
import type { DocumentSummary } from '../api/documents';
import { effectiveSelection, normalizeSelection, setShown, shownState, toggleOne } from './documentSelection';

function doc(id: number, target: string | null, status = 'ready', collection: string | null = null): DocumentSummary {
  return {
    id,
    source: `${id}.pdf`,
    title: null,
    collection,
    target,
    docType: null,
    status,
    failureReason: null,
    pages: 1,
    updatedAt: null,
    chunkCount: 1,
    embeddedCount: 1,
    heroImage: false,
  };
}

const DOCS = [doc(1, 'a'), doc(2, 'a'), doc(3, 'b'), doc(4, null, 'processing')];

describe('effectiveSelection', () => {
  it('選択が無いとき、範囲が決める。すべては、検索できる資料だけ', () => {
    expect([...effectiveSelection(DOCS, { kind: 'all' }, false, null)]).toEqual([1, 2, 3]);
    expect([...effectiveSelection(DOCS, { kind: 'target', name: 'a' }, false, null)]).toEqual([1, 2]);
    expect([...effectiveSelection([doc(5, null, 'ready', 'cafe'), doc(6, null)], { kind: 'collection', name: 'cafe' }, false, null)]).toEqual([5]);
  });

  it('範囲が分からない会話は「すべて」として扱う', () => {
    expect([...effectiveSelection(DOCS, { kind: 'target', name: 'a' }, true, null)]).toEqual([1, 2, 3]);
  });

  it('選択があるときは、選択が範囲に勝つ。検索できない資料は入らない', () => {
    expect([...effectiveSelection(DOCS, { kind: 'target', name: 'a' }, false, [3, 4])]).toEqual([3]);
    expect([...effectiveSelection(DOCS, { kind: 'all' }, false, [])]).toEqual([]);
  });
});

describe('normalizeSelection', () => {
  it('範囲が「すべて」で全部選ばれたら null（絞らない）に戻す', () => {
    expect(normalizeSelection(new Set([1, 2, 3]), DOCS, { kind: 'all' }, false)).toBeNull();
    expect(normalizeSelection(new Set([3, 1]), DOCS, { kind: 'all' }, false)).toEqual([1, 3]);
    expect(normalizeSelection(new Set(), DOCS, { kind: 'all' }, false)).toEqual([]);
  });

  it('対象の会話では、全部選ばれても集合のまま持つ（null にすると別の資料に変わる）', () => {
    expect(normalizeSelection(new Set([1, 2, 3]), DOCS, { kind: 'target', name: 'a' }, false)).toEqual([1, 2, 3]);
  });
});

describe('setShown / toggleOne / shownState', () => {
  it('表示中の資料だけを付け外しする。表示していない資料と、検索できない資料は変えない', () => {
    const current = new Set([1, 2, 3]);
    expect([...setShown(current, [DOCS[0]!, DOCS[3]!], false)]).toEqual([2, 3]);
    expect([...setShown(new Set([3]), [DOCS[0]!, DOCS[1]!, DOCS[3]!], true)].sort()).toEqual([1, 2, 3]);
  });

  it('1 件のチェックを反転する', () => {
    expect([...toggleOne(new Set([1]), 2)]).toEqual([1, 2]);
    expect([...toggleOne(new Set([1, 2]), 2)]).toEqual([1]);
  });

  it('見出し行の状態は、表示中の検索できる資料だけで数える', () => {
    expect(shownState(DOCS, new Set([1, 3, 4]))).toEqual({ selectable: 3, checked: 2 });
    expect(shownState([DOCS[3]!], new Set())).toEqual({ selectable: 0, checked: 0 });
  });
});
