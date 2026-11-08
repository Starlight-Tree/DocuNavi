import { describe, expect, it } from 'vitest';
import { hideOpenCitation, splitCitations, stripCitations } from './citations';
import { referenceTargets } from './sources';
import type { SourceDoc } from './sources';

const SOURCES: SourceDoc[] = [
  { name: 'a.pdf', text: '第5条 入寮の申込みは、2 月 28 日までに行う。', page: 1 },
  { name: 'b.pdf', text: '第6条 退寮の届出は、前月末までに行う。', page: 2 },
  { name: 'c.pdf', text: '弱い出典の本文です。', weak: true },
];
const TARGETS = referenceTargets(SOURCES);

describe('splitCitations', () => {
  it('文末の [n] を引用にする。番号は出典の n 番目', () => {
    expect(splitCitations('申込みは 2 月です[1]。', SOURCES, TARGETS)).toEqual([
      '申込みは 2 月です',
      { type: 'cite', refs: [{ number: 1, index: 0, span: null }] },
      '。',
    ]);
  });

  it('語句は、出典の本文にそのまま（空白を除いて）含まれるときだけ残す', () => {
    const [, cite] = splitCitations('x[1「2月28日まで」]', SOURCES, TARGETS);
    expect(cite).toEqual({ type: 'cite', refs: [{ number: 1, index: 0, span: '2月28日まで' }] });
  });

  it('原文に無い語句の引用は、番号ごと消す（本文にも出さない。チャンク単位の引用に戻さない）', () => {
    expect(splitCitations('x[1「二月末」]。', SOURCES, TARGETS)).toEqual(['x', '。']);
  });

  it('1 文字の語句は、どこにでも一致してしまうので、引用ごと消す', () => {
    expect(splitCitations('x[1「条」]', SOURCES, TARGETS)).toEqual(['x']);
  });

  it('並んだ引用のうち、原文に無い語句のものだけを消す', () => {
    const [, cite] = splitCitations('x[1「2月28日まで」][2「作り話の語句」]', SOURCES, TARGETS);
    expect(cite).toEqual({ type: 'cite', refs: [{ number: 1, index: 0, span: '2月28日まで' }] });
  });

  it('隣り合う引用は 1 つのまとまりにする。間に本文があれば別のまとまり', () => {
    expect(splitCitations('x[1][2]y[2]', SOURCES, TARGETS)).toEqual([
      'x',
      {
        type: 'cite',
        refs: [
          { number: 1, index: 0, span: null },
          { number: 2, index: 1, span: null },
        ],
      },
      'y',
      { type: 'cite', refs: [{ number: 2, index: 1, span: null }] },
    ]);
  });

  it('範囲外の番号・0・weak の出典の番号は消す（本文にも出さない）', () => {
    expect(splitCitations('a[9]b[0]c[3]d', SOURCES, TARGETS)).toEqual(['a', 'b', 'c', 'd']);
  });

  it('同じまとまりの重複（同じ番号・同じ語句）は 1 つにする', () => {
    const [, cite] = splitCitations('x[1][1]', SOURCES, TARGETS);
    expect(cite).toEqual({ type: 'cite', refs: [{ number: 1, index: 0, span: null }] });
  });

  it('同じ根拠でまとめた出典は、最初の 1 件の番号を使う', () => {
    const dup: SourceDoc[] = [
      { name: 'a.pdf', text: '同じ本文です。', page: 1 },
      { name: 'a.pdf', text: '同じ本文です。', page: 1 },
    ];
    const [, cite] = splitCitations('x[2]', dup, referenceTargets(dup));
    expect(cite).toEqual({ type: 'cite', refs: [{ number: 1, index: 0, span: null }] });
  });

  it('引用ではない括弧は、そのまま本文に残す', () => {
    expect(splitCitations('配列[a]と[1 2]と[「x」]', SOURCES, TARGETS)).toEqual(['配列[a]と[1 2]と[「x」]']);
  });
});

describe('hideOpenCitation と stripCitations', () => {
  it('生成の途中の、閉じていない引用だけを末尾から除く', () => {
    expect(hideOpenCitation('回答です[')).toBe('回答です');
    expect(hideOpenCitation('回答です[1')).toBe('回答です');
    expect(hideOpenCitation('回答です[1「根拠の')).toBe('回答です');
    expect(hideOpenCitation('回答です[1「根拠」')).toBe('回答です');
    expect(hideOpenCitation('回答です[1]')).toBe('回答です[1]');
    expect(hideOpenCitation('回答です[1]。続き')).toBe('回答です[1]。続き');
  });

  it('コピー用に、引用を消す', () => {
    expect(stripCitations('A[1「根拠」]。B[2][3]。')).toBe('A。B。');
  });
});
