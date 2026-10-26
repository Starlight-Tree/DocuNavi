import { describe, expect, it } from 'vitest';
import type { CollectionInfo } from '../api/collections';
import { collectionLabel, collectionOptions, isPreparing } from './collections';

const catalog: CollectionInfo[] = [
  { name: 'zahard', label: '設定資料', description: '', sortOrder: 10, registered: true, documentCount: 3, readyCount: 3 },
  { name: 'draft', label: '準備中の資料', description: '', sortOrder: 40, registered: true, documentCount: 0, readyCount: 0 },
];

describe('collectionLabel', () => {
  it('グループの一覧にある名前は、その label を出す', () => {
    expect(collectionLabel('zahard', catalog)).toBe('設定資料');
  });

  it('一覧に無い名前と、一覧を読めなかったとき（catalog なし）は、名前をそのまま出す（固定の表示名は持たない）', () => {
    expect(collectionLabel('manuals', catalog)).toBe('manuals');
    expect(collectionLabel('zahard')).toBe('zahard');
    expect(collectionLabel('cafe', [])).toBe('cafe');
  });

  it('prototype のキー（toString など）を表示名として引かない', () => {
    expect(collectionLabel('toString', catalog)).toBe('toString');
    expect(collectionLabel('constructor')).toBe('constructor');
  });
});

describe('collectionOptions', () => {
  it('一覧にある資料群を、名前順で 1 つずつ返す（未設定は含めない）', () => {
    expect(collectionOptions(['guides', null, 'zahard', 'guides', 'cafe', 'zahard'])).toEqual(['cafe', 'guides', 'zahard']);
  });

  it('資料群が 1 つも無ければ空', () => {
    expect(collectionOptions([null, null])).toEqual([]);
    expect(collectionOptions([])).toEqual([]);
  });
});

describe('isPreparing', () => {
  it('検索できる資料が 0 件のグループは、準備中', () => {
    expect(isPreparing(catalog[0] as CollectionInfo)).toBe(false);
    expect(isPreparing(catalog[1] as CollectionInfo)).toBe(true);
  });
});
