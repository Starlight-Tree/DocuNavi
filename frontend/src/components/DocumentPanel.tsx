import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { MSG_PDF_UNAVAILABLE } from '../api/documents';
import type { DocumentSummary } from '../api/documents';
import { GRAPH_SOURCE } from '../api/graph';
import { useCollectionCatalog } from '../hooks/useCollections';
import { useDocumentChunks, useDocumentPdf } from '../hooks/useDocuments';
import { collectionLabel } from '../lib/collections';
import { takePendingFocus, usePanelCiteSeq, usePanelCitationMiss, usePanelOpenSeq, usePanelSpan } from '../lib/panelSignal';
import { locateSpan } from '../lib/sources';
import type { DetailView } from '../lib/urlState';
import { DocumentName, UpdatedAt, displayName, pageLabel, pdfDownloadName } from './documentDisplay';
import { CloseIcon, CollapseIcon, DownloadIcon, ExpandIcon } from './icons';
import { RelationGraphView } from './RelationGraphView';
import { Status } from './Status';

export const PANEL_ID = 'ws-panel';
export const MSG_EXTRACTED = 'PDFから抽出したテキストです。レイアウトや図は含まれません。';
export const MSG_CITE_MISS = '出典の箇所を特定できなかったため、強調は付けていません。資料全体を読めます。';
export const MSG_EXTRACTED_OTHER = '資料から抽出したテキストです。';

type PdfState = ReturnType<typeof useDocumentPdf>[0];

/**
 * PDF。ブラウザー内蔵の PDF 表示（iframe）に、認証付きで取った PDF の Blob URL を渡す。
 * PDF の取得はパネル本体が持つ（ヘッダーのダウンロードのアイコンも同じ Blob URL を使うため）。
 */
function OriginalPdf({ document, pdf, onRetry, page, cite }: { document: DocumentSummary; pdf: PdfState; onRetry: () => void; page: number | null; cite: number }) {
  if (pdf.status !== 'ready') return <Status state={pdf} onRetry={onRetry} />;
  // ページの指定（#page=N）は、ブラウザー内蔵のビューアが読む。ページが変わったとき・出典を押し直したときだけ、
  // iframe を作り直して確実に飛ばす（作り直しても Blob URL は取り直さない）。ほかの切り替えでは作り直さず、見ている位置を保つ
  return (
    <iframe
      key={`${page ?? 'top'}:${cite}`}
      className="pdf-frame"
      src={page === null ? pdf.data.url : `${pdf.data.url}#page=${page}`}
      title={`${displayName(document)} の PDF`}
    />
  );
}

interface DocumentPanelProps {
  /** 開く資料。別の資料に変わるときは、呼び出し側が `key` で作り直す（前の資料のページ・強調・関係図の選択を引き継がない）。 */
  document: DocumentSummary;
  /** URL で指定された表示。無ければ null（PDF があれば PDF、無ければ抽出テキスト）。 */
  view: DetailView | null;
  /** 「PDF」で開くページ。出典が持つページだけ（推測しない）。 */
  page: number | null;
  /** 抽出テキストで根拠として強調するチャンクの番号。見つからなければ null（何も強調しない）。 */
  chunk: number | null;
  expanded: boolean;
  onToggleExpand: () => void;
  onViewChange: (view: DetailView) => void;
  onClose: () => void;
}

/**
 * 右のパネル。資料の「PDF」「抽出テキスト」「関係図」を、タブで切り替えて見る。
 * 切り替えても閉じない（隠すだけ）ので、PDF の位置と関係図の視点は保たれる。ただし、初めて開くまでは読み込まない。
 * 拡大・縮小は CSS だけで行う（同じ要素のまま。PDF・関係図・チャットを作り直さない）。
 */
