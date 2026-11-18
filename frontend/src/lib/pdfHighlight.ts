import type { PdfSpanPart } from './pdfText';

/** 根拠の語句の強調（<mark>）につける class。見た目は `shell.css`。 */
export const PDF_MARK_CLASS = 'pdf-span-mark';

export interface PdfHighlight {
  /** 付けた強調（上から順）。付けられなかったら空。 */
  marks: HTMLElement[];
  /** 付けた強調を外して、元の文字に戻す。 */
  clear: () => void;
}

/**
 * PDF の文字の層（text layer）の中で、根拠の語句の範囲だけを <mark> で包む。
 * `divs[i]` の文字が `texts[i]` と同じときだけ付ける（違うなら、位置がずれているので付けない）。
 */
export function applyPdfHighlight(divs: readonly HTMLElement[], texts: readonly string[], parts: readonly PdfSpanPart[]): PdfHighlight {
  const marks: HTMLElement[] = [];
  const touched: Array<{ div: HTMLElement; text: string }> = [];
  for (const part of parts) {
    const div = divs[part.item];
    const text = texts[part.item];
    if (div === undefined || text === undefined || div.textContent !== text) continue;
    if (part.start < 0 || part.end > text.length || part.start >= part.end) continue;
    const mark = div.ownerDocument.createElement('mark');
    mark.className = PDF_MARK_CLASS;
    mark.textContent = text.slice(part.start, part.end);
    div.replaceChildren(text.slice(0, part.start), mark, text.slice(part.end));
    marks.push(mark);
    touched.push({ div, text });
  }
  const clear = () => {
    for (const { div, text } of touched) div.textContent = text;
  };
  return { marks, clear };
}
