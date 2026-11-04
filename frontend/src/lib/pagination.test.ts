import { describe, expect, it } from 'vitest';
import { paginate } from './pagination';

const items = Array.from({ length: 60 }, (_, i) => i + 1);

describe('paginate', () => {
  it('指定したページの行と表示範囲を返す', () => {
    const p = paginate(items, 2, 25);
    expect(p.rows[0]).toBe(26);
    expect(p.rows).toHaveLength(25);
    expect([p.page, p.pageCount, p.from, p.to, p.total]).toEqual([2, 3, 26, 50, 60]);
  });

  it('最後のページは端数になる', () => {
    const p = paginate(items, 3, 25);
    expect(p.rows).toHaveLength(10);
    expect([p.from, p.to]).toEqual([51, 60]);
  });

  it('範囲外のページは、最初・最後に丸める', () => {
    expect(paginate(items, 99, 25).page).toBe(3);
    expect(paginate(items, 0, 25).page).toBe(1);
    expect(paginate(items, Number.NaN, 25).page).toBe(1);
  });

  it('0 件は 1 ページ・範囲 0', () => {
    const p = paginate([], 1, 25);
    expect([p.rows.length, p.page, p.pageCount, p.from, p.to, p.total]).toEqual([0, 1, 1, 0, 0, 0]);
  });
});
