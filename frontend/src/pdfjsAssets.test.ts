import { readFileSync, readdirSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';

// `public/pdfjs/` は、`pdfjs-dist` が同梱する CMap・標準フォント・wasm・ICC のコピー（api/pdfjs.ts が同じオリジンから読む。ADR 0014）。
// worker・API とは別に、版の食い違いを PDF.js 自身は検出しない。`pdfjs-dist` の版を上げたときに、コピーの更新を忘れないための検査。
const PUBLIC = `${resolve(import.meta.dirname, '../public/pdfjs')}/`;
const PACKAGE = `${resolve(import.meta.dirname, '../node_modules/pdfjs-dist')}/`;
const HOW_TO_FIX = '（直し方: ADR 0014 の「更新の手順」で public/pdfjs を作り直す）';

const read = (path: string) => readFileSync(path);

describe('public/pdfjs は、インストール済みの pdfjs-dist と同じ版・同じ内容', () => {
  it(`版の印（VERSION）が、pdfjs-dist の版と同じ ${HOW_TO_FIX}`, () => {
    const installed = (JSON.parse(read(`${PACKAGE}package.json`).toString()) as { version: string }).version;
    expect(read(`${PUBLIC}VERSION`).toString().trim()).toBe(installed);
  });

  it.each(['cmaps', 'standard_fonts', 'iccs'])('%s は、同梱のファイルを全部、同じ内容で持つ', (dir) => {
    const names = readdirSync(`${PACKAGE}${dir}`);
    expect(names.length).toBeGreaterThan(0);
    for (const name of names) {
      expect(read(`${PUBLIC}${dir}/${name}`).equals(read(`${PACKAGE}${dir}/${name}`)), `${dir}/${name} ${HOW_TO_FIX}`).toBe(true);
    }
  });

  it.each(['jbig2.wasm', 'openjpeg.wasm', 'qcms_bg.wasm'])('wasm/%s は、同梱のファイルと同じ内容', (name) => {
    expect(read(`${PUBLIC}wasm/${name}`).equals(read(`${PACKAGE}wasm/${name}`)), `wasm/${name} ${HOW_TO_FIX}`).toBe(true);
  });

  it('日本語の PDF が読む CMap（Adobe-Japan1 の UCS2・UniJIS・90ms-RKSJ）がある', () => {
    const names = new Set(readdirSync(`${PUBLIC}cmaps`));
    for (const need of ['Adobe-Japan1-UCS2.bcmap', 'UniJIS-UCS2-H.bcmap', 'UniJIS-UTF16-H.bcmap', '90ms-RKSJ-H.bcmap', 'H.bcmap', 'V.bcmap']) {
      expect(names.has(need), need).toBe(true);
    }
  });
});
