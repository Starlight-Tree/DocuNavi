import { useEffect, useRef, useState } from 'react';
import { useCollectionCatalog } from '../hooks/useCollections';
import { classificationItems, useClassificationCatalog } from '../hooks/useClassifications';
import type { DocumentList, DocumentSummary } from '../api/documents';
import { collectionLabel } from '../lib/collections';
import { classificationLabel } from '../lib/classifications';
import { EMPTY_FILTER, UNCLASSIFIED_LABEL, facetOptions, filterDocuments, isFiltered, sortDocuments } from '../lib/documentFilter';
import type { DocumentFilter, FilterContext } from '../lib/documentFilter';
import type { Loaded } from '../hooks/useLoaded';
import type { NavigateMode, UrlState } from '../lib/urlState';
import { useUrlState } from '../lib/urlState';
import { isSelectable, setShown, shownState, toggleOne } from '../lib/documentSelection';
import { PAGE_SIZE, paginate } from '../lib/pagination';
import { signalPanelOpen } from '../lib/panelSignal';
import { DocumentFilterBar } from './DocumentFilterBar';
import { DocumentName, UpdatedAt, displayName, statusName } from './documentDisplay';
import { Status } from './Status';

export const MSG_NOT_CONFIGURED = '資料のデータベースが設定されていません。';
export const MSG_NO_DOCUMENTS = '登録された資料はありません。資料を取り込むと、ここに表示され、チャットで検索できます。';
export const MSG_NO_MATCH = '条件を減らすか、クリアしてください。';
export const MSG_NO_MATCH_LEGACY =
  '旧フィルターと、ほかの条件の両方に当てはまる資料はありません。旧フィルターを解除するか、条件を変えてください。';
export const MSG_NOTHING_SELECTED = '回答に使う資料が 1 件も選ばれていないため、質問は送れません。';

