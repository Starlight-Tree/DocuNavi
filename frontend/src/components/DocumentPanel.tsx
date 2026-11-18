import { useEffect, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { MSG_PDF_UNAVAILABLE, documentPdfUrl } from '../api/documents';
import type { DocumentSummary } from '../api/documents';
import { GRAPH_SOURCE } from '../api/graph';
import { useAuth } from '../auth/AuthContext';
import { useDocumentChunks } from '../hooks/useDocuments';
import { takePendingFocus, usePanelCiteSeq, usePanelCitationMiss, usePanelOpenSeq, usePanelSpan } from '../lib/panelSignal';
import { locateSpan } from '../lib/sources';
import type { DetailView } from '../lib/urlState';
import { DocumentName, displayName, pageLabel, pdfDownloadName } from './documentDisplay';
import { CloseIcon, CollapseIcon, ExpandIcon } from './icons';
import { RelationGraphView } from './RelationGraphView';
import { Status } from './Status';

export const PANEL_ID = 'ws-panel';
export const MSG_EXTRACTED = 'PDFから抽出したテキストです。レイアウトや図は含まれません。';
export const MSG_CITE_MISS = '出典の箇所を特定できなかったため、強調は付けていません。資料全体を読めます。';
export const MSG_EXTRACTED_OTHER = '資料から抽出したテキストです。';

/**
 * PDF。ブラウザー内蔵の PDF 表示（iframe）に、同じオリジンの URL を渡す（保存ボタンの名前が資料名になる。ADR 0013）。
 * 失敗したときの表示は、中継が返す固定の HTML（iframe の中）。事前に `original_pdf` を確かめてから出すので、通常は届かない。
 */
function OriginalPdf({ document, url, page, cite }: { document: DocumentSummary; url: string; page: number | null; cite: number }) {
  // ページの指定（#page=N）は、ブラウザー内蔵のビューアが読む。ページが変わったとき・出典を押し直したときだけ、
  // iframe を作り直して確実に飛ばす。ほかの切り替えでは作り直さず、見ている位置を保つ
  return (
    <iframe
      key={`${page ?? 'top'}:${cite}`}
      className="pdf-frame"
      src={page === null ? url : `${url}#page=${page}`}
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

  const originalPdf = state.status === 'ready' && state.data.originalPdf;
  // 関係図は入寮規程だけ。サーバーが PDF から計算するので、PDF が使えるときだけ出す
  const hasGraph = originalPdf && document.source === GRAPH_SOURCE;
  // PDF があれば、それを先に見せる。無ければ抽出テキスト（切り替えは出さず、使えない旨を示す）
  // 関係図が使えない資料に view=graph が付いていたら、PDF にする（URL が古い・手で書き換えられたとき）
  const mode: DetailView = originalPdf ? (view === 'graph' && !hasGraph ? 'pdf' : (view ?? 'pdf')) : 'text';

  // 一度でも開いた表示。開いた表示は、切り替えても隠すだけで残す。まだ開いていない表示は、読み込まない
  const [seen, setSeen] = useState<ReadonlySet<DetailView>>(new Set());
  if (state.status === 'ready' && !seen.has(mode)) setSeen(new Set(seen).add(mode));

  // PDF の URL。認証ありの構成は同じオリジンの中継、認証なしの構成は API を直接指す
  const { client } = useAuth();
  const pdfUrl = documentPdfUrl(document.id, pdfDownloadName(document), client !== null);

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
      {/* 最上段は、題名（左。その下に版）とアイコン（右）。「資料」の見出し・取り込みの日付・ページ数・資料グループ名は出さない
          （日付は取り込みの記録で、利用者の判断に使えない。ページ数は PDF のビューアが示す）。
          取り込み用のファイルパスは、常時は出さず、題名に重ねたときの説明にだけ残す */}
      <header className="ws-panel-head ws-panel-head-doc">
        <div className="ws-panel-titles">
          <h2 className="ws-panel-title" title={document.title ? `${name}\n${document.source}` : name}>
            <DocumentName document={document} showFile={false} />
          </h2>
          {/* 版（「初版」「改訂 2」など）。取り込みの版の一覧の値で、無い資料には何も出さない */}
          {document.revision && <p className="ws-panel-revision">{document.revision}</p>}
        </div>
        <span className="ws-panel-tools">
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
              <OriginalPdf document={document} url={pdfUrl} page={page} cite={cite} />
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
