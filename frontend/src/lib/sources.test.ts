import { describe, expect, it } from 'vitest';
import { formatPages, isVerifiedSpan, locateSpan, parseSourceRefs, parseSources, referenceCards, referenceItems, withPages } from './sources';
import type { SourceDoc } from './sources';

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
      { name: 'a.pdf', page: 2, pageEnd: 3, weak: false, collection: null, documentId: null },
      { name: 'b.pdf', page: 1, pageEnd: null, weak: false, collection: null, documentId: null },
      { name: 'c.pdf', page: null, pageEnd: null, weak: false, collection: null, documentId: null },
      { name: 'd.pdf', page: null, pageEnd: null, weak: false, collection: null, documentId: null },
      { name: 'e.pdf', page: 2, pageEnd: null, weak: false, collection: null, documentId: null },
      { name: 'f.pdf', page: null, pageEnd: null, weak: false, collection: null, documentId: null },
    ]);
  });

  it('collection は、資料グループの名前の形のときだけ読む。それ以外・無いときは null', () => {
    expect(
      parseSourceRefs([
        { name: 'a.pdf', page: 1, page_end: null, collection: 'zahard' },
        { name: 'b.pdf', page: 1, page_end: null, collection: null },
        { name: 'c.pdf', page: 1, page_end: null },
        { name: 'd.pdf', page: 1, page_end: null, collection: 'a b' },
        { name: 'e.pdf', page: 1, page_end: null, collection: 3 },
      ])?.map((ref) => ref.collection),
    ).toEqual(['zahard', null, null, null, null]);
  });

  it('weak は true のときだけ弱い（文字列の "true" などは弱くしない）', () => {
    expect(
      parseSourceRefs([
        { name: 'a.pdf', page: 1, page_end: null, weak: true },
        { name: 'b.pdf', page: 1, page_end: null, weak: 'true' },
        { name: 'c.pdf', page: 1, page_end: null },
      ])?.map((ref) => ref.weak),
    ).toEqual([true, false, false]);
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

  it('件数とファイル名が合えば、weak も付ける。合わなければ付けない', () => {
    const weakRefs = [
      { name: 'a.pdf', page: null, pageEnd: null, weak: false, collection: null },
      { name: 'b.pdf', page: null, pageEnd: null, weak: true, collection: null },
    ];
    expect(withPages(docs, weakRefs)).toEqual([docs[0], { ...docs[1], weak: true }]);
    expect(withPages(docs, weakRefs.slice(1))).toEqual(docs);
  });

  it('出典に資料グループがあれば、その名前も付ける（無ければ付けない）', () => {
    expect(
      withPages(docs, [
        { name: 'a.pdf', page: null, pageEnd: null, weak: false, collection: 'zahard' },
        { name: 'b.pdf', page: null, pageEnd: null, weak: false, collection: null },
      ]),
    ).toEqual([{ ...docs[0], collection: 'zahard' }, docs[1]]);
  });

  it('件数とファイル名が合えば、出典ごとにページを付ける', () => {
    expect(
      withPages(docs, [
        { name: 'a.pdf', page: 2, pageEnd: 3, weak: false, collection: null },
        { name: 'b.pdf', page: null, pageEnd: null, weak: false, collection: null },
      ]),
    ).toEqual([
      { name: 'a.pdf', text: 'A', page: 2, pageEnd: 3 },
      { name: 'b.pdf', text: 'B' },
    ]);
  });

  it('件数かファイル名が合わなければ、ずれを避けて何も付けない', () => {
    expect(withPages(docs, [{ name: 'a.pdf', page: 2, pageEnd: null, weak: false, collection: null }])).toEqual(docs);
    expect(
      withPages(docs, [
        { name: 'a.pdf', page: 2, pageEnd: null, weak: false, collection: null },
        { name: 'x.pdf', page: 1, pageEnd: null, weak: false, collection: null },
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

describe('referenceCards', () => {
  const docs: SourceDoc[] = [
    { name: 'a.pdf', text: 'a' },
    { name: 'b.pdf', text: 'b', weak: true },
    { name: 'c.pdf', text: 'c', weak: false },
  ];

  it('weak を除き、元の並びでの添字を残す（番号が元の出典とずれない）', () => {
    expect(referenceCards(docs).map(({ source, index }) => [source.name, index])).toEqual([
      ['a.pdf', 0],
      ['c.pdf', 2],
    ]);
  });

  it('出典が無ければ空', () => {
    expect(referenceCards([])).toEqual([]);
  });
});

describe('referenceItems', () => {
  it('weak を除く。資料名・ページ・抜粋がすべて同じものだけを 1 つにまとめる（空白の違いは同じ抜粋）', () => {
    const docs: SourceDoc[] = [
      { name: 'a.pdf', text: '第5条 申込みは 2 月末まで。', page: 1 },
      { name: 'a.pdf', text: '第5条  申込みは\n2 月末まで。', page: 1 },
      { name: 'b.pdf', text: 'b', page: 1, weak: true },
      { name: 'a.pdf', text: '第5条 申込みは 2 月末まで。', page: 3 },
    ];
    expect(referenceItems(docs).map(({ source, index }) => [source.name, source.page, index])).toEqual([
      ['a.pdf', 1, 0],
      ['a.pdf', 3, 3],
    ]);
  });

  it('同じ資料・同じページでも、抜粋が違えば別の根拠として残す。ページが無い出典も、抜粋が違えば残す', () => {
    const docs: SourceDoc[] = [
      { name: 'a.pdf', text: '第5条 申込みは 2 月末まで。', page: 1 },
      { name: 'a.pdf', text: '第6条 退寮は前月末まで。', page: 1 },
      { name: 'a.pdf', text: '第7条 寮費は月額。' },
      { name: 'a.pdf', text: '第8条 門限は 22 時。' },
    ];
    expect(referenceItems(docs).map(({ index }) => index)).toEqual([0, 1, 2, 3]);
  });

  it('出典が無ければ空', () => {
    expect(referenceItems([])).toEqual([]);
  });
});

describe('引用のための出典（資料の ID・番号つきの見出し・語句の位置）', () => {
  it('番号つきの見出しも、旧形式の見出しも読む', () => {
    expect(parseSources('[1] a.pdf\n本文A\n\n[2] 資料 b.txt\n本文B\n\n[c.pdf]\n本文C')).toEqual([
      { name: 'a.pdf', text: '本文A' },
      { name: '資料 b.txt', text: '本文B' },
      { name: 'c.pdf', text: '本文C' },
    ]);
  });

  it('サーバーの document_id は、正の整数だけ読む。出典に引き継ぐ', () => {
    const refs = parseSourceRefs([
      { name: 'a.pdf', document_id: 7 },
      { name: 'b.pdf', document_id: 0 },
      { name: 'c.pdf', document_id: '7' },
      { name: 'd.pdf' },
    ]);
    expect(refs?.map((ref) => ref.documentId)).toEqual([7, null, null, null]);
    const docs = withPages(
      [
        { name: 'a.pdf', text: 'x' },
        { name: 'b.pdf', text: 'y' },
      ],
      [
        { name: 'a.pdf', page: null, pageEnd: null, weak: false, collection: null, documentId: 7 },
        { name: 'b.pdf', page: null, pageEnd: null, weak: false, collection: null, documentId: null },
      ],
    );
    expect(docs.map((doc) => doc.documentId)).toEqual([7, undefined]);
  });

  it('locateSpan は、改行・空白が違っても、語句がある範囲を本文の位置で返す。無ければ null', () => {
    const content = '第2条 寮は 3 棟で\n構成する。';
    const range = locateSpan(content, '3棟で構成');
    expect(range && content.slice(range[0], range[1])).toBe('3 棟で\n構成');
    expect(locateSpan(content, '4棟')).toBeNull();
    expect(locateSpan(content, '条')).toBeNull(); // 短すぎる
  });

  it('isVerifiedSpan は、空白を除いて出典の本文に含まれるかを見る', () => {
    expect(isVerifiedSpan('2月28日', '申込みは 2 月 28 日までに行う。')).toBe(true);
    expect(isVerifiedSpan('三月', '申込みは 2 月 28 日までに行う。')).toBe(false);
  });
});
