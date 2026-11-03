import { useCollectionCatalog } from '../hooks/useCollections';
import { classificationItems, useClassificationCatalog, usableClassifications } from '../hooks/useClassifications';
import type { DocumentList, DocumentSummary } from '../api/documents';
import { collectionLabel } from '../lib/collections';
import { classificationLabel } from '../lib/classifications';
import { EMPTY_FILTER, UNCLASSIFIED, UNCLASSIFIED_LABEL, facetOptions, filterDocuments, sortDocuments } from '../lib/documentFilter';
import type { DocumentFilter, FilterContext } from '../lib/documentFilter';
import type { Loaded } from '../hooks/useLoaded';
import type { NavigateMode, UrlState } from '../lib/urlState';
import { useUrlState } from '../lib/urlState';
import { signalPanelOpen } from '../lib/panelSignal';
import { DocumentFilterBar } from './DocumentFilterBar';
import { DocumentName, UpdatedAt, displayName, statusName } from './documentDisplay';
import { Status } from './Status';

export const MSG_NOT_CONFIGURED = '資料のデータベースが設定されていません。';
export const MSG_NO_DOCUMENTS = '登録された資料はありません。資料を取り込むと、ここに表示され、チャットで検索できます。';
export const MSG_NO_MATCH = '条件を減らすか、クリアしてください。';
export const MSG_NO_MATCH_LEGACY =
  '旧フィルターと、ほかの条件の両方に当てはまる資料はありません。旧フィルターを解除するか、条件を変えてください。';
