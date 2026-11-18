import { describe, expect, it } from 'vitest';
import { PAGE_GUTTER, ZOOM_MAX, ZOOM_MIN, clampPage, fitScale, pixelRatioFor, stepZoom } from './pdfView';

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
