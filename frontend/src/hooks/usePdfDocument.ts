import { useCallback, useEffect } from 'react';
import { openPdf } from '../api/pdfjs';
import type { PdfDoc } from '../api/pdfjs';
import { useLoaded } from './useLoaded';
import type { Loaded } from './useLoaded';

/**
 * PDF（`url`）を開く。画面を離れる・取り直す・URL が変わるときは、前の読み込みを中断し、開いていた PDF を閉じる（PDF.js の worker の資源を解放する）。
 * 閉じるのは、PDF が開けた（ready になった）後の効果の後始末。開く前（loading）の効果ではないので、開発時の二重実行（StrictMode）で、使用中の PDF を閉じない。
 */
export function usePdfDocument(url: string): [Loaded<PdfDoc>, () => void] {
  const load = useCallback((signal: AbortSignal) => openPdf(url, signal), [url]);
  const [state, reload] = useLoaded(load);
  const doc = state.status === 'ready' ? state.data : null;
  useEffect(() => {
    if (doc === null) return;
    return () => doc.destroy();
  }, [doc]);
  return [state, reload];
}
