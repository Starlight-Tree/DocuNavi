import { describe, expect, it } from 'vitest';
import { locatePdfSpan } from './pdfText';

describe('locatePdfSpan: PDF の文字の中から、根拠の語句の範囲を探す', () => {
  it('1 つの項目の中にあれば、その範囲を返す', () => {
    const texts = ['AW-820 には、洗濯時に付着した糸くずやゴミを捕捉する「糸くずフィルター」と、'];
    const parts = locatePdfSpan(texts, '「糸くずフィルター」');
    expect(parts).toEqual([{ item: 0, start: texts[0]!.indexOf('「糸くず'), end: texts[0]!.indexOf('」と') + 1 }]);
  });

  it('項目をまたぐ語句は、項目ごとの範囲に分けて返す（間の項目は全体を含む）', () => {
    const texts = ['手順 糸くずフィ', 'ルター', 'の取り出し方', '関係ない行'];
    expect(locatePdfSpan(texts, '糸くずフィルターの取り出し方')).toEqual([
      { item: 0, start: 3, end: 8 },
      { item: 1, start: 0, end: 3 },
      { item: 2, start: 0, end: 6 },
    ]);
  });

  it('改行・空白の違いは無視する（語句の側の空白も、PDF の側の空白も）', () => {
    const texts = ['第5条', ' 入寮の申込みは、', '2 月 28 日', 'までに行う。'];
    expect(locatePdfSpan(texts, '入寮の申込みは、2月28日までに行う。')).toEqual([
      { item: 1, start: 1, end: 9 },
      { item: 2, start: 0, end: 8 },
      { item: 3, start: 0, end: 6 },
    ]);
  });

  it('全角・半角と合字の違いは、NFKC でそろえて照合する。範囲は元の文字の位置', () => {
    const texts = ['型番ＡＷ－８２０の', 'ﬁlter（ﾌｨﾙﾀｰ）'];
    expect(locatePdfSpan(texts, 'AW-820')).toEqual([{ item: 0, start: 2, end: 8 }]);
    expect(locatePdfSpan(texts, 'filter')).toEqual([{ item: 1, start: 0, end: 5 }]);
    expect(locatePdfSpan(texts, 'フィルター')).toEqual([{ item: 1, start: 6, end: 11 }]);
  });

  it('1 文字が NFKC で複数の文字になるとき、その元の 1 文字全体を範囲にする', () => {
    expect(locatePdfSpan(['㈱あおば電機'], '(株)あおば')).toEqual([{ item: 0, start: 0, end: 4 }]);
  });

  it('見つからなければ null（何も強調しない）', () => {
    expect(locatePdfSpan(['乾燥フィルターは、乾燥時に発生する繊維くずを除去する。'], '糸くずフィルター')).toBeNull();
    expect(locatePdfSpan([], '糸くずフィルター')).toBeNull();
  });

  it('同じ語句がページに複数あるときは、どれか分からないので null', () => {
    expect(locatePdfSpan(['糸くずフィルター', 'と', '糸くずフィルター'], '糸くずフィルター')).toBeNull();
  });

  it('短すぎる語句（空白を除いて 2 文字未満）は、探さない', () => {
    expect(locatePdfSpan(['あ', 'いう'], 'あ')).toBeNull();
    expect(locatePdfSpan(['あ', 'いう'], ' \n ')).toBeNull();
    expect(locatePdfSpan(['あ', 'いう'], 'あい')).toEqual([
      { item: 0, start: 0, end: 1 },
      { item: 1, start: 0, end: 1 },
    ]);
  });

  it('長い本文（チャンク全体）も、項目をまたいで 1 か所に見つかれば、その範囲を返す', () => {
    const passage = '洗濯できる衣類の重さは、原則として 8kg までです。\nただし、毛布を洗うときは 2.0kg までとし、羽毛布団と電気毛布は洗えません。';
    const texts = ['安全上のご注意', '洗濯できる衣類の重さは、原則として', ' 8kg までです。', 'ただし、毛布を洗うときは 2.0kg', 'までとし、羽毛布団と電気毛布は洗えません。', '運転中は、原則としてふたを開けられません。'];
    const parts = locatePdfSpan(texts, passage);
    expect(parts?.map((p) => p.item)).toEqual([1, 2, 3, 4]);
    expect(parts?.[0]).toEqual({ item: 1, start: 0, end: texts[1]!.length });
    expect(parts?.[3]).toEqual({ item: 4, start: 0, end: texts[4]!.length });
  });

  it('一部だけが一致する（先頭だけ・途中が違う）ときは、推測せず null', () => {
    const texts = ['洗濯時に付着した糸くずやゴミを捕捉する', '（ここに別の文が入る）', '乾燥時に発生する繊維くずを除去する'];
    expect(locatePdfSpan(texts, '洗濯時に付着した糸くずやゴミを捕捉する乾燥時に発生する繊維くずを除去する')).toBeNull();
  });
});