export function DocumentPanel({ document, view, page, chunk, expanded, onToggleExpand, onViewChange, onClose }: DocumentPanelProps) {
  const [state, reload] = useDocumentChunks(document.id);
  const ref = useRef<HTMLElement>(null);
  const openSeq = usePanelOpenSeq();
  const cite = usePanelCiteSeq();
  const span = usePanelSpan();
  const citationMiss = usePanelCitationMiss() === document.id && chunk === null;
  const { state: catalog } = useCollectionCatalog();
  const groupName = document.collection
    ? collectionLabel(document.collection, catalog.status === 'ready' ? catalog.data.collections : undefined)
    : null;

  const originalPdf = state.status === 'ready' && state.data.originalPdf;
  // 関係図は入寮規程だけ。サーバーが PDF から計算するので、PDF が使えるときだけ出す
  const hasGraph = originalPdf && document.source === GRAPH_SOURCE;
  // PDF があれば、それを先に見せる。無ければ抽出テキスト（切り替えは出さず、使えない旨を示す）
  // 関係図が使えない資料に view=graph が付いていたら、PDF にする（URL が古い・手で書き換えられたとき）
  const mode: DetailView = originalPdf ? (view === 'graph' && !hasGraph ? 'pdf' : (view ?? 'pdf')) : 'text';

  // 一度でも開いた表示。開いた表示は、切り替えても隠すだけで残す。まだ開いていない表示は、読み込まない
  const [seen, setSeen] = useState<ReadonlySet<DetailView>>(new Set());
  if (state.status === 'ready' && !seen.has(mode)) setSeen(new Set(seen).add(mode));

  // PDF は、初めて「PDF」を開くまで取らない。取ったあとは、表示が切り替わっても持ち続ける
  const [pdf, reloadPdf] = useDocumentPdf(originalPdf && seen.has('pdf') ? document.id : null);

  // 利用者の操作で開いたときは、パネルへフォーカスを移す（戻る・進む・再読み込みで開いたときは動かさない）
  useEffect(() => {
    if (takePendingFocus()) ref.current?.focus({ preventScroll: true });
  }, [openSeq]);

  // 根拠のチャンクが見える位置までスクロールする（出典を押し直したときも、また飛ぶ）
  const textReady = state.status === 'ready' && seen.has('text');
  useEffect(() => {
    if (chunk === null || mode !== 'text' || !textReady) return;
    // 根拠の語句があれば、その位置へ。無ければ、根拠のチャンクへ
    const target = ref.current?.querySelector<HTMLElement>('mark.span-mark') ?? ref.current?.querySelector<HTMLElement>('[data-focus="true"]');
    target?.scrollIntoView?.({ block: 'center' });
  }, [chunk, cite, mode, textReady]);

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    if (expanded) onToggleExpand();
    else onClose();
  };

  const name = displayName(document);

  return (
    <aside
      ref={ref}
      id={PANEL_ID}
      className={expanded ? 'ws-panel ws-panel-expanded' : 'ws-panel'}
      aria-label={`資料: ${name}`}
      tabIndex={-1}
      onKeyDown={handleKeyDown}
    >
      <header className="ws-panel-head">
        <span className="ws-eyebrow">資料</span>
        <span className="ws-panel-tools">
          {mode === 'pdf' && pdf.status === 'ready' && (
            // 内蔵ビューアの保存は Blob URL の ID が名前になる。ここは、資料名（題名）で保存する
            // （取得済みの Blob をそのまま使う。追加の通信も権限の確認も増えない）
            <a className="ws-icon-btn" href={pdf.data.url} download={pdfDownloadName(document)} aria-label="PDF をダウンロード" title="PDF をダウンロード（資料名で保存）">
              <DownloadIcon />
            </a>
          )}
          <button
            type="button"
            className="ws-icon-btn"
            aria-label={expanded ? '縮小' : '拡大'}
            title={expanded ? '縮小' : '拡大'}
            aria-pressed={expanded}
            onClick={onToggleExpand}
          >
            {expanded ? <CollapseIcon /> : <ExpandIcon />}
          </button>
          <button type="button" className="ws-icon-btn" aria-label="パネルを閉じる" title="閉じる" onClick={onClose}>
            <CloseIcon />
          </button>
        </span>
      </header>
      {/* 取り込み用のファイルパスは利用者に意味が無いので、常時は出さない（重ねたときの説明にだけ残す） */}
      <h2 className="ws-panel-title" title={document.title ? document.source : undefined}>
        <DocumentName document={document} showFile={false} />
      </h2>
      <p className="document-meta">
        {groupName && <span>{groupName}</span>}
        {document.pages !== null && <span>{`${document.pages} ページ`}</span>}
        <span>
          更新 <UpdatedAt value={document.updatedAt} withTime />
        </span>
      </p>
      {state.status !== 'ready' ? (
        <Status state={state} onRetry={reload} />
      ) : (
        <>
          {originalPdf ? (
            <div className="view-switch" role="group" aria-label="表示の切り替え">
              {/* 「PDF から抽出したテキスト」という断り書きは、常時は出さず、このタブのホバーの説明（title）にする */}
              <button type="button" className="btn btn-secondary" aria-pressed={mode === 'text'} title={MSG_EXTRACTED} onClick={() => onViewChange('text')}>
                抽出テキスト
              </button>
              <button type="button" className="btn btn-secondary" aria-pressed={mode === 'pdf'} onClick={() => onViewChange('pdf')}>
                PDF
              </button>
              {hasGraph && (
                <button type="button" className="btn btn-secondary" aria-pressed={mode === 'graph'} onClick={() => onViewChange('graph')}>
                  関係図
                </button>
              )}
            </div>
          ) : (
            <p className="documents-summary">{MSG_PDF_UNAVAILABLE}</p>
          )}

          {originalPdf && seen.has('pdf') && (
            <div className="ws-view-body" hidden={mode !== 'pdf'}>
              <OriginalPdf document={document} pdf={pdf} onRetry={reloadPdf} page={page} cite={cite} />
            </div>
          )}
          {hasGraph && seen.has('graph') && (
            <div className="ws-view-body" hidden={mode !== 'graph'}>
              <RelationGraphView document={document} />
            </div>
          )}
          {seen.has('text') && (
            <div className="ws-view-body" hidden={mode !== 'text'}>
              {citationMiss && (
                <p className="chunk-miss" role="status">
                  {MSG_CITE_MISS}
                </p>
              )}
              {!originalPdf && <p className="documents-summary">{MSG_EXTRACTED_OTHER}</p>}
              <ol className="chunk-list">
                {state.data.chunks.map((item) => {
                  const focused = chunk !== null && item.chunkIndex === chunk;
                  return (
                    <li key={item.chunkIndex} className={focused ? 'chunk chunk-focus' : 'chunk'} data-focus={focused ? 'true' : undefined}>
                      <div className="chunk-meta">
                        {focused && <span className="chunk-cite">根拠の箇所</span>}
                        {pageLabel(item) && <span>{pageLabel(item)}</span>}
                        <span className="chunk-heading">{item.heading ?? '（見出しなし）'}</span>
                      </div>
                      <pre className="chunk-text">{focused ? <FocusedText content={item.content} span={span} /> : item.content}</pre>
                    </li>
                  );
                })}
              </ol>
            </div>
          )}
        </>
      )}
    </aside>
  );
}

/**
 * 根拠のチャンクの本文。根拠の語句（引用先の本文にそのまま含まれると確かめたもの）が、このチャンクの本文にあれば、その範囲だけを強調する。
 * 見つからなければ、チャンク全体を強調する（語句を推測で当てない）。
 */
function FocusedText({ content, span }: { content: string; span: string | null }) {
  const range = span === null ? null : locateSpan(content, span);
  if (range === null) return <mark>{content}</mark>;
  const [start, end] = range;
  return (
    <mark>
      {content.slice(0, start)}
      <mark className="span-mark" data-testid="span-mark">
        {content.slice(start, end)}
      </mark>
      {content.slice(end)}
    </mark>
  );
}
