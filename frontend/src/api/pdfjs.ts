import type { PDFDocumentProxy, PDFPageProxy } from 'pdfjs-dist';
import { pixelRatioFor } from '../lib/pdfView';
import { ChatApiError } from './chat';
import { MSG_PDF_LOAD_FAILED } from './documents';

/**
 * PDF.js（`pdfjs-dist`）との境界。画面（コンポーネント・hooks）は、ここの型だけを使う。PDF.js を直接 import するのは、このファイルだけ。
 * PDF の取得は PDF.js の中の fetch（同じオリジンの中継なら Cookie が付く。ADR 0013）。テストは、このモジュールを差し替える（`src/test/fakePdf.ts`）。
 */

/**
 * 日本語の CMap・標準フォント・wasm・ICC は、`public/pdfjs/` に置いた PDF.js 同梱のコピーを、同じオリジンから読む（ADR 0014）。
 * 末尾の「/」が要る。`pdfjs-dist` の版を上げたら、コピーも同じ版にそろえる（`pdfjsAssets.test.ts` が確かめる）。
 */
const ASSET_BASE = '/pdfjs/';

/** 描き終えた 1 ページ。`canvas`（絵）と `textLayer`（選択・検索のための透明な文字）を、そのまま同じ位置に重ねて置く。 */
export interface RenderedPage {
  canvas: HTMLCanvasElement;
  textLayer: HTMLDivElement;
  /** text layer の項目の文字（`divs` と同じ順・同じ数）。 */
  texts: string[];
  /** text layer の項目の要素。 */
  divs: HTMLElement[];
  /** 画面での大きさ（CSS の px）。 */
  width: number;
  height: number;
}

export interface PdfRender {
  /** 描き終えたら、そのページ。中断（`cancel`）したときは reject する（呼び出し側が、中断したことを覚えて無視する）。 */
  done: Promise<RenderedPage>;
  cancel: () => void;
}

export interface PdfPage {
  /** 倍率 1 のときのページの大きさ（CSS の px）。 */
  width: number;
  height: number;
  /** 倍率 `scale` で、新しい canvas と text layer に描く（前の描画には触らない）。 */
  render: (scale: number) => PdfRender;
}

export interface PdfDoc {
  numPages: number;
  /** 1 始まりのページ番号。 */
  getPage: (pageNumber: number) => Promise<PdfPage>;
  destroy: () => void;
}

type PdfJs = typeof import('pdfjs-dist/legacy/build/pdf.mjs');

let engine: Promise<PdfJs> | null = null;

/**
 * PDF.js は大きい（約 0.5 MB）ので、PDF を初めて開くときに読み込む。互換のために `legacy` の版を使う（新しめの JS の機能を、ポリフィルで補う版）。
 * 処理は別スレッドの worker で行う。worker は、バンドラーが `pdfjs-dist` と同じ版のファイルを配信用に出力する。
 */
function loadEngine(): Promise<PdfJs> {
  engine ??= import('pdfjs-dist/legacy/build/pdf.mjs')
    .then((lib) => {
      lib.GlobalWorkerOptions.workerSrc = new URL('pdfjs-dist/legacy/build/pdf.worker.min.mjs', import.meta.url).toString();
      return lib;
    })
    .catch((error: unknown) => {
      engine = null; // 一時的な失敗なら、次の再試行でもう一度読み込む
      throw error;
    });
  return engine;
}

function abortError(): DOMException {
  return new DOMException('aborted', 'AbortError');
}

function wrapPage(lib: PdfJs, page: PDFPageProxy): PdfPage {
  const base = page.getViewport({ scale: 1 });
  return {
    width: base.width,
    height: base.height,
    render(scale) {
      const viewport = page.getViewport({ scale });
      const width = Math.floor(viewport.width);
      const height = Math.floor(viewport.height);
      const ratio = pixelRatioFor(window.devicePixelRatio, width, height);

      const canvas = document.createElement('canvas');
      canvas.width = Math.floor(width * ratio);
      canvas.height = Math.floor(height * ratio);
      canvas.style.width = `${width}px`;
      canvas.style.height = `${height}px`;

      // text layer の大きさ・文字の大きさは、この 3 つの変数から決まる（PDF.js の既定の CSS は、ページの枠でこれを定める）
      const textLayer = document.createElement('div');
      textLayer.className = 'pdf-text-layer';
      textLayer.style.setProperty('--total-scale-factor', String(scale));
      textLayer.style.setProperty('--scale-round-x', '1px');
      textLayer.style.setProperty('--scale-round-y', '1px');

      const task = page.render(
        ratio === 1 ? { canvas, viewport } : { canvas, viewport, transform: [ratio, 0, 0, ratio, 0, 0] },
      );
      const text = new lib.TextLayer({ textContentSource: page.streamTextContent(), container: textLayer, viewport });
      const done = Promise.all([task.promise, text.render()]).then(
        (): RenderedPage => ({ canvas, textLayer, texts: [...text.textContentItemsStr], divs: [...text.textDivs], width, height }),
      );
      return {
        done,
        cancel: () => {
          task.cancel();
          text.cancel();
        },
      };
    },
  };
}

/**
 * PDF を開く。失敗は固定の文（`MSG_PDF_LOAD_FAILED`）にする（サーバーや PDF.js の本文は画面に出さない）。
 * `signal` が中断されたら、読み込みを止めて AbortError で reject する。
 */
export async function openPdf(url: string, signal?: AbortSignal): Promise<PdfDoc> {
  try {
    const lib = await loadEngine();
    if (signal?.aborted) throw abortError();
    const task = lib.getDocument({
      url,
      cMapUrl: `${ASSET_BASE}cmaps/`,
      cMapPacked: true,
      standardFontDataUrl: `${ASSET_BASE}standard_fonts/`,
      wasmUrl: `${ASSET_BASE}wasm/`,
      iccUrl: `${ASSET_BASE}iccs/`,
      enableXfa: false,
    });
    const stop = () => void task.destroy();
    signal?.addEventListener('abort', stop, { once: true });
    let doc: PDFDocumentProxy;
    try {
      doc = await task.promise;
    } finally {
      signal?.removeEventListener('abort', stop);
    }
    if (signal?.aborted) {
      stop();
      throw abortError();
    }
    return {
      numPages: doc.numPages,
      getPage: async (pageNumber) => wrapPage(lib, await doc.getPage(pageNumber)),
      destroy: stop,
    };
  } catch (error) {
    if (signal?.aborted) throw abortError();
    throw new ChatApiError(0, MSG_PDF_LOAD_FAILED);
  }
}