export const MSG_FILTER_IS_NOT_SCOPE = '一覧の絞り込みは、チャットの質問対象を変えません。質問するには、右のボタンを押します。';
export const MSG_UNCLASSIFIED_NOT_ASKABLE = '未分類の資料は、質問の対象に選べません';

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
  /** URL で指定された旧フィルター（資料グループの名前）。対象・資料種別と AND で重ねる。一覧に無い名前でも黙って捨てない。 */
  collection: string | null;
  onClearCollection: () => void;
  /** URL で指定された、一覧の対象・資料種別・検索・絞り込み・並び替え。 */
  filter: DocumentFilter;
  onFilterChange: (next: Partial<DocumentFilter>, mode?: NavigateMode) => void;
  /** 「この対象について質問」「すべての資料に質問」（null は「すべて」）。質問の対象を変える。渡さなければ、ボタンは出さない。 */
  onAskTarget?: ((target: string | null, trigger: HTMLElement) => void) | undefined;
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
  onClearCollection,
  filter,
  onFilterChange,
  onAskTarget,
  chatTargetLabel,
  onRetry,
  openId,
  onOpen,
}: DocumentTableProps) {
  const { state: catalogState } = useCollectionCatalog();
  const { targets: targetState, docTypes: docTypeState } = useClassificationCatalog();
  if (state.status !== 'ready') return <Status state={state} onRetry={onRetry} />;
  if (!state.data.configured) return <p>{MSG_NOT_CONFIGURED}</p>;
  if (state.data.documents.length === 0) return <p>{MSG_NO_DOCUMENTS}</p>;

  const labelGroups = catalogState.status === 'ready' ? catalogState.data.collections : undefined;
  const targetItems = classificationItems(targetState);
  const docTypeItems = classificationItems(docTypeState);
  // 旧フィルターは先に適用する。選択肢の件数も、旧フィルターで絞った資料の中で数える
  const documents = collection === null ? state.data.documents : state.data.documents.filter((d) => d.collection === collection);
  const context: FilterContext = {
    now: new Date(),
    targetLabel: (name) => classificationLabel(name, targetItems),
    docTypeLabel: (name) => classificationLabel(name, docTypeItems),
    ...(targetItems ? { targetOrder: targetItems.map((t) => t.name) } : {}),
    ...(docTypeItems ? { docTypeOrder: docTypeItems.map((t) => t.name) } : {}),
  };
  const shown = sortDocuments(filterDocuments(documents, filter, context), filter.sort);

  // 質問の対象にできるのは、サーバーが選べると返した対象だけ（一覧に無い識別名・未分類では、質問のボタンを出さない）
  const askable = usableClassifications(targetState);
  const askTarget = filter.target !== null && filter.target !== UNCLASSIFIED ? filter.target : null;
  const askKnown = askTarget === null || (askable?.some((t) => t.name === askTarget) ?? false);
  const askInfo = askTarget === null ? undefined : askable?.find((t) => t.name === askTarget);
  const askLabel = askTarget === null ? 'すべての資料' : classificationLabel(askTarget, targetItems);

  return (
    <>
      <DocumentFilterBar
        filter={filter}
        options={facetOptions(documents, filter, context, statusName)}
        resultCount={shown.length}
        onChange={onFilterChange}
        onClear={() => onFilterChange({ ...EMPTY_FILTER, sort: filter.sort })}
        notice={
          collection !== null && (
            <div className="legacy-filter" role="status">
              <p>
                <strong>旧フィルターが有効です:</strong> 旧グループ「{collectionLabel(collection, labelGroups)}」の資料だけを表示しています
                （URL の <code>collection={collection}</code>）。
                {(filter.target !== null || filter.docType !== null) && ' 対象・資料種別の選択とあわせて、すべてに当てはまる資料だけが出ます。'}
              </p>
              <button type="button" className="btn btn-secondary btn-small" onClick={onClearCollection}>
                旧フィルターを解除
              </button>
            </div>
          )
        }
        trailing={
          onAskTarget &&
          (filter.target === UNCLASSIFIED ? (
            <div className="docs-ask">
              <span className="docs-ask-label">{MSG_UNCLASSIFIED_NOT_ASKABLE}</span>
            </div>
          ) : (
            askKnown && (
              <div className="docs-ask" role="group" aria-label="質問する">
                <span className="docs-ask-target" title={askInfo?.description || undefined}>
                  <span className="docs-ask-label">質問の対象</span>
                  <strong>{askLabel}</strong>
                </span>
                <button
                  type="button"
                  className="btn btn-secondary"
                  aria-label={askTarget === null ? 'すべて: すべての資料に質問' : `${askLabel}: この対象について質問`}
                  onClick={(event) => onAskTarget(askTarget, event.currentTarget)}
                >
                  {askTarget === null ? 'すべての資料に質問' : 'この対象について質問'}
                </button>
              </div>
            )
          ))
        }
      />
      {onAskTarget && (
        <p className="docs-ask-note">
          {MSG_FILTER_IS_NOT_SCOPE}
          {chatTargetLabel !== undefined && <span> 現在のチャットの質問対象: {chatTargetLabel}</span>}
        </p>
      )}
      {shown.length === 0 ? (
        <p className="document-filter-empty">{collection !== null ? MSG_NO_MATCH_LEGACY : MSG_NO_MATCH}</p>
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

/** 資料のテーブル。列は、API が返す情報だけ（資料名・対象・資料種別・状態・ページ数・更新日時）。資料名を押すと、右のパネルに詳細を開く。 */
function DocumentRows({ documents, openId, onOpen }: DocumentRowsProps) {
  const { targets, docTypes } = useClassificationCatalog();
  const targetItems = classificationItems(targets);
  const docTypeItems = classificationItems(docTypes);
  return (
    <div className="documents-table-wrap">
      <table className="documents-table">
        <thead>
          <tr>
            <th scope="col">資料名</th>
            <th scope="col">対象</th>
            <th scope="col">資料種別</th>
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
              <td data-target={document.target ?? undefined}>
                {document.target ? classificationLabel(document.target, targetItems) : <span className="doc-missing">{UNCLASSIFIED_LABEL}</span>}
              </td>
              <td data-doc-type={document.docType ?? undefined}>
                {document.docType ? classificationLabel(document.docType, docTypeItems) : <span className="doc-missing">{UNCLASSIFIED_LABEL}</span>}
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
  onAskTarget?: ((target: string | null, trigger: HTMLElement) => void) | undefined;
  chatTargetLabel?: string | undefined;
}

/**
 * 資料ライブラリ（読み取り専用）。資料を押すと、右のパネルに開く（一覧は残る）。
 * 開いている資料・絞り込み・並び替えは、URL が決める（戻る・再読み込み・共有で戻れる）。
 * 一覧の絞り込み（表示する資料を絞る）と、チャットの質問対象（回答に使う資料の範囲）は、別のもの。
 */
export function DocumentsView({ state, onReload, onAskTarget, chatTargetLabel }: DocumentsViewProps) {
  const { doc, target, docType, collection, query, status, pageBand, updatedBand, sort, navigate } = useUrlState();
  const filter: DocumentFilter = {
    query: query ?? '',
    target,
    docType,
    status,
    pages: pageBand,
    updated: updatedBand,
    sort: sort ?? 'default',
  };
  // 画面の条件（DocumentFilter）を、URL の状態（UrlState）に直して書く。空のキーワードと既定の並び順は、URL から外す
  const onFilterChange = (next: Partial<DocumentFilter>, mode?: NavigateMode) => {
    const patch: Partial<UrlState> = {};
    if (next.query !== undefined) patch.query = next.query.trim() === '' ? null : next.query;
    if (next.target !== undefined) patch.target = next.target;
    if (next.docType !== undefined) patch.docType = next.docType;
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
        onClearCollection={() => navigate({ collection: null })}
        filter={filter}
        onFilterChange={onFilterChange}
        onAskTarget={onAskTarget}
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
