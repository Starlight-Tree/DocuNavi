import { describe, expect, it } from 'vitest';
import { PAGE_GUTTER, ZOOM_MAX, ZOOM_MIN, clampPage, currentPageOf, fitScale, pageAtLine, pixelRatioFor, renderWindow, stepZoom, visibleRange } from './pdfView';
import type { PageBox } from './pdfView';

describe('clampPage', () => {
  it('1〜総数に収める', () => {
    expect(clampPage(3, 5)).toBe(3);
    expect(clampPage(0, 5)).toBe(1);
    expect(clampPage(-2, 5)).toBe(1);
    expect(clampPage(9, 5)).toBe(5);
  });

  it('総数がまだ分からない・不正な値は 1', () => {
    expect(clampPage(4, 0)).toBe(1);
    expect(clampPage(Number.NaN, 5)).toBe(1);
    expect(clampPage(2.9, 5)).toBe(2);
  });
});

describe('stepZoom', () => {
  it('1 段階ずつ上げ下げし、範囲の外へは出ない', () => {
    expect(stepZoom(1, 1)).toBe(1.25);
    expect(stepZoom(1, -1)).toBe(0.8);
    let zoom = 1;
    for (let i = 0; i < 20; i += 1) zoom = stepZoom(zoom, 1);
    expect(zoom).toBe(ZOOM_MAX);
    for (let i = 0; i < 20; i += 1) zoom = stepZoom(zoom, -1);
    expect(zoom).toBe(ZOOM_MIN);
  });
});

describe('fitScale', () => {
  it('左右の余白を引いた幅に、ページの幅を合わせる', () => {
    expect(fitScale(600 + PAGE_GUTTER * 2, 600)).toBe(1);
    expect(fitScale(300 + PAGE_GUTTER * 2, 600)).toBe(0.5);
  });

  it('幅が分からない（非表示のとき）・余白より狭いときは null', () => {
    expect(fitScale(0, 600)).toBeNull();
    expect(fitScale(PAGE_GUTTER * 2, 600)).toBeNull();
    expect(fitScale(800, 0)).toBeNull();
  });
});

describe('pixelRatioFor', () => {
  it('通常は、画面の比率のまま。1 未満・不正な値は 1', () => {
    expect(pixelRatioFor(2, 600, 800)).toBe(2);
    expect(pixelRatioFor(0.5, 600, 800)).toBe(1);
    expect(pixelRatioFor(Number.NaN, 600, 800)).toBe(1);
  });

  it('画素数が上限を超えるときは、収まるまで下げる（1 未満にはしない）', () => {
    const ratio = pixelRatioFor(3, 3000, 4000);
    expect(ratio).toBeLessThan(3);
    expect(ratio).toBeGreaterThanOrEqual(1);
    expect(3000 * ratio * 4000 * ratio).toBeLessThanOrEqual(16_000_001);
    expect(pixelRatioFor(2, 20000, 20000)).toBe(1);
  });
});

/** 高さ 100 のページが 10 ずつ空いて 5 枚並ぶ（上の余白 10）。先頭の位置は 10・120・230・340・450。 */
const boxes: PageBox[] = Array.from({ length: 5 }, (_, i) => ({ top: 10 + i * 110, height: 100 }));

describe('pageAtLine', () => {
  it('その位置にあるページ。ページの間は手前のページ。範囲の外は端のページ', () => {
    expect(pageAtLine(boxes, 10)).toBe(1);
    expect(pageAtLine(boxes, 109)).toBe(1);
    expect(pageAtLine(boxes, 115)).toBe(1); // ページの間（110〜120）は手前
    expect(pageAtLine(boxes, 120)).toBe(2);
    expect(pageAtLine(boxes, 460)).toBe(5);
    expect(pageAtLine(boxes, 99999)).toBe(5);
    expect(pageAtLine(boxes, -50)).toBe(1);
  });

  it('ページが無ければ 1', () => {
    expect(pageAtLine([], 100)).toBe(1);
  });
});

describe('currentPageOf', () => {
  it('表示の上から 1/3 の線にあるページ', () => {
    // 表示の高さ 300 → 線は scrollTop + 100
    expect(currentPageOf(boxes, 0, 300, 560)).toBe(1);
    expect(currentPageOf(boxes, 100, 300, 560)).toBe(2); // 線 200 は 2 ページ目（120〜220）
    expect(currentPageOf(boxes, 130, 300, 560)).toBe(3); // 線 230 は 3 ページ目の先頭
  });

  it('いちばん下までスクロールしたら、最終ページ（最終ページが短くても番号が最後に届く）', () => {
    expect(currentPageOf(boxes, 260, 300, 560)).toBe(5);
    expect(currentPageOf(boxes, 259, 300, 560)).toBe(5); // 末尾まで 1px 以内
    expect(currentPageOf(boxes, 100, 300, 560)).toBe(2);
  });

  it('全体が表示より小さい（スクロールしない）ときは、下端の規則を使わない', () => {
    expect(currentPageOf(boxes, 0, 600, 560)).toBe(2); // 線は 200（2 ページ目）。下端の規則なら最終ページになってしまう
  });

  it('ページが無ければ 1', () => {
    expect(currentPageOf([], 0, 300, 300)).toBe(1);
  });
});

describe('visibleRange', () => {
  it('見えている範囲にかかる最初と最後のページ', () => {
    expect(visibleRange(boxes, 0, 100)).toEqual({ first: 1, last: 1 });
    expect(visibleRange(boxes, 100, 200)).toEqual({ first: 1, last: 3 }); // 100〜300 は、1 ページ目の終わりから 3 ページ目の始まり
    expect(visibleRange(boxes, 400, 300)).toEqual({ first: 4, last: 5 });
  });
});

describe('renderWindow', () => {
  it('見えている範囲の前後に、先読みを足し、1〜総数に収める', () => {
    expect(renderWindow({ first: 4, last: 5 }, 10)).toEqual({ from: 3, to: 6 });
    expect(renderWindow({ first: 1, last: 1 }, 10)).toEqual({ from: 1, to: 2 });
    expect(renderWindow({ first: 10, last: 10 }, 10)).toEqual({ from: 9, to: 10 });
    expect(renderWindow({ first: 3, last: 4 }, 10, 0)).toEqual({ from: 3, to: 4 });
  });

  it('総数が 1 ページでも、枠の外へは出ない', () => {
    expect(renderWindow({ first: 1, last: 1 }, 1)).toEqual({ from: 1, to: 1 });
  });
});
