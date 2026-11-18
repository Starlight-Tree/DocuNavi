import { useEffect, useRef, useState } from 'react';
import { MSG_PDF_RENDER_FAILED } from '../api/documents';
import type { PdfDoc, PdfRender, RenderedPage } from '../api/pdfjs';
import { usePdfDocument } from '../hooks/usePdfDocument';
import { applyPdfHighlight } from '../lib/pdfHighlight';
import { locatePdfSpan } from '../lib/pdfText';
import { ZOOM_MAX, ZOOM_MIN, clampPage, fitScale, stepZoom } from '../lib/pdfView';
import { Status } from './Status';

export const MSG_PDF_CITE_MISS = '根拠の箇所を PDF の文字から特定できなかったため、強調は付けていません。該当のページを開いています。';

interface PdfViewerProps {
  /** 資料の名前（読み上げ用）。 */
  title: string;
  /** PDF の URL。認証ありの構成は、同じオリジンの中継（ADR 0013）。 */
  url: string;
  /** 「保存」で付けるファイル名（同じオリジンのときだけ効く）。 */
  saveName: string;
  /** 最初に開くページ（出典が持つページ。1 始まり）。無ければ先頭。 */
  page: number | null;
  /** 出典を押すたびに増える合図。変わったら、出典のページへ戻し、根拠の語句へ移る。 */
  cite: number;
  /** 根拠の語句（出典の本文にそのまま含まれると確かめたもの）。あれば、先にこれを強調する。 */
  span: string | null;
  /** 根拠のチャンクの本文。語句が無い・見つからないとき、この本文の全体を強調する。どちらも無ければ、ページを開くだけ。 */
  passage: string | null;
}

/**
 * PDF の表示（PDF.js）。1 ページずつ、絵（canvas）と透明な文字（選択できる）を重ねて描く。上のツールバーは、ページ送り・拡大縮小・保存だけ。
 * 出典から開いたときは、その出典のページへ移り、根拠（語句、無ければチャンクの本文の全体）を黄色で強調する（ページの文字の中で 1 か所に決まったときだけ。
 * 決まらなければ、強調せず、その旨を示す）。
 * 内蔵のビューアと違って、検索・印刷は無い。保存は元の PDF をそのまま保存する。
 */
export function PdfViewer(props: PdfViewerProps) {
  const [state, reload] = usePdfDocument(props.url);
  if (state.status !== 'ready') return <Status state={state} onRetry={reload} />;
  return <PdfPages doc={state.data} {...props} />;
}

interface Shown {
  /** 描いているページ。 */
  page: number;
  content: RenderedPage;
}

