import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import { MSG_PDF_RENDER_FAILED } from '../api/documents';
import type { PdfDoc, PdfRender, RenderedPage } from '../api/pdfjs';
import { usePdfDocument } from '../hooks/usePdfDocument';
import { applyPdfHighlight } from '../lib/pdfHighlight';
import { locatePdfSpan } from '../lib/pdfText';
import { PAGE_GAP, ZOOM_MAX, ZOOM_MIN, clampPage, currentPageOf, fitScale, renderWindow, stepZoom, visibleRange } from '../lib/pdfView';
import type { PageBox } from '../lib/pdfView';
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
  /** 出典を押すたびに増える合図。変わったら、出典のページへ戻り、根拠の語句へ移る。 */
  cite: number;
  /** 根拠の語句（出典の本文にそのまま含まれると確かめたもの）。あれば、先にこれを強調する。 */
  span: string | null;
  /** 根拠のチャンクの本文。語句が無い・見つからないとき、この本文の全体を強調する。どちらも無ければ、ページを開くだけ。 */
  passage: string | null;
}

/**
 * PDF の表示（PDF.js）。全ページを縦に並べて連続でスクロールする。各ページは、絵（canvas）と透明な文字（選択できる）を重ねて描く。
 * 描くのは、見えているページとその前後だけ（それより遠いページは、枠だけ残して絵を捨てる）。操作は、下に浮かぶ小さなバー（ページ送り・ページ番号・拡大縮小・保存）。
 * 出典から開いたときは、その出典のページへ移り、根拠（語句、無ければチャンクの本文の全体）を黄色で強調する（ページの文字の中で 1 か所に決まったときだけ。
 * 決まらなければ、強調せず、その旨を示す）。
 * 内蔵のビューアと違って、検索・印刷は無い。保存は元の PDF をそのまま保存する。
 */
export function PdfViewer(props: PdfViewerProps) {
  const [state, reload] = usePdfDocument(props.url);
  if (state.status !== 'ready') return <Status state={state} onRetry={reload} />;
  return <PdfPages doc={state.data} {...props} />;
}

/** 描き終えたページの大きさ。倍率が変わったら古いので使わない。 */
interface Size {
  scale: number;
  width: number;
  height: number;
}

const boxOf = (el: HTMLDivElement | null): PageBox => ({ top: el?.offsetTop ?? 0, height: el?.offsetHeight ?? 0 });

