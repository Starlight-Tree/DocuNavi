import { useEffect, useState } from 'react';
import { MSG_PDF_UNAVAILABLE } from '../api/documents';
import type { ChunkItem, DocumentList, DocumentSummary } from '../api/documents';
import { GRAPH_SOURCE } from '../api/graph';
import { usableCollections, useCollectionCatalog } from '../hooks/useCollections';
import { useDocumentChunks, useDocumentList, useDocumentPdf } from '../hooks/useDocuments';
import { collectionLabel, collectionOptions } from '../lib/collections';
import { facetOptions, filterDocuments, sortDocuments } from '../lib/documentFilter';
import { recentDocuments } from '../lib/recentDocuments';
import type { DocumentFilter, FilterContext } from '../lib/documentFilter';
import type { Loaded } from '../hooks/useLoaded';
import type { DetailView, NavigateMode, UrlState } from '../lib/urlState';
import { useUrlState } from '../lib/urlState';
import { DocumentFilterBar } from './DocumentFilterBar';
import { HeroCarousel } from './HeroCarousel';
import { CollectionCards } from './CollectionCards';
import { RelationGraphView } from './RelationGraphView';
import { Status } from './Status';

export const MSG_NOT_CONFIGURED = '資料のデータベースが設定されていません。';
export const MSG_NO_GRAPH = '関係図は、入寮規程だけで見られます（ほかの資料は、まだ対応していません）。';
export const MSG_NO_DOCUMENTS = '登録された資料はありません。資料を取り込むと、ここに表示され、チャットで検索できます。';
export const MSG_NO_DOCUMENTS_IN_GROUP = 'このグループには、まだ資料がありません。';
export const MSG_NO_MATCH = 'キーワードを減らすか、条件をクリアしてください。';

const STATUS_LABELS: Record<string, string> = { ready: '取り込み済み', failed: '取り込み失敗' };

function statusName(status: string): string {
  return STATUS_LABELS[status] ?? status;
}

