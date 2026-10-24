import { describe, expect, it } from 'vitest';
import { collectionLabel, collectionOptions } from './collections';

describe('collectionLabel', () => {
  it('知っている資料群は日本語の表示名にし、知らない名前はそのまま出す', () => {
    expect(collectionLabel('zahard')).toBe('設定資料');
    expect(collectionLabel('cafe')).toBe('店舗案内');
    expect(collectionLabel('guides')).toBe('規程・取扱説明書');
    expect(collectionLabel('manuals')).toBe('manuals');
  });

  it('prototype のキー（toString など）を表示名として引かない', () => {
    expect(collectionLabel('toString')).toBe('toString');
    expect(collectionLabel('constructor')).toBe('constructor');
  });
});

describe('collectionOptions', () => {
  it('一覧にある資料群を、決めた並びで 1 つずつ返す（未設定は含めない）', () => {
    expect(collectionOptions(['guides', null, 'zahard', 'guides', 'cafe', 'zahard'])).toEqual(['zahard', 'cafe', 'guides']);
  });

  it('知らない名前は、知っている名前の後ろに、名前順で並べる', () => {
    expect(collectionOptions(['zeta', 'cafe', 'alpha'])).toEqual(['cafe', 'alpha', 'zeta']);
  });

  it('資料群が 1 つも無ければ空', () => {
    expect(collectionOptions([null, null])).toEqual([]);
    expect(collectionOptions([])).toEqual([]);
  });
});
