import { ChatApiError } from '../api/chat';
import { MSG_PDF_LOAD_FAILED } from '../api/documents';
import type { PdfDoc, PdfPage, PdfRender, RenderedPage } from '../api/pdfjs';

/**
 * `api/pdfjs.ts`（PDF.js との境界）の、テスト用の差し替え。`setup.ts` が、全テストでこちらに置き換える。
 * jsdom には、PDF.js が要る canvas・Worker・DOMMatrix・レイアウトが無い。実際の描画と日本語の表示は、実ブラウザーで確かめる（E2E ではない）。
 * 各テストは `fakePdf` の値を変えて挙動を決める（`setup.ts` が、テストごとに初期値へ戻す）。
 */

export interface FakePdfState {
  /** ページごとの text layer の項目（文字列）。 */
  pages: string[][];
  /** true の間、PDF を開くのが失敗する。 */
  failOpen: boolean;
  /** true の間、ページの描画が失敗する。 */
  failRender: boolean;
  /** 開いた URL（順）。 */
  opened: string[];
  /** 閉じた（destroy した）PDF の数。 */
  destroyed: number;
  /** 描いたページと倍率（順）。 */
  renders: Array<{ page: number; scale: number }>;
}

const DEFAULT_PAGES: string[][] = [
  ['あおば電機 AW-820 お手入れガイド', '本ガイドは、全自動洗濯乾燥機 AW-820 の清掃方法を解説したものです。'],
  ['AW-820 には、洗濯時に付着した糸くずやゴミを捕捉する', '「糸くずフィルター」と、乾燥時に発生する繊維くずを除去する', '「乾燥フィルター」の 2 種類のフィルターが設置されています。'],
  ['1. フィルターカバーの開閉レバーを押して、フィルターケースを開きます。'],
];

export const fakePdf: FakePdfState = { pages: DEFAULT_PAGES, failOpen: false, failRender: false, opened: [], destroyed: 0, renders: [] };

export function resetFakePdf(): void {
  fakePdf.pages = DEFAULT_PAGES;
  fakePdf.failOpen = false;
  fakePdf.failRender = false;
  fakePdf.opened = [];
  fakePdf.destroyed = 0;
  fakePdf.renders = [];
}

/** 倍率 1 のときのページの大きさ（A4 相当）と、表示の幅（`pdf-stage` の clientWidth）。 */
const PAGE_WIDTH = 612;
const PAGE_HEIGHT = 792;
export const STAGE_WIDTH = 800;

/** jsdom にはレイアウトが無く、clientWidth が常に 0。ビューアの表示領域（`.pdf-stage`）だけ、固定の幅を返す。 */
export function installStageWidth(): void {
  Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
    configurable: true,
    get(this: HTMLElement) {
      return this.classList.contains('pdf-stage') ? STAGE_WIDTH : 0;
    },
  });
}

function fakePage(pageNumber: number): PdfPage {
  const items = fakePdf.pages[pageNumber - 1] ?? [];
  return {
    width: PAGE_WIDTH,
    height: PAGE_HEIGHT,
    render(scale): PdfRender {
      fakePdf.renders.push({ page: pageNumber, scale });
      let cancelled = false;
      const done = new Promise<RenderedPage>((resolve, reject) => {
        queueMicrotask(() => {
          if (cancelled) return reject(new Error('cancelled'));
          if (fakePdf.failRender) return reject(new Error('render failed'));
          const textLayer = document.createElement('div');
          textLayer.className = 'pdf-text-layer';
          const divs = items.map((text) => {
            const span = document.createElement('span');
            span.textContent = text;
            textLayer.append(span);
            return span;
          });
          resolve({
            canvas: document.createElement('canvas'),
            textLayer,
            texts: [...items],
            divs,
            width: Math.floor(PAGE_WIDTH * scale),
            height: Math.floor(PAGE_HEIGHT * scale),
          });
        });
      });
      return { done, cancel: () => void (cancelled = true) };
    },
  };
}

export async function openPdf(url: string, signal?: AbortSignal): Promise<PdfDoc> {
  fakePdf.opened.push(url);
  await Promise.resolve();
  if (signal?.aborted) throw new DOMException('aborted', 'AbortError');
  if (fakePdf.failOpen) throw new ChatApiError(0, MSG_PDF_LOAD_FAILED); // 本物も、失敗は固定の文にして投げる
  return {
    numPages: fakePdf.pages.length,
    getPage: async (pageNumber) => fakePage(pageNumber),
    destroy: () => void (fakePdf.destroyed += 1),
  };
}
