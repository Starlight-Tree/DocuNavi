import { describe, expect, it } from 'vitest';
import { parseSources } from './sources';

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

  it('.txt 以外や行頭でない角括弧は見出しとみなさない', () => {
    expect(parseSources('[メモ]\n本文')).toEqual([]);
    expect(parseSources('参照: [ザーハード1.txt] を見よ')).toEqual([]);
  });

  it('CRLF でも見出しを認識する', () => {
    expect(parseSources('[a.txt]\r\n本文\r\n')).toEqual([{ name: 'a.txt', text: '本文' }]);
  });
});
