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
import { CloseIcon, CollapseIcon, ExpandIcon, FileIcon, GraphIcon, TextIcon } from './icons';
import { RelationGraphView } from './RelationGraphView';
import { PdfViewer } from './PdfViewer';
import { Status } from './Status';

export const PANEL_ID = 'ws-panel';
export const MSG_EXTRACTED = 'PDFから抽出したテキストです。レイアウトや図は含まれません。';
export const MSG_CITE_MISS = '出典の箇所を特定できなかったため、強調は付けていません。資料全体を読めます。';
export const MSG_EXTRACTED_OTHER = '資料から抽出したテキストです。';

interface DocumentPanelProps {
  /** 開く資料。別の資料に変わるときは、呼び出し側が `key` で作り直す（前の資料のページ・強調・関係図の選択を引き継がない）。 */
  document: DocumentSummary;
  /** URL で指定された表示。無ければ null（既定は抽出テキスト）。 */
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
 * 右のパネル。資料の「抽出テキスト」「PDF」「関係図」のうち、いま開いている 1 つを出し、ほかの表示へはボタンで移る（タブは置かない）。
 * 既定は抽出テキスト（ナレッジとして取り込んだ内容。運用者が取り込みの結果を確かめる）。チャットの出典からは PDF で開く（原本で根拠を確かめる）。
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
  // 既定は抽出テキスト。PDF が無い資料は、いつも抽出テキスト（切り替えは出さず、使えない旨を示す）
  // 関係図が使えない資料に view=graph が付いていたら、抽出テキストにする（URL が古い・手で書き換えられたとき）
  const mode: DetailView = originalPdf ? (view === 'graph' && !hasGraph ? 'text' : (view ?? 'text')) : 'text';

  // 一度でも開いた表示。開いた表示は、切り替えても隠すだけで残す。まだ開いていない表示は、読み込まない
  const [seen, setSeen] = useState<ReadonlySet<DetailView>>(new Set());
  if (state.status === 'ready' && !seen.has(mode)) setSeen(new Set(seen).add(mode));

  // PDF の URL（PDF.js が取る）。認証ありの構成は同じオリジンの中継（Cookie が付く。ADR 0013）、認証なしの構成は API を直接指す
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
      className={['ws-panel', expanded && 'ws-panel-expanded', mode === 'pdf' && originalPdf && 'ws-panel-pdf'].filter(Boolean).join(' ')}
      aria-label={`資料: ${name}`}
      tabIndex={-1}
      onKeyDown={handleKeyDown}
    >
      {/* 最上段は、題名（左。その上に版）とアイコン（右）。「資料」の見出し・取り込みの日付・ページ数・資料グループ名は出さない
          （日付は取り込みの記録で、利用者の判断に使えない。ページ数は PDF のビューアが示す）。
          取り込み用のファイルパスは、常時は出さず、題名に重ねたときの説明にだけ残す */}
      <header className="ws-panel-head ws-panel-head-doc">
        <div className="ws-panel-titles">
          {/* 版（「初版」「改訂 2」など）。題名の上の小さな文字（以前「資料」の見出しがあった位置）。取り込みの版の一覧の値で、無い資料には何も出さない */}
          {document.revision && <p className="ws-panel-revision">{document.revision}</p>}
          <h2 className="ws-panel-title" title={document.title ? `${name}\n${document.source}` : name}>
            <DocumentName document={document} showFile={false} />
          </h2>
        </div>
        <span className="ws-panel-tools">
          {/* ほかの表示へ移るボタン（アイコンだけ。説明は title と aria-label）。いま開いている表示のボタンは出さない。「PDF から抽出したテキスト」という断り書きは、抽出テキストのボタンの説明にする */}
          {originalPdf && (
            <span className="view-links" role="group" aria-label="表示の切り替え">
              {mode !== 'text' && (
                <button type="button" className="ws-icon-btn" aria-label="抽出テキストを見る" title={MSG_EXTRACTED} onClick={() => onViewChange('text')}>
                  <TextIcon />
                </button>
              )}
              {mode !== 'pdf' && (
                <button type="button" className="ws-icon-btn" aria-label="PDFを開く" title="PDFを開く" onClick={() => onViewChange('pdf')}>
                  <FileIcon />
                </button>
              )}
              {hasGraph && mode !== 'graph' && (
                <button type="button" className="ws-icon-btn" aria-label="関係図を見る" title="関係図を見る" onClick={() => onViewChange('graph')}>
                  <GraphIcon />
                </button>
              )}
            </span>
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
      {state.status !== 'ready' ? (
        <Status state={state} onRetry={reload} />
      ) : (
        <>
          {!originalPdf && <p className="documents-summary">{MSG_PDF_UNAVAILABLE}</p>}

          {citationMiss && mode !== 'graph' && (
            <p className="chunk-miss" role="status">
              {MSG_CITE_MISS}
            </p>
          )}

          {originalPdf && seen.has('pdf') && (
            <div className="ws-view-body" hidden={mode !== 'pdf'}>
              <PdfViewer
                title={name}
                url={pdfUrl}
                saveName={pdfDownloadName(document)}
                page={page}
                cite={cite}
                span={span}
                passage={chunk === null ? null : (state.data.chunks.find((item) => item.chunkIndex === chunk)?.content ?? null)}
              />
            </div>
          )}
          {hasGraph && seen.has('graph') && (
            <div className="ws-view-body" hidden={mode !== 'graph'}>
              <RelationGraphView document={document} />
            </div>
          )}
          {seen.has('text') && (
            <div className="ws-view-body" hidden={mode !== 'text'}>
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
