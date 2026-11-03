import { useCollectionCatalog, usableCollections } from '../hooks/useCollections';
import type { DocumentList, DocumentSummary } from '../api/documents';
import { collectionLabel, collectionOptions } from '../lib/collections';
import { facetOptions, filterDocuments, sortDocuments } from '../lib/documentFilter';
import type { DocumentFilter, FacetOption, FilterContext } from '../lib/documentFilter';
import type { Loaded } from '../hooks/useLoaded';
import type { NavigateMode, UrlState } from '../lib/urlState';
import { useUrlState } from '../lib/urlState';
import { signalPanelOpen } from '../lib/panelSignal';
import { DocumentFilterBar } from './DocumentFilterBar';
import { DocumentName, UpdatedAt, displayName, statusName } from './documentDisplay';
import { Status } from './Status';

export const MSG_NOT_CONFIGURED = '資料のデータベースが設定されていません。';
export const MSG_NO_DOCUMENTS = '登録された資料はありません。資料を取り込むと、ここに表示され、チャットで検索できます。';
export const MSG_NO_DOCUMENTS_IN_GROUP = 'このグループには、まだ資料がありません。';
export const MSG_NO_MATCH = 'キーワードを減らすか、条件をクリアしてください。';
export const MSG_FILTER_IS_NOT_SCOPE = '一覧の絞り込みは、チャットの質問対象を変えません。質問するには、右のボタンを押します。';

/** 取り込みの状態の見た目。`ready` と `failed` 以外（取り込み中など）は、待ちの見た目にする。 */
function statusKind(status: string): 'ready' | 'failed' | 'processing' {
  return status === 'ready' ? 'ready' : status === 'failed' ? 'failed' : 'processing';
}

function DocumentStatus({ document }: { document: DocumentSummary }) {
  const kind = statusKind(document.status);
  const glyph = kind === 'ready' ? '✓' : kind === 'failed' ? '!' : '…';
  return (
    <>
      <span className={`ws-status ws-status-${kind}`} data-status={document.status}>
        <span aria-hidden="true">{glyph}</span>
        {statusName(document.status)}
      </span>
      {document.failureReason && <span className="doc-status-reason">{document.failureReason}</span>}
    </>
  );
}

interface DocumentTableProps {
  state: Loaded<DocumentList>;
  /** URL で指定された絞り込み（資料グループの名前）。一覧に無い名前は、絞り込みなしと同じに扱う。 */
  collection: string | null;
  onCollectionChange: (collection: string | null) => void;
  /** URL で指定された、一覧の検索・絞り込み・並び替え。 */
  filter: DocumentFilter;
  onFilterChange: (next: Partial<DocumentFilter>, mode?: NavigateMode) => void;
  /** 「このグループに質問」「すべての資料に質問」（null は「すべて」）。検索範囲を変える。渡さなければ、ボタンは出さない。 */
  onAskGroup?: ((collection: string | null, trigger: HTMLElement) => void) | undefined;
  /** いまのチャットの質問対象の表示名。一覧の絞り込みとは別のものだと示すために出す。 */
  chatTargetLabel?: string | undefined;
  onRetry: () => void;
  /** 開いている資料（右のパネル）の ID。その行を示す。 */
  openId: number | null;
  onOpen: (document: DocumentSummary, opener: Element | null) => void;
}

function DocumentTable({
  state,
  collection,
  onCollectionChange,
  filter,
  onFilterChange,
  onAskGroup,
  chatTargetLabel,
  onRetry,
  openId,
  onOpen,
}: DocumentTableProps) {
  const { state: catalogState } = useCollectionCatalog();
  if (state.status !== 'ready') return <Status state={state} onRetry={onRetry} />;
  if (!state.data.configured) return <p>{MSG_NOT_CONFIGURED}</p>;
  if (state.data.documents.length === 0) return <p>{MSG_NO_DOCUMENTS}</p>;

  // グループの一覧（GET /collections）を読めたときは、その表示名で選ぶ。読めなかったときは、資料にある名前（識別名）で選ぶ
  const groups = usableCollections(catalogState);
  const names = groups ? groups.map((g) => g.name) : collectionOptions(state.data.documents.map((d) => d.collection));
  const active = collection !== null && names.includes(collection) ? collection : null;
  const labelGroups = catalogState.status === 'ready' ? catalogState.data.collections : undefined;
  const documents = active === null ? state.data.documents : state.data.documents.filter((d) => d.collection === active);
  // 一覧の検索・絞り込み・並び替えは、グループで絞った資料に対して行う
  const context: FilterContext = { now: new Date(), collectionLabel: (name) => collectionLabel(name, labelGroups) };
  const shown = sortDocuments(filterDocuments(documents, filter, context), filter.sort);

  const countIn = (name: string) => state.data.documents.filter((d) => d.collection === name).length;
  const groupOptions: FacetOption[] = [
    { value: null, label: 'すべて', count: state.data.documents.length },
    ...names.map((name) => {
      return { value: name, label: collectionLabel(name, labelGroups), count: countIn(name) };
    }),
  ];
  const activeLabel = active === null ? 'すべての資料' : collectionLabel(active, labelGroups);
  const activeInfo = active === null ? undefined : groups?.find((g) => g.name === active);

  return (
    <>
      <DocumentFilterBar
        filter={filter}
        group={names.length > 0 ? { value: active, options: groupOptions, onChange: onCollectionChange } : undefined}
        options={facetOptions(documents, filter, context, statusName)}
        resultCount={shown.length}
        onChange={onFilterChange}
        onClear={() => onFilterChange({ query: '', status: null, pages: null, updated: null })}
        trailing={
          onAskGroup && (
            <div className="docs-ask" role="group" aria-label="質問する">
              <span className="docs-ask-target" title={activeInfo?.description || undefined}>
                <span className="docs-ask-label">質問の対象</span>
                <strong>{activeLabel}</strong>
              </span>
              <button
                type="button"
                className="btn btn-secondary"
                aria-label={active === null ? 'すべて: すべての資料に質問' : `${activeLabel}: このグループに質問`}
                onClick={(event) => onAskGroup(active, event.currentTarget)}
              >
                {active === null ? 'すべての資料に質問' : 'このグループに質問'}
              </button>
            </div>
          )
        }
      />
      {onAskGroup && (
        <p className="docs-ask-note">
          {MSG_FILTER_IS_NOT_SCOPE}
          {chatTargetLabel !== undefined && <span> 現在のチャットの質問対象: {chatTargetLabel}</span>}
        </p>
      )}
      {documents.length === 0 ? (
        <p className="document-filter-empty">{MSG_NO_DOCUMENTS_IN_GROUP}</p>
      ) : shown.length === 0 ? (
        <p className="document-filter-empty">{MSG_NO_MATCH}</p>
      ) : (
        <DocumentRows documents={shown} openId={openId} onOpen={onOpen} />
      )}
    </>
  );
}

