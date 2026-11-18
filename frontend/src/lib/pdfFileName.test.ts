import { describe, expect, it } from 'vitest';
import { encodeRfc5987, pdfFileName } from './pdfFileName';

describe('pdfFileName', () => {
  it('日本語はそのまま。末尾に .pdf を付ける（既にあれば重ねない）', () => {
    expect(pdfFileName('あおば電機 AW-820 お手入れガイド（フィルターのお手入れ）')).toBe('あおば電機 AW-820 お手入れガイド（フィルターのお手入れ）.pdf');
    expect(pdfFileName('guide-01.PDF')).toBe('guide-01.pdf');
  });

  it('ファイル名に使えない文字と制御文字は「_」にする', () => {
    expect(pdfFileName('規程 第1/2版: 改定?')).toBe('規程 第1_2版_ 改定_.pdf');
    expect(pdfFileName('a"b<c>d|e\\f\r\ng')).toBe('a_b_c_d_e_f__g.pdf');
  });

  it('空・空白だけは document.pdf', () => {
    expect(pdfFileName('')).toBe('document.pdf');
    expect(pdfFileName('   ')).toBe('document.pdf');
    expect(pdfFileName('.pdf')).toBe('document.pdf');
  });

  it('100 文字までに切る（サロゲートペアを割らない）', () => {
    const name = pdfFileName('あ'.repeat(150));
    expect(name).toBe(`${'あ'.repeat(100)}.pdf`);
    expect(pdfFileName('😀'.repeat(120))).toBe(`${'😀'.repeat(100)}.pdf`);
  });
});

describe('encodeRfc5987', () => {
  it("encodeURIComponent が残す ' ( ) * も % にする", () => {
    expect(encodeRfc5987("a'b(c)d*e")).toBe('a%27b%28c%29d%2Ae');
    expect(encodeRfc5987('資料.pdf')).toBe('%E8%B3%87%E6%96%99.pdf');
  });
});
