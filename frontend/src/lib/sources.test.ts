import { describe, expect, it } from 'vitest';
import { formatPages, parseSourceRefs, parseSources, withPages } from './sources';

describe('parseSources', () => {
  it('見出しごとにファイル名と本文を取り出す', () => {
    const context = '[ザーハード1.txt]\n大気は薄い。\n気温は低い。\n\n[ザーハード2.txt]\n探査団は三名。';
    expect(parseSources(context)).toEqual([
      { name: 'ザーハード1.txt', text: '大気は薄い。\n気温は低い。' },
      { name: 'ザーハード2.txt', text: '探査団は三名。' },
    ]);
  });

  it('未検索（空文字）なら空', () => {
    expect(parseSources('')).toEqual([]);
  });

  it('該当なしの定型文は出典にしない', () => {
    const notFound =
      '該当する設定資料は見つかりませんでした。別のキーワードで再検索してください。';
    expect(parseSources(notFound)).toEqual([]);
  });

  it('ヒットの後ろに連なった「該当なし」の定型文は、出典の本文に混ぜない', () => {
    const notFound =
      '該当する設定資料は見つかりませんでした。別のキーワードで再検索してください。';
    const context = `[ザーハード1.txt]\n大気は薄い。\n\n${notFound}`;
    expect(parseSources(context)).toEqual([{ name: 'ザーハード1.txt', text: '大気は薄い。' }]);

    // 定型文が出典と出典の間にあっても、前の出典の本文にならない
    const between = `[ザーハード1.txt]\n大気は薄い。\n\n${notFound}\n\n[ザーハード2.txt]\n探査団は三名。`;
    expect(parseSources(between)).toEqual([
      { name: 'ザーハード1.txt', text: '大気は薄い。' },
      { name: 'ザーハード2.txt', text: '探査団は三名。' },
    ]);
  });

  it('PDF の資料も出典として認識する', () => {
    const context = '[ザーハード1.txt]\n大気は薄い。\n\n[cafe_store_guide.pdf]\n定休日は水曜日。';
    expect(parseSources(context)).toEqual([
      { name: 'ザーハード1.txt', text: '大気は薄い。' },
      { name: 'cafe_store_guide.pdf', text: '定休日は水曜日。' },
    ]);
  });

  it('.txt と .pdf 以外や行頭でない角括弧は見出しとみなさない', () => {
    expect(parseSources('[メモ]\n本文')).toEqual([]);
    expect(parseSources('[メモ.md]\n本文')).toEqual([]);
    expect(parseSources('参照: [ザーハード1.txt] を見よ')).toEqual([]);
  });

  it('CRLF でも見出しを認識する', () => {
    expect(parseSources('[a.txt]\r\n本文\r\n')).toEqual([{ name: 'a.txt', text: '本文' }]);
  });
});

describe('parseSourceRefs', () => {
  it('サーバーの sources を読む。ページが無い・不正なら null、page_end は page より大きいときだけ', () => {
    expect(
      parseSourceRefs([
        { name: 'a.pdf', page: 2, page_end: 3 },
        { name: 'b.pdf', page: 1, page_end: null },
        { name: 'c.pdf', page: 0, page_end: 5 },
        { name: 'd.pdf', page: '2', page_end: null },
        { name: 'e.pdf', page: 2, page_end: 2 },
        { name: 'f.pdf', page: 1.5, page_end: null },
      ]),
    ).toEqual([
      { name: 'a.pdf', page: 2, pageEnd: 3 },
      { name: 'b.pdf', page: 1, pageEnd: null },
      { name: 'c.pdf', page: null, pageEnd: null },
      { name: 'd.pdf', page: null, pageEnd: null },
      { name: 'e.pdf', page: 2, pageEnd: null },
      { name: 'f.pdf', page: null, pageEnd: null },
    ]);
  });

  it('配列でない・要素の形が違うときは undefined（古い応答として、画面が突き合わせにフォールバックする）', () => {
    expect(parseSourceRefs(undefined)).toBeUndefined();
    expect(parseSourceRefs('x')).toBeUndefined();
    expect(parseSourceRefs([null])).toBeUndefined();
    expect(parseSourceRefs([{ page: 1 }])).toBeUndefined();
    expect(parseSourceRefs([])).toEqual([]);
  });
});

describe('withPages', () => {
  const docs = [
    { name: 'a.pdf', text: 'A' },
    { name: 'b.pdf', text: 'B' },
  ];

  it('件数とファイル名が合えば、出典ごとにページを付ける', () => {
    expect(
      withPages(docs, [
        { name: 'a.pdf', page: 2, pageEnd: 3 },
        { name: 'b.pdf', page: null, pageEnd: null },
      ]),
    ).toEqual([
      { name: 'a.pdf', text: 'A', page: 2, pageEnd: 3 },
      { name: 'b.pdf', text: 'B' },
    ]);
  });

  it('件数かファイル名が合わなければ、ずれを避けて何も付けない', () => {
    expect(withPages(docs, [{ name: 'a.pdf', page: 2, pageEnd: null }])).toEqual(docs);
    expect(
      withPages(docs, [
        { name: 'a.pdf', page: 2, pageEnd: null },
        { name: 'x.pdf', page: 1, pageEnd: null },
      ]),
    ).toEqual(docs);
    expect(withPages(docs, undefined)).toEqual(docs);
  });
});

describe('formatPages', () => {
  it('「p.2」、ページをまたぐなら「p.2–3」、ページが無ければ null', () => {
    expect(formatPages(2, undefined)).toBe('p.2');
    expect(formatPages(2, 3)).toBe('p.2–3');
    expect(formatPages(undefined, undefined)).toBeNull();
  });
});
