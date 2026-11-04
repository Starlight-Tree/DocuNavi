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
export const MSG_UNCLASSIFIED_NOT_ASKABLE = '未分類の資料は、回答の対象に選べません';

/** 取り込みの状態の見た目。`ready` と `failed` 以外（取り込み中など）は、待ちの見た目にする。 */
function statusKind(status: string): 'ready' | 'failed' | 'processing' {
  return status === 'ready' ? 'ready' : status === 'failed' ? 'failed' : 'processing';
}

/** 取り込み済みは出さない（全件が取り込み済みの一覧に、同じ印を並べない）。取り込み中・失敗のときだけ、資料名の横に出す。 */
function DocumentStatus({ document }: { document: DocumentSummary }) {
  const kind = statusKind(document.status);
  if (kind === 'ready') return null;
  const glyph = kind === 'failed' ? '!' : '…';
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
  /** 「全○資料を回答に使う」（null は「すべて」）。チャットの回答対象を変える。渡さなければ、ボタンは出さない。 */
  onAskTarget?: ((target: string | null, trigger: HTMLElement) => void) | undefined;
  /** いまのチャットの回答対象が、その対象（null は「すべて」）か。そうならボタンは押せなくする。 */
  isApplied?: ((target: string | null) => boolean) | undefined;
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
  isApplied,
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

  // 回答の対象にできるのは、サーバーが選べると返した対象だけ（一覧に無い識別名・未分類では、「回答に使う」のボタンを出さない）
  const askable = usableClassifications(targetState);
  const askTarget = filter.target !== null && filter.target !== UNCLASSIFIED ? filter.target : null;
  const askKnown = askTarget === null || (askable?.some((t) => t.name === askTarget) ?? false);
  const askInfo = askTarget === null ? undefined : askable?.find((t) => t.name === askTarget);
  // 「全○資料」の数: 対象はサーバーが返す件数、すべては一覧の資料数
  const askCount = askTarget === null ? documents.length : askInfo?.documentCount;
  const applied = isApplied?.(askTarget) ?? false;

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
      />
      {onAskTarget &&
        (filter.target === UNCLASSIFIED ? (
          <div className="docs-ask">
            <span className="docs-ask-label">{MSG_UNCLASSIFIED_NOT_ASKABLE}</span>
          </div>
        ) : (
          askKnown && (
            <div className="docs-ask" role="group" aria-label="回答に使う資料">
              <button
                type="button"
                className="btn btn-primary"
                disabled={applied}
                onClick={(event) => onAskTarget(askTarget, event.currentTarget)}
              >
                {askCount === undefined ? 'この対象を回答に使う' : `全${askCount}資料を回答に使う`}
              </button>
            </div>
          )
        ))}
      {shown.length === 0 ? (
        <p className="document-filter-empty">{collection !== null ? MSG_NO_MATCH_LEGACY : MSG_NO_MATCH}</p>
      ) : (
        <DocumentRows documents={shown} showTarget={filter.target === null} openId={openId} onOpen={onOpen} />
      )}
    </>
  );
}

interface DocumentRowsProps {
  documents: DocumentSummary[];
  /** 対象の列を出すか。対象を 1 つ選んでいる間は、全行が同じ対象なので出さない（対象名は、上の見出しにある）。 */
  showTarget: boolean;
  openId: number | null;
  onOpen: (document: DocumentSummary, opener: Element | null) => void;
}

/**
 * 資料のテーブル。列は、API が返す情報だけ（資料名・対象・資料種別・ページ数・更新日時）。資料名を押すと、右のパネルに詳細を開く。
 * 取り込み済みの印は出さず、取り込み中・失敗だけを資料名の横に出す。ファイル名は、同じ題名の資料を見分けるときだけ出す（ふだんは詳細のパネルで見る）。
 */
function DocumentRows({ documents, showTarget, openId, onOpen }: DocumentRowsProps) {
  const { targets, docTypes } = useClassificationCatalog();
  const targetItems = classificationItems(targets);
  const docTypeItems = classificationItems(docTypes);
  const names = new Map<string, number>();
  for (const d of documents) names.set(displayName(d), (names.get(displayName(d)) ?? 0) + 1);
  return (
    <div className="documents-table-wrap">
      <table className="documents-table">
        <thead>
          <tr>
            <th scope="col">資料名</th>
            {showTarget && <th scope="col">対象</th>}
            <th scope="col">資料種別</th>
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
                  <DocumentName document={document} showFile={(names.get(displayName(document)) ?? 0) > 1} />
                </button>
                <DocumentStatus document={document} />
              </th>
              {showTarget && (
                <td data-target={document.target ?? undefined}>
                  {document.target ? classificationLabel(document.target, targetItems) : <span className="doc-missing">{UNCLASSIFIED_LABEL}</span>}
                </td>
              )}
              <td data-doc-type={document.docType ?? undefined}>
                {document.docType ? classificationLabel(document.docType, docTypeItems) : <span className="doc-missing">{UNCLASSIFIED_LABEL}</span>}
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
  isApplied?: ((target: string | null) => boolean) | undefined;
  /** 「× 閉じる」。直前のチャットへ戻る。 */
  onClose: () => void;
}

/**
 * 資料ライブラリ（読み取り専用）。資料を押すと、右のパネルに開く（一覧は残る）。
 * 開いている資料・絞り込み・並び替えは、URL が決める（戻る・再読み込み・共有で戻れる）。
 * 一覧の絞り込み（表示する資料を絞る）と、チャットの回答対象（回答に使う資料の範囲）は、別のもの。
 */
export function DocumentsView({ state, onReload, onAskTarget, isApplied, onClose }: DocumentsViewProps) {
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
    <main className="documents" aria-label="ナレッジ">
      <div className="docs-head">
        <h2 className="docs-title">ナレッジ</h2>
        <button type="button" className="btn btn-secondary btn-small" onClick={onClose}>
          <span aria-hidden="true">× </span>閉じる
        </button>
      </div>
      <DocumentTable
        state={state}
        collection={collection}
        onClearCollection={() => navigate({ collection: null })}
        filter={filter}
        onFilterChange={onFilterChange}
        onAskTarget={onAskTarget}
        isApplied={isApplied}
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
