import { describe, expect, it } from 'vitest';
import { PDF_MARK_CLASS, applyPdfHighlight } from './pdfHighlight';

function layer(texts: string[]): HTMLElement[] {
  return texts.map((text) => {
    const span = document.createElement('span');
    span.textContent = text;
    return span;
  });
}

describe('applyPdfHighlight', () => {
  it('範囲の文字だけを <mark> で包み、前後の文字はそのまま残す', () => {
    const texts = ['AW-820 の「糸くずフィルター」と'];
    const divs = layer(texts);

    const { marks } = applyPdfHighlight(divs, texts, [{ item: 0, start: 8, end: 18 }]);

    expect(marks).toHaveLength(1);
    expect(marks[0]).toHaveClass(PDF_MARK_CLASS);
    expect(marks[0]).toHaveTextContent('「糸くずフィルター」');
    expect(divs[0]).toHaveTextContent('AW-820 の「糸くずフィルター」と'); // 全体の文字は変わらない
    expect(divs[0]!.childNodes).toHaveLength(3);
  });

  it('複数の項目にまたがる範囲は、項目ごとに包む', () => {
    const texts = ['手順 糸くずフィ', 'ルター', 'の取り出し方'];
    const divs = layer(texts);

    const { marks } = applyPdfHighlight(divs, texts, [
      { item: 0, start: 3, end: 8 },
      { item: 1, start: 0, end: 3 },
      { item: 2, start: 0, end: 1 },
    ]);

    expect(marks.map((m) => m.textContent)).toEqual(['糸くずフィ', 'ルター', 'の']);
  });

  it('clear で、強調を外して元の文字に戻す', () => {
    const texts = ['あいうえお'];
    const divs = layer(texts);
    const { clear } = applyPdfHighlight(divs, texts, [{ item: 0, start: 1, end: 3 }]);
    expect(divs[0]!.querySelector('mark')).not.toBeNull();

    clear();

    expect(divs[0]!.querySelector('mark')).toBeNull();
    expect(divs[0]!.textContent).toBe('あいうえお');
    expect(divs[0]!.childNodes).toHaveLength(1);
  });

  it('層の文字が項目の文字と違う・範囲が不正なものは付けない（位置がずれた強調を出さない）', () => {
    const texts = ['あいうえお', 'かきく'];
    const divs = layer(['あいうえお', '違う文字']);

    const { marks } = applyPdfHighlight(divs, texts, [
      { item: 1, start: 0, end: 2 }, // 文字が違う
      { item: 0, start: 4, end: 9 }, // はみ出す
      { item: 0, start: 2, end: 2 }, // 空
      { item: 5, start: 0, end: 1 }, // 項目が無い
    ]);

    expect(marks).toEqual([]);
    expect(divs[0]!.querySelector('mark')).toBeNull();
  });
});
