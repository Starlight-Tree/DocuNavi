import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { MSG_PDF_UNAVAILABLE } from '../api/documents';
import type { DocumentSummary } from '../api/documents';
import { GRAPH_SOURCE } from '../api/graph';
import { useDocumentChunks, useDocumentPdf } from '../hooks/useDocuments';
import { takePendingFocus, usePanelCiteSeq, usePanelOpenSeq } from '../lib/panelSignal';
import type { DetailView } from '../lib/urlState';
import { DocumentName, displayName, formatDate, pageLabel } from './documentDisplay';
import { RelationGraphView } from './RelationGraphView';
import { Status } from './Status';

export const PANEL_ID = 'ws-panel';
export const MSG_NO_GRAPH = '関係図は、入寮規程だけで見られます（ほかの資料は、まだ対応していません）。';
export const MSG_EXTRACTED = 'PDFから抽出したテキストです。';
export const MSG_EXTRACTED_OTHER = '資料から抽出したテキストです。';

/** 元の PDF。ブラウザー内蔵の PDF 表示（iframe）に、認証付きで取った PDF の Blob URL を渡す。 */
function OriginalPdf({ document, page, cite }: { document: DocumentSummary; page: number | null; cite: number }) {
  const [state, reload] = useDocumentPdf(document.id);
  if (state.status !== 'ready') return <Status state={state} onRetry={reload} />;
  // ページの指定（#page=N）は、ブラウザー内蔵のビューアが読む。ページが変わったとき・出典を押し直したときだけ、
  // iframe を作り直して確実に飛ばす（作り直しても Blob URL は取り直さない）。ほかの切り替えでは作り直さず、見ている位置を保つ
  return (
    <iframe
      key={`${page ?? 'top'}:${cite}`}
      className="pdf-frame"
      src={page === null ? state.data.url : `${state.data.url}#page=${page}`}
      title={`${displayName(document)} の元PDF`}
    />
  );
}

interface DocumentPanelProps {
  /** 開く資料。別の資料に変わるときは、呼び出し側が `key` で作り直す（前の資料のページ・強調・関係図の選択を引き継がない）。 */
  document: DocumentSummary;
  /** URL で指定された表示。無ければ null（元の PDF があれば PDF、無ければ抽出テキスト）。 */
  view: DetailView | null;
  /** 「元のPDF」で開くページ。出典が持つページだけ（推測しない）。 */
  page: number | null;
  /** 抽出テキストで根拠として強調するチャンクの番号。見つからなければ null（何も強調しない）。 */
  chunk: number | null;
  expanded: boolean;
  onToggleExpand: () => void;
  onViewChange: (view: DetailView) => void;
  onClose: () => void;
}

/**
 * 右のパネル。資料の「元のPDF」「抽出テキスト」「関係図」を、タブで切り替えて見る。
 * 切り替えても閉じない（隠すだけ）ので、PDF の位置と関係図の視点は保たれる。ただし、初めて開くまでは読み込まない。
 * 拡大・縮小は CSS だけで行う（同じ要素のまま。PDF・関係図・チャットを作り直さない）。
 */