function PdfPages({ doc, title, url, saveName, page, cite, span, passage }: PdfViewerProps & { doc: PdfDoc }) {
  const total = doc.numPages;
  // 「幅に合わせる」を 1 とした倍率。パネルの幅が変わっても、1 なら幅に合わせ続ける
  const [zoom, setZoom] = useState(1);
  const [width, setWidth] = useState(0);
  // 1 ページ目の大きさ（倍率 1）。まだ描いていないページの枠の大きさに使う
  const [base, setBase] = useState<{ width: number; height: number } | null>(null);
  // ツールバーに出す、いま見ているページと、見えているページの範囲（スクロールで変わる）
  const [current, setCurrent] = useState(() => clampPage(page ?? 1, total));
  const [range, setRange] = useState(() => {
    const first = clampPage(page ?? 1, total);
    return { first, last: first };
  });
  const [sizes, setSizes] = useState<ReadonlyMap<number, Size>>(() => new Map());
  const [failed, setFailed] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const [miss, setMiss] = useState(false);
  // 出典のページを描き終えた内容（強調に使う）。そのページが範囲の外で捨てられたら null
  const [cited, setCited] = useState<RenderedPage | null>(null);
  // ページ番号の入力の途中の文字（入力していないときは null で、見ているページを出す）
  const [draft, setDraft] = useState<string | null>(null);

  const stageRef = useRef<HTMLDivElement>(null);
  const slotEls = useRef<Array<HTMLDivElement | null>>([]);
  const rendered = useRef(new Map<number, RenderedPage>());
  const lastScroll = useRef(0);
  const hiddenNow = useRef(false);
  // 出典・ページ番号・ボタンで飛んだページ。そのページを描き終えたとき、位置を 1 回だけ合わせ直す（手動のスクロールを始めたら取り消す）
  const pendingJump = useRef<number | null>(null);
  const movedForCite = useRef<number | null>(null);
  const citedPageRef = useRef<number | null>(null);
  const prevScale = useRef<number | null>(null);

  const citedPage = page !== null && page >= 1 && page <= total ? page : null;
  const fitted = base === null ? null : fitScale(width, base.width);
  const scale = fitted === null ? null : fitted * zoom;
  const placeholder = base !== null && scale !== null ? { width: Math.floor(base.width * scale), height: Math.floor(base.height * scale) } : null;
  const win = renderWindow(range, total);

  // 表示の幅。非表示の間（0）は、前の幅を保つ。表示に戻ったら、隠す前のスクロール位置へ戻す（display: none でスクロールは 0 に戻る）
  useEffect(() => {
    const stage = stageRef.current;
    if (!stage) return;
    const measure = () => {
      if (stage.clientWidth > 0) {
        setWidth(stage.clientWidth);
        if (hiddenNow.current) {
          hiddenNow.current = false;
          stage.scrollTop = lastScroll.current;
        }
      } else {
        hiddenNow.current = true;
      }
    };
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(stage);
    return () => observer.disconnect();
  }, []);

  // 1 ページ目の大きさ。ほかのページの枠にも、描き終えるまでこの大きさを使う
  useEffect(() => {
    let stale = false;
    doc
      .getPage(1)
      .then((first) => {
        if (!stale) setBase({ width: first.width, height: first.height });
      })
      .catch(() => {
        if (!stale) setFailed(true);
      });
    return () => {
      stale = true;
    };
  }, [doc, attempt]);

  // 倍率（拡大縮小・パネルの幅の変化）が変わったら、見ている位置を、倍率に比例して保つ
  useLayoutEffect(() => {
    if (scale === null) return;
    const stage = stageRef.current;
    if (stage && prevScale.current !== null && prevScale.current !== scale) stage.scrollTop = (stage.scrollTop * scale) / prevScale.current;
    prevScale.current = scale;
  }, [scale]);

  const setSlot = useCallback((n: number, el: HTMLDivElement | null) => {
    slotEls.current[n - 1] = el;
  }, []);

  const alignTo = useCallback((n: number) => {
    const stage = stageRef.current;
    const slot = slotEls.current[n - 1];
    if (stage && slot) stage.scrollTop = Math.max(0, slot.offsetTop - PAGE_GAP);
  }, []);

  // そのページの先頭へ飛ぶ。飛んだ先の前後を、すぐ描く範囲に入れる
  const jumpTo = useCallback(
    (n: number) => {
      pendingJump.current = n;
      alignTo(n);
      setCurrent(n);
      setRange({ first: n, last: n });
    },
    [alignTo],
  );

  // 開いたとき、出典を押し直したとき（ページ・合図が変わったとき）は、出典のページへ飛ぶ。出典が無いページなら先頭。見ているページを動かしても、これだけが飛ぶ
  const ready = scale !== null;
  useEffect(() => {
    if (ready) jumpTo(clampPage(page ?? 1, total));
  }, [ready, page, cite, total, jumpTo]);

  // 出典のページの内容を、強調に使う。範囲の中で描き済みなら、すぐ拾う
  useEffect(() => {
    citedPageRef.current = citedPage;
    setCited(citedPage === null ? null : (rendered.current.get(citedPage) ?? null));
  }, [citedPage, cite]);

  const onRendered = useCallback(
    (n: number, renderedScale: number, content: RenderedPage | null) => {
      if (content === null) {
        rendered.current.delete(n);
      } else {
        rendered.current.set(n, content);
        setSizes((prev) => {
          const old = prev.get(n);
          if (old && old.scale === renderedScale && old.width === content.width && old.height === content.height) return prev;
          const next = new Map(prev);
          next.set(n, { scale: renderedScale, width: content.width, height: content.height });
          return next;
        });
        if (pendingJump.current === n) {
          pendingJump.current = null;
          alignTo(n);
        }
      }
      if (n === citedPageRef.current) setCited(content);
    },
    [alignTo],
  );

  const onFailed = useCallback(() => setFailed(true), []);

  // 根拠の強調。出典のページ（`page`）の絵が描かれているときだけ（ページが無い出典は、ページを推測しないので強調もしない）。語句、無ければチャンクの本文の全体
  // 出典を押した直後は、強調の先頭が見える位置までスクロールする
  useEffect(() => {
    if (cited === null || citedPage === null || (span === null && passage === null)) {
      setMiss(false);
      return;
    }
    const { texts, divs } = cited;
    const parts = (span === null ? null : locatePdfSpan(texts, span)) ?? (passage === null ? null : locatePdfSpan(texts, passage));
    const highlight = parts === null ? null : applyPdfHighlight(divs, texts, parts);
    const mark = highlight?.marks[0];
    setMiss(mark === undefined);
    const stage = stageRef.current;
    if (mark !== undefined && stage && movedForCite.current !== cite) {
      movedForCite.current = cite;
      pendingJump.current = null;
      const stageBox = stage.getBoundingClientRect();
      const markBox = mark.getBoundingClientRect();
      stage.scrollTop += markBox.top - stageBox.top - stage.clientHeight / 4;
    }
    return () => highlight?.clear();
  }, [cited, citedPage, span, passage, cite]);

  // スクロールしたら、見ているページと、描くページの範囲を求め直す（飛ぶのは、上の効果だけ。ここでは飛ばない）
  const onScroll = () => {
    const stage = stageRef.current;
    if (!stage || stage.clientHeight === 0) return;
    lastScroll.current = stage.scrollTop;
    const boxes = slotEls.current.slice(0, total).map(boxOf);
    setCurrent(currentPageOf(boxes, stage.scrollTop, stage.clientHeight, stage.scrollHeight));
    const next = visibleRange(boxes, stage.scrollTop, stage.clientHeight);
    setRange((prev) => (prev.first === next.first && prev.last === next.last ? prev : next));
  };

  const commitDraft = () => {
    if (draft === null) return;
    const n = Number.parseInt(draft, 10);
    setDraft(null);
    if (Number.isFinite(n)) jumpTo(clampPage(n, total));
  };

  const sizeOf = (n: number) => {
    const done = sizes.get(n);
    return done !== undefined && done.scale === scale ? done : placeholder;
  };

  const fit = zoom === 1;

  return (
    <div className="pdf-viewer" role="group" aria-label={`${title} の PDF`}>
      {miss && (
        <p className="chunk-miss" role="status">
          {MSG_PDF_CITE_MISS}
        </p>
      )}
      <div
        ref={stageRef}
        className="pdf-stage"
        tabIndex={0}
        role="region"
        aria-label="PDF のページ"
        onScroll={onScroll}
        onWheel={() => void (pendingJump.current = null)}
        onPointerDown={() => void (pendingJump.current = null)}
        onKeyDown={() => void (pendingJump.current = null)}
      >
        {failed ? (
          <div className="documents-error" role="alert">
            <p>{MSG_PDF_RENDER_FAILED}</p>
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setFailed(false);
                setAttempt((n) => n + 1);
              }}
            >
              再試行
            </button>
          </div>
        ) : (
          placeholder === null && <p role="status">読み込み中…</p>
        )}
        {/* ページの絵と文字は、描き終えたときに PDF.js の出力をそのまま入れる（React の子にしない）。枠は全ページ分を先に並べる */}
        {scale !== null &&
          placeholder !== null &&
          Array.from({ length: total }, (_, index) => index + 1).map((n) => {
            const size = sizeOf(n) ?? placeholder;
            return (
              <PdfPageSlot
                key={n}
                doc={doc}
                n={n}
                scale={scale}
                width={size.width}
                height={size.height}
                active={n >= win.from && n <= win.to}
                attempt={attempt}
                setSlot={setSlot}
                onRendered={onRendered}
                onFailed={onFailed}
              />
            );
          })}
      </div>
      <div className="pdf-toolbar" role="toolbar" aria-label="PDF の操作">
        <button type="button" className="pdf-tool" aria-label="前のページ" title="前のページ" disabled={current <= 1} onClick={() => jumpTo(clampPage(current - 1, total))}>
          ‹
        </button>
        <input
          className="pdf-pageno"
          type="text"
          inputMode="numeric"
          aria-label="ページ番号"
          value={draft ?? String(current)}
          onChange={(event) => setDraft(event.target.value)}
          onFocus={(event) => event.target.select()}
          onBlur={commitDraft}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault();
              commitDraft();
            } else if (event.key === 'Escape' && draft !== null) {
              event.stopPropagation(); // 入力を取り消すだけ。パネルは閉じない
              setDraft(null);
            }
          }}
        />
        <span className="pdf-total" aria-hidden="true">
          / {total}
        </span>
        <button type="button" className="pdf-tool" aria-label="次のページ" title="次のページ" disabled={current >= total} onClick={() => jumpTo(clampPage(current + 1, total))}>
          ›
        </button>
        <span className="pdf-sep" aria-hidden="true" />
        <button type="button" className="pdf-tool" aria-label="PDF を縮小" title="縮小" disabled={zoom <= ZOOM_MIN} onClick={() => setZoom(stepZoom(zoom, -1))}>
          −
        </button>
        <button type="button" className="pdf-tool" aria-label="幅に合わせる" title="幅に合わせる" aria-pressed={fit} onClick={() => setZoom(1)}>
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M4 6v12M20 6v12M8 12h8M10.5 9.5 8 12l2.5 2.5M13.5 9.5 16 12l-2.5 2.5" />
          </svg>
        </button>
        <button type="button" className="pdf-tool" aria-label="PDF を拡大" title="拡大" disabled={zoom >= ZOOM_MAX} onClick={() => setZoom(stepZoom(zoom, 1))}>
          ＋
        </button>
        <span className="pdf-sep" aria-hidden="true" />
        <a className="pdf-tool pdf-save" href={url} download={saveName} aria-label="PDF を保存" title="保存（ダウンロード）">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M12 4v11M7 11l5 5 5-5M5 20h14" />
          </svg>
        </a>
      </div>
    </div>
  );
}