interface DocumentRowsProps {
  documents: DocumentSummary[];
  openId: number | null;
  onOpen: (document: DocumentSummary, opener: Element | null) => void;
}

/** 資料のテーブル。列は、API が返す情報だけ（資料名・グループ・状態・ページ数・更新日時）。資料名を押すと、右のパネルに詳細を開く。 */
function DocumentRows({ documents, openId, onOpen }: DocumentRowsProps) {
  const { state: catalogState } = useCollectionCatalog();
  const groups = catalogState.status === 'ready' ? catalogState.data.collections : undefined;
  return (
    <div className="documents-table-wrap">
      <table className="documents-table">
        <thead>
          <tr>
            <th scope="col">資料名</th>
            <th scope="col">グループ</th>
            <th scope="col">状態</th>
            <th scope="col" className="col-num">
              ページ数
            </th>
            <th scope="col">更新日時</th>
          </tr>
        </thead>
        <tbody>
          {documents.map((document) => (
            // 行のどこを押しても開く（マウス用）。キーボードと読み上げには、資料名のボタンを使う
            <tr
              key={document.id}
              className="documents-row"
              aria-current={document.id === openId ? 'true' : undefined}
              onClick={(event) => onOpen(document, event.currentTarget.querySelector('.document-open'))}
            >
              <th scope="row">
                <button
                  type="button"
                  className="document-open"
                  aria-label={`${displayName(document)} を開く`}
                  onClick={(event) => {
                    event.stopPropagation();
                    onOpen(document, event.currentTarget);
                  }}
                >
                  <DocumentName document={document} />
                </button>
              </th>
              <td data-collection={document.collection ?? undefined}>
                {document.collection ? collectionLabel(document.collection, groups) : <span className="doc-missing">未設定</span>}
              </td>
              <td>
                <DocumentStatus document={document} />
              </td>
              <td className="col-num">
                {document.pages ?? (
                  <span className="doc-missing" title="ページ数の記録がありません">
                    —
                  </span>
                )}
              </td>
              <td>
                <UpdatedAt value={document.updatedAt} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

interface DocumentsViewProps {
  /** 資料の一覧（GET /documents）。右のパネルと共有するので、呼び出し側が取る。 */
  state: Loaded<DocumentList>;
  onReload: () => void;
  onAskGroup?: ((collection: string | null, trigger: HTMLElement) => void) | undefined;
  chatTargetLabel?: string | undefined;
}

/**
 * 資料ライブラリ（読み取り専用）。資料を押すと、右のパネルに開く（一覧は残る）。
 * 開いている資料・絞り込み・並び替えは、URL が決める（戻る・再読み込み・共有で戻れる）。
 * 一覧の絞り込み（表示する資料を絞る）と、チャットの質問対象（回答に使う資料の範囲）は、別のもの。
 */
export function DocumentsView({ state, onReload, onAskGroup, chatTargetLabel }: DocumentsViewProps) {
  const { doc, collection, query, status, pageBand, updatedBand, sort, navigate } = useUrlState();
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

  return (
    <main className="documents" aria-label="資料一覧">
      <h2 className="docs-title">資料</h2>
      <DocumentTable
        state={state}
        collection={collection}
        onCollectionChange={(next) => navigate({ collection: next })}
        filter={filter}
        onFilterChange={onFilterChange}
        onAskGroup={onAskGroup}
        chatTargetLabel={chatTargetLabel}
        onRetry={onReload}
        openId={doc}
        onOpen={(d, opener) => {
          signalPanelOpen(opener);
          navigate({ doc: d.id });
        }}
      />
    </main>
  );
}