/** 回答に使う資料の選択（資料単位）。選択は呼び出し側（チャット）が持つ。一覧の検索・絞り込みでは変わらない。 */
export interface DocumentSelection {
  /** いま回答に使われる資料の ID（検索できる資料だけ）。 */
  selected: ReadonlySet<number>;
  /** チェックを付け外しして、選択が変わった（新しい集合全体）。質問は送らない。 */
  onChange: (next: ReadonlySet<number>) => void;
  /** 「全資料に戻す」（`trigger` は、確認を閉じたあとにフォーカスを戻す先）。 */
  onReset: (trigger: HTMLElement) => void;
  /** すでに全資料が対象（「全資料に戻す」を押せない）。 */
  isAll: boolean;
}

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
  /** 回答に使う資料の選択（チェックボックスと「全資料に戻す」）。渡さなければ、選択の操作は出さない。 */
  selection?: DocumentSelection | undefined;
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
  selection,
  onRetry,
  openId,
  onOpen,
}: DocumentTableProps) {
  const { state: catalogState } = useCollectionCatalog();
  const { targets: targetState, docTypes: docTypeState } = useClassificationCatalog();
  // ページ番号は、検索・絞り込み・並び替えの条件（key）とともに持つ。条件が変わったら 1 ページ目に戻す（選択は変えない）
  const [paging, setPaging] = useState<{ key: string; page: number }>({ key: '', page: 1 });
  const key = JSON.stringify([collection, filter]);
  // 条件が変わると、見える（有効な）ページは 1 に戻る。保存しているページ番号でなく、見えているページを見張る
  const visiblePage = paging.key === key ? paging.page : 1;
  // 表だけがスクロールする（ページ送りは下端に固定）。ページ移動・絞り込みの変更では、表のスクロールを先頭へ戻す（選択は変えない）
  const wrapRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (wrapRef.current) wrapRef.current.scrollTop = 0;
  }, [key, visiblePage]);
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

  // 一覧の検索・絞り込みは表示を絞るだけで、選択は変えない。全選択・解除は、表示している資料だけに効く
  const allDocuments = state.data.documents;
  const pageData = paginate(shown, visiblePage, PAGE_SIZE);
  // 件数と回答対象は 1 行にまとめる。選択の操作は、行のチェックと見出しのチェック（このページ）だけ。「全資料に戻す」は、一部だけ選んでいるときに限って出す
  const searching = isFiltered(filter) || collection !== null;
  const answerCount = selection?.selected.size ?? 0;
  const partial = selection !== undefined && !selection.isAll;
  const summary = (
    <>
      <p role="status" className="docs-summary">
        {searching ? `検索結果 ${shown.length}件` : `${allDocuments.length}件の資料`}
        {/* 初期状態（絞り込みなし・全選択）は、入力欄にも同じ内容が出るので、何も足さない。知らせるのは、一部だけ選んだとき・絞り込み中（選択は変わらない）だけ */}
        {selection && (searching || partial) && ` · ${searching ? `回答対象 ${answerCount}件` : `${answerCount}件を回答対象`}`}
      </p>
      {selection && partial && (
        <button type="button" className="docs-reset" onClick={(event) => selection.onReset(event.currentTarget)}>
          全資料に戻す
        </button>
      )}
    </>
  );
  const goTo = (page: number) => setPaging({ key, page });

  return (
    <>
      <DocumentFilterBar
        filter={filter}
        options={facetOptions(documents, filter, context, statusName)}
        summary={summary}
        onChange={onFilterChange}
        onClear={() => onFilterChange({ ...EMPTY_FILTER, sort: filter.sort })}
      />
      {/* 表の包み。ここだけがスクロールし、列見出しは上に残る。旧フィルターの知らせと「選ばれていない」の警告は、固定の領域に置かず、表と一緒にスクロールさせる */}
      <div className="documents-table-wrap" ref={wrapRef}>
        {collection !== null && (
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
        )}
        {selection && selection.selected.size === 0 && (
          <div className="docs-ask" role="group" aria-label="回答に使う資料">
            <span className="docs-ask-warning" role="status">
              {MSG_NOTHING_SELECTED}
            </span>
          </div>
        )}
        {shown.length === 0 ? (
          <p className="document-filter-empty">{collection !== null ? MSG_NO_MATCH_LEGACY : MSG_NO_MATCH}</p>
        ) : (
          <DocumentRows documents={pageData.rows} showTarget={filter.target === null} openId={openId} onOpen={onOpen} selection={selection} />
        )}
      </div>
      {shown.length > 0 && (
        <nav className="docs-pager" aria-label="ページ移動">
          <span className="docs-pager-range">
            {pageData.from}–{pageData.to} / {pageData.total} 件
          </span>
          <span className="docs-pager-nav">
            <button type="button" className="docs-pager-btn" aria-label="前のページ" disabled={pageData.page <= 1} onClick={() => goTo(pageData.page - 1)}>
              ‹
            </button>
            <span aria-live="polite">
              {pageData.page} / {pageData.pageCount}
            </span>
            <button
              type="button"
              className="docs-pager-btn"
              aria-label="次のページ"
              disabled={pageData.page >= pageData.pageCount}
              onClick={() => goTo(pageData.page + 1)}
            >
              ›
            </button>
          </span>
        </nav>
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
  selection?: DocumentSelection | undefined;
}

/**
 * 資料のテーブル。列は、API が返す情報だけ（資料名・対象・資料種別・ページ数・更新日時）。資料名を押すと、右のパネルに詳細を開く。
 * 取り込み済みの印は出さず、取り込み中・失敗だけを資料名の横に出す。ファイル名は、同じ題名の資料を見分けるときだけ出す（ふだんは詳細のパネルで見る）。
 */
function DocumentRows({ documents, showTarget, openId, onOpen, selection }: DocumentRowsProps) {
  const { targets, docTypes } = useClassificationCatalog();
  const targetItems = classificationItems(targets);
  const docTypeItems = classificationItems(docTypes);
  const names = new Map<string, number>();
  for (const d of documents) names.set(displayName(d), (names.get(displayName(d)) ?? 0) + 1);
  const state = selection ? shownState(documents, selection.selected) : null;
  const allChecked = state !== null && state.selectable > 0 && state.checked === state.selectable;
  return (
    <table className="documents-table">
      <thead>
        <tr>
          {selection && state && (
            <th scope="col" className="col-check">
              <input
                type="checkbox"
                aria-label={`このページの ${state.selectable} 件を全選択・解除`}
                title="このページの資料だけが対象です"
                checked={allChecked}
                disabled={state.selectable === 0}
                ref={(input) => {
                  if (input) input.indeterminate = state.checked > 0 && !allChecked;
                }}
                onChange={() => selection.onChange(setShown(selection.selected, documents, !allChecked))}
              />
            </th>
          )}
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
            {selection && (
              // チェックは資料を開かない（行のクリックに伝えない）
              <td className="col-check" onClick={(event) => event.stopPropagation()}>
                <input
                  type="checkbox"
                  aria-label={`${displayName(document)} を回答に使う`}
                  title={isSelectable(document) ? undefined : '取り込みが終わると選べます'}
                  checked={selection.selected.has(document.id)}
                  disabled={!isSelectable(document)}
                  onChange={() => selection.onChange(toggleOne(selection.selected, document.id))}
                />
              </td>
            )}
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
  );
}

interface DocumentsViewProps {
  /** 資料の一覧（GET /documents）。右のパネルと共有するので、呼び出し側が取る。 */
  state: Loaded<DocumentList>;
  onReload: () => void;
  /** 回答に使う資料の選択。渡さなければ、選択の操作は出さない。 */
  selection?: DocumentSelection | undefined;
}

/**
 * 資料ライブラリ（読み取り専用）。資料を押すと、右のパネルに開く（一覧は残る）。
 * 開いている資料・絞り込み・並び替えは、URL が決める（戻る・再読み込み・共有で戻れる）。
 * 一覧の絞り込み（表示する資料を絞る）と、チャットの回答対象（回答に使う資料の選択）は、別のもの。
 */
export function DocumentsView({ state, onReload, selection }: DocumentsViewProps) {
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
      </div>
      <DocumentTable
        state={state}
        collection={collection}
        onClearCollection={() => navigate({ collection: null })}
        filter={filter}
        onFilterChange={onFilterChange}
        selection={selection}
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