function statusLabel(document: DocumentSummary): string {
  const label = statusName(document.status);
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

interface DocumentTableProps {
  state: Loaded<DocumentList>;
  /** URL で指定された絞り込み（資料群の名前）。一覧に無い名前は、絞り込みなしと同じに扱う。 */
  collection: string | null;
  onCollectionChange: (collection: string | null) => void;
  /** URL で指定された、一覧の検索・絞り込み・並び替え。 */
  filter: DocumentFilter;
  onFilterChange: (next: Partial<DocumentFilter>, mode?: NavigateMode) => void;
  /** 「このグループに質問」。検索範囲を変える。渡さなければ、ボタンは出さない。 */
  onAskGroup?: ((collection: string, trigger: HTMLElement) => void) | undefined;
  onRetry: () => void;
  onOpen: (document: DocumentSummary) => void;
}

function DocumentTable({ state, collection, onCollectionChange, filter, onFilterChange, onAskGroup, onRetry, onOpen }: DocumentTableProps) {
  const { state: catalogState } = useCollectionCatalog();
  if (state.status !== 'ready') return <Status state={state} onRetry={onRetry} />;
  if (!state.data.configured) return <p>{MSG_NOT_CONFIGURED}</p>;
  if (state.data.documents.length === 0) return <p>{MSG_NO_DOCUMENTS}</p>;

  // グループの一覧（GET /collections）を読めたときは、カードで絞り込む。読めなかったときは、資料にある名前の絞り込みボタンにする
  const groups = usableCollections(catalogState);
  const options = groups ? groups.map((g) => g.name) : collectionOptions(state.data.documents.map((d) => d.collection));
  const active = collection !== null && options.includes(collection) ? collection : null;
  const documents = active === null ? state.data.documents : state.data.documents.filter((d) => d.collection === active);
  // 一覧の検索・絞り込み・並び替えは、種別（カード）で絞った資料に対して行う
  const labelGroups = catalogState.status === 'ready' ? catalogState.data.collections : undefined;
  const context: FilterContext = { now: new Date(), collectionLabel: (name) => collectionLabel(name, labelGroups) };
  const shown = sortDocuments(filterDocuments(documents, filter, context), filter.sort);

  return (
    <>
      {/* 最近更新された資料（ヒーロー）。種別・検索の絞り込みには連動せず、全資料から選ぶ（一覧からも外さない） */}
      <HeroCarousel
        documents={recentDocuments(state.data.documents)}
        collectionLabel={(name) => collectionLabel(name, labelGroups)}
        onOpen={onOpen}
      />
      {groups ? (
        <CollectionCards
          collections={groups}
          active={active}
          totalCount={state.data.documents.length}
          onFilter={onCollectionChange}
          onAsk={onAskGroup}
        />
      ) : (
        options.length > 0 && (
          <div className="view-switch collection-filter" role="group" aria-label="種別で絞り込み">
            <button
              type="button"
              className="btn btn-secondary"
              aria-pressed={active === null}
              onClick={() => onCollectionChange(null)}
            >
              すべて
            </button>
            {options.map((name) => (
              <button
                key={name}
                type="button"
                className="btn btn-secondary"
                aria-pressed={active === name}
                onClick={() => onCollectionChange(name)}
              >
                {collectionLabel(name)}
              </button>
            ))}
          </div>
        )
      )}
      {documents.length === 0 ? (
        <p>{MSG_NO_DOCUMENTS_IN_GROUP}</p>
      ) : (
        <>
          <DocumentFilterBar
            filter={filter}
            options={facetOptions(documents, filter, context, statusName)}
            resultCount={shown.length}
            onChange={onFilterChange}
            onClear={() => onFilterChange({ query: '', status: null, pages: null, updated: null })}
          />
          {shown.length === 0 ? <p className="document-filter-empty">{MSG_NO_MATCH}</p> : <DocumentCards documents={shown} onOpen={onOpen} />}
        </>
      )}
    </>
  );
}

function DocumentCards({ documents, onOpen }: { documents: DocumentSummary[]; onOpen: (document: DocumentSummary) => void }) {
  const { state: catalogState } = useCollectionCatalog();
  const groups = catalogState.status === 'ready' ? catalogState.data.collections : undefined;
  return (
    <div className="documents-table-wrap">
      <table className="documents-table">
        <thead>
          <tr>
            <th scope="col">文書名</th>
            <th scope="col">種別</th>
            <th scope="col">状態</th>
            <th scope="col">ページ数</th>
            <th scope="col">チャンク数</th>
            <th scope="col">更新</th>
          </tr>
        </thead>
        <tbody>
          {documents.map((document) => (
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
              <td data-label="種別" data-collection={document.collection ?? undefined}>
                {document.collection ? collectionLabel(document.collection, groups) : null}
              </td>
              <td data-label="状態" data-status={document.status}>
                {statusLabel(document)}
              </td>
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

/** 元の PDF。ブラウザー内蔵の PDF 表示（iframe）に、認証付きで取った PDF の Blob URL を渡す。 */
function OriginalPdf({ document, id, page }: { document: DocumentSummary; id: number; page: number | null }) {
  const [state, reload] = useDocumentPdf(id);
  if (state.status !== 'ready') return <Status state={state} onRetry={reload} />;
  // ページの指定（#page=N）は、ブラウザー内蔵のビューアが読む。ページが変わったときは、作り直して確実に飛ばす
  return (
    <iframe
      key={page ?? 'top'}
      className="pdf-frame"
      src={page === null ? state.data.url : `${state.data.url}#page=${page}`}
      title={`${displayName(document)} の元PDF`}
    />
  );
}

interface DocumentChunksProps {
  document: DocumentSummary;
  /** URL で指定された表示。無ければ null（元の PDF があれば PDF、無ければ AI 用テキスト）。 */
  view: DetailView | null;
  /** URL で指定された、元PDF で開くページ。 */
  page: number | null;
  onViewChange: (view: DetailView) => void;
  onBack: () => void;
}

function DocumentChunks({ document, view, page, onViewChange, onBack }: DocumentChunksProps) {
  const [state, reload] = useDocumentChunks(document.id);
  // チャンクの番号・埋め込みの状態は、開発者向けの情報。既定では隠す
  const [showDev, setShowDev] = useState(false);
  const originalPdf = state.status === 'ready' && state.data.originalPdf;
  // 関係図は入寮規程だけ。サーバーが元の PDF から計算するので、元の PDF が使えるときだけ出す
  const hasGraph = originalPdf && document.source === GRAPH_SOURCE;
  // 元の PDF があれば、それを先に見せる。無ければ AI 用テキスト（切り替えは出さず、使えない旨を示す）
  // 関係図が使えない資料に ?view=graph が付いていたら、PDF にする（URL が古い・手で書き換えられたとき）
  const mode: DetailView = originalPdf ? (view === 'graph' && !hasGraph ? 'pdf' : (view ?? 'pdf')) : 'text';

  return (
    <section aria-label={`${displayName(document)} のチャンク`}>
      <button type="button" className="btn btn-secondary btn-small" onClick={onBack}>
        資料一覧に戻る
      </button>
      <h2 className="documents-title">
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
              <button
                type="button"
                className="btn btn-secondary"
                aria-pressed={mode === 'pdf'}
                onClick={() => onViewChange('pdf')}
              >
                元PDF
              </button>
              <button
                type="button"
                className="btn btn-secondary"
                aria-pressed={mode === 'text'}
                onClick={() => onViewChange('text')}
              >
                AI用テキスト
              </button>
              {hasGraph && (
                <button
                  type="button"
                  className="btn btn-secondary"
                  aria-pressed={mode === 'graph'}
                  onClick={() => onViewChange('graph')}
                >
                  関係図
                </button>
              )}
            </div>
          ) : (
            <p className="documents-summary">{MSG_PDF_UNAVAILABLE}</p>
          )}
          {originalPdf && !hasGraph && <p className="documents-summary">{MSG_NO_GRAPH}</p>}
          {/* 切り替えても閉じない（隠すだけ）。PDF を取り直さず、表示しているページも保つ。画面を離れると Blob URL を解放する */}
          {originalPdf && (
            <div hidden={mode !== 'pdf'}>
              <OriginalPdf document={document} id={document.id} page={page} />
            </div>
          )}
          {hasGraph && mode === 'graph' && <RelationGraphView document={document} />}
          {mode === 'text' && (
            <>
              <div className="chunk-toolbar">
                <p className="documents-summary">
                  {showDev
                    ? `${state.data.document.chunkCount} チャンク（埋め込み済み ${state.data.document.embeddedCount} / ${state.data.document.chunkCount}）`
                    : 'AI が回答に使う、資料の本文です。'}
                </p>
                <button
                  type="button"
                  className="btn btn-secondary btn-small"
                  aria-pressed={showDev}
                  onClick={() => setShowDev((v) => !v)}
                >
                  開発者向けの情報
                </button>
              </div>
              <ol className="chunk-list">
                {state.data.chunks.map((chunk) => (
                  <li key={chunk.chunkIndex} className="chunk">
                    <div className="chunk-meta">
                      {showDev && <span className="chunk-index">{`#${chunk.chunkIndex}`}</span>}
                      {pageLabel(chunk) && <span>{pageLabel(chunk)}</span>}
                      <span className="chunk-heading">{chunk.heading ?? '（見出しなし）'}</span>
                      {showDev && (
                        <span>
                          {chunk.embedded
                            ? `埋め込み済み${chunk.embeddingModel ? `（${chunk.embeddingModel}）` : ''}`
                            : '埋め込みなし'}
                        </span>
                      )}
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

/**
 * 資料一覧（読み取り専用）。DB に保存された資料を、文書ごとに、チャンクの全文まで見せる。
 * 開いている資料・表示・関係図の項は、URL が決める（戻る・再読み込み・共有で戻れる）。
 */
export function DocumentsView({ onAskGroup }: { onAskGroup?: ((collection: string, trigger: HTMLElement) => void) | undefined } = {}) {
  const { doc, view, page, collection, query, status, pageBand, updatedBand, sort, navigate } = useUrlState();
  const filter: DocumentFilter = { query: query ?? '', status, pages: pageBand, updated: updatedBand, sort: sort ?? 'default' };
  // 画面の条件（DocumentFilter）を、URL の状態（UrlState）に直して書く。空のキーワードと既定の並び順は、URL から外す
  const onFilterChange = (next: Partial<DocumentFilter>, mode?: NavigateMode) => {
    const patch: Partial<UrlState> = {};
    if (next.query !== undefined) patch.query = next.query.trim() === '' ? null : next.query;
    if (next.status !== undefined) patch.status = next.status;
    if (next.pages !== undefined) patch.pageBand = next.pages;
    if (next.updated !== undefined) patch.updatedBand = next.updated;
    if (next.sort !== undefined) patch.sort = next.sort === 'default' ? null : next.sort;
    navigate(patch, mode);
  };
  const [state, reload] = useDocumentList();
  const selected =
    doc !== null && state.status === 'ready' ? (state.data.documents.find((d) => d.id === doc) ?? null) : null;
  const unknownDoc = doc !== null && state.status === 'ready' && selected === null;

  // 一覧に無い資料の ID（消えた・手で書き換えた）は、一覧に戻す。履歴は増やさない
  useEffect(() => {
    if (unknownDoc) navigate({ doc: null }, 'replace');
  }, [unknownDoc, navigate]);

  return (
    <main className="documents" aria-label="資料一覧">
      {selected ? (
        <DocumentChunks
          document={selected}
          view={view}
          page={page}
          onViewChange={(next) => navigate({ view: next })}
          onBack={() => navigate({ doc: null })}
        />
      ) : (
        <DocumentTable
          state={state}
          collection={collection}
          onCollectionChange={(next) => navigate({ collection: next })}
          filter={filter}
          onFilterChange={onFilterChange}
          onAskGroup={onAskGroup}
          onRetry={reload}
          onOpen={(d) => navigate({ doc: d.id })}
        />
      )}
    </main>
  );
}
