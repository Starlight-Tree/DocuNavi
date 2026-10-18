import { useState } from 'react';
import { MSG_PDF_UNAVAILABLE } from '../api/documents';
import type { ChunkItem, DocumentSummary } from '../api/documents';
import { GRAPH_SOURCE } from '../api/graph';
import { useDocumentChunks, useDocumentList, useDocumentPdf } from '../hooks/useDocuments';
import { RelationGraphView } from './RelationGraphView';
import { Status } from './Status';

export const MSG_NOT_CONFIGURED = '資料のデータベースが設定されていません。';
export const MSG_NO_DOCUMENTS = '登録された資料はありません。資料を取り込むと、ここに表示され、チャットで検索できます。';

const STATUS_LABELS: Record<string, string> = { ready: '取り込み済み', failed: '取り込み失敗' };

function statusLabel(document: DocumentSummary): string {
  const label = STATUS_LABELS[document.status] ?? document.status;
  return document.failureReason ? `${label}（${document.failureReason}）` : label;
}

const DATE_FORMAT = new Intl.DateTimeFormat('ja-JP', {
  timeZone: 'Asia/Tokyo',
  year: 'numeric',
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
});

function formatDate(value: string | null): string {
  if (!value) return '-';
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? '-' : DATE_FORMAT.format(date);
}

/** 題名があれば題名を主に、ファイル名を補助に出す。題名が無ければ、ファイル名だけ（同じ文字を 2 度出さない）。 */
function displayName(document: DocumentSummary): string {
  return document.title ?? document.source;
}

function DocumentName({ document }: { document: DocumentSummary }) {
  return (
    <>
      <span className="document-name">{displayName(document)}</span>
      {document.title && <span className="document-file">{document.source}</span>}
    </>
  );
}

function pageLabel(chunk: ChunkItem): string | null {
  if (chunk.page === null) return null;
  return chunk.pageEnd !== null && chunk.pageEnd !== chunk.page
    ? `p.${chunk.page}–${chunk.pageEnd}`
    : `p.${chunk.page}`;
}