function PdfPages({ doc, title, url, saveName, page, cite, span, passage }: PdfViewerProps & { doc: PdfDoc }) {
  const total = doc.numPages;
  const [requested, setRequested] = useState(page ?? 1);
  // 「幅に合わせる」を 1 とした倍率。パネルの幅が変わっても、1 なら幅に合わせ続ける
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(0);
  const [shown, setShown] = useState<Shown | null>(null);
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [miss, setMiss] = useState(false);
  const stageRef = useRef<HTMLDivElement>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const shownPage = useRef<number | null>(null);
  const movedForCite = useRef<number | null>(null);

  const current = clampPage(requested, total);

  // 出典を押したとき（ページ・合図が変わったとき）は、出典のページへ戻す。出典が無いページなら先頭
  useEffect(() => {
    setRequested(page ?? 1);
  }, [page, cite]);

  // 表示の幅。非表示の間（0）は、前の幅を保つ（表示に戻ったとき、倍率が変わらなければ描き直さない）
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const measure = () => {
      if (stage.clientWidth > 0) setWidth(stage.clientWidth);
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(stage);
    return () => observer.disconnect();
  }, []);

  // ページを描く。描き終えるまでは、前のページを見せたまま（ちらつかせない）。条件が変わったら、前の描画を中断する
  useEffect(() => {
    if (width <= 0) return;
    let stale = false;
    let render: PdfRender | null = null;
    setFailed(false);
    doc
      .getPage(current)
      .then((pdfPage) => {
        if (stale) return;
        const fit = fitScale(width, pdfPage.width);
        if (fit === null) return;
        render = pdfPage.render(fit * zoom);
        return render.done.then((content) => {
          const host = hostRef.current;
          const stage = stageRef.current;
          if (stale || !host) return;
          host.replaceChildren(content.canvas, content.textLayer);
          host.style.width = `${content.width}px`;
          host.style.height = `${content.height}px`;
          if (shownPage.current !== current && stage) stage.scrollTop = 0;
          shownPage.current = current;
          setShown({ page: current, content });
        });
      })
      .catch(() => {
        if (!stale) setFailed(true);
      });
    return () => {
      stale = true;
      render?.cancel();
    };
  }, [doc, current, width, zoom, attempt]);

  // 根拠の強調。出典のページ（`page`）を見ているときだけ（ページが無い出典は、ページを推測しないので強調もしない）。語句、無ければチャンクの本文の全体
  // 出典を押した直後は、強調の先頭が見える位置までスクロールする
  useEffect(() => {
    if (shown === null) return;
    if ((span === null && passage === null) || page === null || shown.page !== page) {
      setMiss(false);
      return;
    }
    const { texts, divs } = shown.content;
    const parts = (span === null ? null : locatePdfSpan(texts, span)) ?? (passage === null ? null : locatePdfSpan(texts, passage));
    const highlight = parts === null ? null : applyPdfHighlight(divs, texts, parts);
    const mark = highlight?.marks[0];
    setMiss(mark === undefined);
    const stage = stageRef.current;
    if (mark !== undefined && stage && movedForCite.current !== cite) {
      movedForCite.current = cite;
      const stageBox = stage.getBoundingClientRect();
      const markBox = mark.getBoundingClientRect();
      stage.scrollTop += markBox.top - stageBox.top - stage.clientHeight / 4;
    }
    return () => highlight?.clear();
  }, [shown, span, passage, page, cite]);

  const fit = zoom === 1;

  return (
    <div className="pdf-viewer" role="group" aria-label={`${title} の PDF`}>
      <div className="pdf-toolbar" role="toolbar" aria-label="PDF の操作">
        <button type="button" className="pdf-tool" aria-label="前のページ" disabled={current <= 1} onClick={() => setRequested(clampPage(current - 1, total))}>
          ‹
        </button>
        <span className="pdf-pageno" aria-live="polite">
          {current} / {total}
        </span>
        <button type="button" className="pdf-tool" aria-label="次のページ" disabled={current >= total} onClick={() => setRequested(clampPage(current + 1, total))}>
          ›
        </button>
        <span className="pdf-sep" aria-hidden="true" />
        <button type="button" className="pdf-tool" aria-label="PDF を縮小" disabled={zoom <= ZOOM_MIN} onClick={() => setZoom(stepZoom(zoom, -1))}>
          −
        </button>
        <button type="button" className="pdf-tool pdf-fit" aria-pressed={fit} onClick={() => setZoom(1)}>
          幅に合わせる
        </button>
        <button type="button" className="pdf-tool" aria-label="PDF を拡大" disabled={zoom >= ZOOM_MAX} onClick={() => setZoom(stepZoom(zoom, 1))}>
          ＋
        </button>
        <span className="pdf-spacer" />
        <a className="btn btn-secondary pdf-save" href={url} download={saveName} aria-label="PDF を保存">
          保存
        </a>
      </div>
      {miss && (
        <p className="chunk-miss" role="status">
          {MSG_PDF_CITE_MISS}
        </p>
      )}
      <div ref={stageRef} className="pdf-stage" tabIndex={0} role="region" aria-label="PDF のページ">
        {failed ? (
          <div className="documents-error" role="alert">
            <p>{MSG_PDF_RENDER_FAILED}</p>
            <button type="button" className="btn btn-secondary" onClick={() => setAttempt((n) => n + 1)}>
              再試行
            </button>
          </div>
        ) : (
          shown === null && <p role="status">読み込み中…</p>
        )}
        {/* ページの絵と文字は、描き終えたときに PDF.js の出力をそのまま入れる（React の子にしない） */}
        <div ref={hostRef} className="pdf-page" />
      </div>
    </div>
  );
}