export function DocumentPanel({ document, view, page, chunk, expanded, onToggleExpand, onViewChange, onClose }: DocumentPanelProps) {
  const [state, reload] = useDocumentChunks(document.id);
  // チャンクの番号・埋め込みの状態は、開発者向けの情報。既定では隠す
  const [showDev, setShowDev] = useState(false);
  const ref = useRef<HTMLElement>(null);
  const openSeq = usePanelOpenSeq();
  const cite = usePanelCiteSeq();

  const originalPdf = state.status === 'ready' && state.data.originalPdf;
  // 関係図は入寮規程だけ。サーバーが元の PDF から計算するので、元の PDF が使えるときだけ出す
  const hasGraph = originalPdf && document.source === GRAPH_SOURCE;
  // 元の PDF があれば、それを先に見せる。無ければ抽出テキスト（切り替えは出さず、使えない旨を示す）
  // 関係図が使えない資料に view=graph が付いていたら、PDF にする（URL が古い・手で書き換えられたとき）
  const mode: DetailView = originalPdf ? (view === 'graph' && !hasGraph ? 'pdf' : (view ?? 'pdf')) : 'text';

  // 一度でも開いた表示。開いた表示は、切り替えても隠すだけで残す。まだ開いていない表示は、読み込まない
  const [seen, setSeen] = useState<ReadonlySet<DetailView>>(new Set());
  if (state.status === 'ready' && !seen.has(mode)) setSeen(new Set(seen).add(mode));

  // 利用者の操作で開いたときは、パネルへフォーカスを移す（戻る・進む・再読み込みで開いたときは動かさない）
  useEffect(() => {
    if (takePendingFocus()) ref.current?.focus({ preventScroll: true });
  }, [openSeq]);

  // 根拠のチャンクが見える位置までスクロールする（出典を押し直したときも、また飛ぶ）
  const textReady = state.status === 'ready' && seen.has('text');
  useEffect(() => {
    if (chunk === null || mode !== 'text' || !textReady) return;
    ref.current?.querySelector<HTMLElement>('[data-focus="true"]')?.scrollIntoView?.({ block: 'center' });
  }, [chunk, cite, mode, textReady]);

  const handleKeyDown = (event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'Escape') return;
    event.preventDefault();
    if (expanded) onToggleExpand();
    else onClose();
  };

  const name = displayName(document);
  const extractedLabel = originalPdf ? MSG_EXTRACTED : MSG_EXTRACTED_OTHER;

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
          <button type="button" className="btn btn-secondary btn-small" aria-pressed={expanded} onClick={onToggleExpand}>
            {expanded ? '縮小' : '拡大'}
          </button>
          <button type="button" className="ws-icon-btn" aria-label="パネルを閉じる" onClick={onClose}>
            ×
          </button>
        </span>
      </header>
      <h2 className="ws-panel-title">
        <DocumentName document={document} />
      </h2>
      <p className="document-meta">
        {document.pages !== null && <span>{`${document.pages} ページ`}</span>}
        <span>{`更新 ${formatDate(document.updatedAt)}`}</span>
      </p>
      {state.status !== 'ready' ? (
        <Status state={state} onRetry={reload} />
      ) : (
        <>
          {originalPdf ? (
            <div className="view-switch" role="group" aria-label="表示の切り替え">
              <button type="button" className="btn btn-secondary" aria-pressed={mode === 'pdf'} onClick={() => onViewChange('pdf')}>
                元のPDF
              </button>
              <button type="button" className="btn btn-secondary" aria-pressed={mode === 'text'} onClick={() => onViewChange('text')}>
                抽出テキスト
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
          {originalPdf && !hasGraph && <p className="documents-summary">{MSG_NO_GRAPH}</p>}

          {originalPdf && seen.has('pdf') && (
            <div className="ws-view-body" hidden={mode !== 'pdf'}>
              <OriginalPdf document={document} page={page} cite={cite} />
            </div>
          )}
          {hasGraph && seen.has('graph') && (
            <div className="ws-view-body" hidden={mode !== 'graph'}>
              <RelationGraphView document={document} />
            </div>
          )}
          {seen.has('text') && (
            <div className="ws-view-body" hidden={mode !== 'text'}>
              <div className="chunk-toolbar">
                <p className="documents-summary">
                  {showDev
                    ? `${state.data.document.chunkCount} チャンク（埋め込み済み ${state.data.document.embeddedCount} / ${state.data.document.chunkCount}）`
                    : extractedLabel}
                </p>
                <button type="button" className="btn btn-secondary btn-small" aria-pressed={showDev} onClick={() => setShowDev((v) => !v)}>
                  開発者向けの情報
                </button>
              </div>
              <ol className="chunk-list">
                {state.data.chunks.map((item) => {
                  const focused = chunk !== null && item.chunkIndex === chunk;
                  return (
                    <li key={item.chunkIndex} className={focused ? 'chunk chunk-focus' : 'chunk'} data-focus={focused ? 'true' : undefined}>
                      <div className="chunk-meta">
                        {focused && <span className="chunk-cite">根拠の箇所</span>}
                        {showDev && <span className="chunk-index">{`#${item.chunkIndex}`}</span>}
                        {pageLabel(item) && <span>{pageLabel(item)}</span>}
                        <span className="chunk-heading">{item.heading ?? '（見出しなし）'}</span>
                        {showDev && (
                          <span>
                            {item.embedded ? `埋め込み済み${item.embeddingModel ? `（${item.embeddingModel}）` : ''}` : '埋め込みなし'}
                          </span>
                        )}
                      </div>
                      <pre className="chunk-text">{focused ? <mark>{item.content}</mark> : item.content}</pre>
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