function DocumentTable({ onOpen }: { onOpen: (document: DocumentSummary) => void }) {
  const [state, reload] = useDocumentList();

  if (state.status !== 'ready') return <Status state={state} onRetry={reload} />;
  if (!state.data.configured) return <p>{MSG_NOT_CONFIGURED}</p>;
  if (state.data.documents.length === 0) return <p>{MSG_NO_DOCUMENTS}</p>;

  return (
    <div className="documents-table-wrap">
      <table className="documents-table">
        <thead>
          <tr>
            <th scope="col">文書名</th>
            <th scope="col">状態</th>
            <th scope="col">ページ数</th>
            <th scope="col">チャンク数</th>
            <th scope="col">更新</th>
          </tr>
        </thead>
        <tbody>
          {state.data.documents.map((document) => (
            // 行のどこを押しても開く（マウス用）。キーボードと読み上げには、文書名のボタンを使う
            <tr key={document.id} className="documents-row" onClick={() => onOpen(document)}>
              <th scope="row">
                <button
                  type="button"
                  className="document-open"
                  aria-label={`${displayName(document)} を開く`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onOpen(document);
                  }}
                >
                  <DocumentName document={document} />
                </button>
              </th>
              <td data-label="状態">{statusLabel(document)}</td>
              <td data-label="ページ数">{document.pages ?? '-'}</td>
              <td data-label="チャンク数">{document.chunkCount}</td>
              <td data-label="更新">{formatDate(document.updatedAt)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

type ViewMode = 'pdf' | 'text' | 'graph';

/** 元の PDF。ブラウザー内蔵の PDF 表示（iframe）に、認証付きで取った PDF の Blob URL を渡す。 */
function OriginalPdf({ document, id }: { document: DocumentSummary; id: number }) {
  const [state, reload] = useDocumentPdf(id);
  if (state.status !== 'ready') return <Status state={state} onRetry={reload} />;
  return <iframe className="pdf-frame" src={state.data.url} title={`${displayName(document)} の元PDF`} />;
}

function DocumentChunks({ document, onBack }: { document: DocumentSummary; onBack: () => void }) {
  const [state, reload] = useDocumentChunks(document.id);
  const [choice, setChoice] = useState<ViewMode | null>(null);
  const originalPdf = state.status === 'ready' && state.data.originalPdf;
  // 関係図は入寮規程だけ。サーバーが元の PDF から計算するので、元の PDF が使えるときだけ出す
  const hasGraph = originalPdf && document.source === GRAPH_SOURCE;
  // 元の PDF があれば、それを先に見せる。無ければ AI 用テキスト（切り替えは出さず、使えない旨を示す）
  const mode: ViewMode = originalPdf ? (choice ?? 'pdf') : 'text';

  return (
    <section aria-label={`${displayName(document)} のチャンク`}>
      <button type="button" className="btn btn-secondary" onClick={onBack}>
        資料一覧に戻る
      </button>
      <h2 className="documents-title">
        <DocumentName document={document} />
      </h2>
      {state.status !== 'ready' ? (
        <Status state={state} onRetry={reload} />
      ) : (
        <>
          {originalPdf ? (
            <div className="view-switch" role="group" aria-label="表示の切り替え">
              <button
                type="button"
                className="btn btn-secondary"
                aria-pressed={mode === 'pdf'}
                onClick={() => setChoice('pdf')}
              >
                元PDF
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                aria-pressed={mode === 'text'}
                onClick={() => setChoice('text')}
              >
                AI用テキスト
              </button>
              {hasGraph && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  aria-pressed={mode === 'graph'}
                  onClick={() => setChoice('graph')}
                >
                  関係図
                </button>
              )}
            </div>
          ) : (
            <p className="documents-summary">{MSG_PDF_UNAVAILABLE}</p>
          )}
          {/* 切り替えても閉じない（隠すだけ）。PDF を取り直さず、表示しているページも保つ。画面を離れると Blob URL を解放する */}
          {originalPdf && (
            <div hidden={mode !== 'pdf'}>
              <OriginalPdf document={document} id={document.id} />
            </div>
          )}
          {hasGraph && mode === 'graph' && <RelationGraphView document={document} />}
          {mode === 'text' && (
            <>
              <p className="documents-summary">
                {`${state.data.document.chunkCount} チャンク（埋め込み済み ${state.data.document.embeddedCount} / ${state.data.document.chunkCount}）`}
              </p>
              <ol className="chunk-list">
                {state.data.chunks.map((chunk) => (
                  <li key={chunk.chunkIndex} className="chunk">
                    <div className="chunk-meta">
                      <span className="chunk-index">{`#${chunk.chunkIndex}`}</span>
                      {pageLabel(chunk) && <span>{pageLabel(chunk)}</span>}
                      <span className="chunk-heading">{chunk.heading ?? '（見出しなし）'}</span>
                      <span>
                        {chunk.embedded
                          ? `埋め込み済み${chunk.embeddingModel ? `（${chunk.embeddingModel}）` : ''}`
                          : '埋め込みなし'}
                      </span>
                    </div>
                    <pre className="chunk-text">{chunk.content}</pre>
                  </li>
                ))}
              </ol>
            </>
          )}
        </>
      )}
    </section>
  );
}

/** 資料一覧（読み取り専用）。DB に保存された資料を、文書ごとに、チャンクの全文まで見せる。 */
export function DocumentsView() {
  const [selected, setSelected] = useState<DocumentSummary | null>(null);

  return (
    <main className="documents" aria-label="資料一覧">
      {selected ? (
        <DocumentChunks document={selected} onBack={() => setSelected(null)} />
      ) : (
        <DocumentTable onOpen={setSelected} />
      )}
    </main>
  );
}