interface SlotProps {
  doc: PdfDoc;
  n: number;
  scale: number;
  width: number;
  height: number;
  /** 描くページの範囲の中か。外なら、絵を捨てて枠だけ残す。 */
  active: boolean;
  attempt: number;
  setSlot: (n: number, el: HTMLDivElement | null) => void;
  onRendered: (n: number, scale: number, content: RenderedPage | null) => void;
  onFailed: () => void;
}

/** 1 ページ分の枠。範囲の中にあるときだけ、PDF.js で描く。条件（倍率・範囲）が変わったら、前の描画を中断する。描き終えるまでは、前の絵を見せたまま（ちらつかせない）。 */
function PdfPageSlot({ doc, n, scale, width, height, active, attempt, setSlot, onRendered, onFailed }: SlotProps) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const hasContent = useRef(false);
  const ref = useCallback(
    (el: HTMLDivElement | null) => {
      hostRef.current = el;
      setSlot(n, el);
    },
    [n, setSlot],
  );

  useEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    if (!active) {
      if (hasContent.current) {
        hasContent.current = false;
        host.replaceChildren();
        onRendered(n, scale, null);
      }
      return;
    }
    let stale = false;
    let render: PdfRender | null = null;
    doc
      .getPage(n)
      .then((pdfPage) => {
        if (stale) return;
        render = pdfPage.render(scale);
        return render.done.then((content) => {
          if (stale) return;
          host.replaceChildren(content.canvas, content.textLayer);
          hasContent.current = true;
          onRendered(n, scale, content);
        });
      })
      .catch(() => {
        if (!stale) onFailed();
      });
    return () => {
      stale = true;
      render?.cancel();
    };
  }, [doc, n, scale, active, attempt, onRendered, onFailed]);

  return <div ref={ref} className="pdf-page" data-page={n} style={{ width, height }} />;
}
