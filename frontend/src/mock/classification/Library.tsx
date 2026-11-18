import { useEffect, useId, useState } from 'react';
import { formatDateTime } from '../data';
import { DOCUMENTS, DOC_TYPES, GROUPS, NOW, TARGETS, label } from './data';
import type { DocStatus, MockDocument } from './data';

export const UNCLASSIFIED = '__none__';

export type PageBand = 'one' | 'few' | 'many';
export type UpdatedBand = 'week' | 'month' | 'older';
export type SortKey = 'default' | 'name' | 'updated' | 'pages';

export interface LibraryFilter {
  query: string;
  /** 対象の識別名。`UNCLASSIFIED` は未分類。null は絞らない。 */
  target: string | null;
  docType: string | null;
  status: DocStatus | null;
  pages: PageBand | null;
  updated: UpdatedBand | null;
  sort: SortKey;
}

export const EMPTY_FILTER: LibraryFilter = { query: '', target: null, docType: null, status: null, pages: null, updated: null, sort: 'default' };

const PAGE_LABELS: Record<PageBand, string> = { one: '1ページ', few: '2〜4ページ', many: '5ページ以上' };
const UPDATED_LABELS: Record<UpdatedBand, string> = { week: '7日以内', month: '30日以内', older: 'それ以前' };
const STATUS_LABELS: Record<DocStatus, string> = { ready: '取り込み済み', processing: '取り込み中' };
const SORT_LABELS: Record<SortKey, string> = { default: 'ファイル名順', name: '資料名順', updated: '更新が新しい順', pages: 'ページ数が多い順' };

function pageBand(pages: number): PageBand {
  return pages <= 1 ? 'one' : pages <= 4 ? 'few' : 'many';
}

function updatedBand(iso: string): UpdatedBand {
  const days = (NOW.getTime() - new Date(iso).getTime()) / 86_400_000;
  return days <= 7 ? 'week' : days <= 30 ? 'month' : 'older';
}

type Facet = 'target' | 'docType' | 'status' | 'pages' | 'updated';

/**
 * 条件に合う資料。`legacy`（旧グループ）は、対象・資料種別と AND で重ねる（どれも黙って捨てない・範囲を広げない）。
 * `skip` を渡すと、その条件だけを外す（選択肢ごとの件数に使う）。
 */
export function filterDocuments(
  documents: readonly MockDocument[],
  filter: LibraryFilter,
  legacy: string | null,
  skip?: Facet,
): MockDocument[] {
  const words = filter.query.normalize('NFKC').toLowerCase().split(/\s+/).filter(Boolean);
  return documents.filter((d) => {
    const haystack = [d.title, d.source, label(TARGETS, d.target), label(DOC_TYPES, d.docType)].join(' ').normalize('NFKC').toLowerCase();
    return (
      words.every((w) => haystack.includes(w)) &&
      (legacy === null || d.collection === legacy) &&
      (skip === 'target' || filter.target === null || (filter.target === UNCLASSIFIED ? d.target === null : d.target === filter.target)) &&
      (skip === 'docType' || filter.docType === null || d.docType === filter.docType) &&
      (skip === 'status' || filter.status === null || d.status === filter.status) &&
      (skip === 'pages' || filter.pages === null || pageBand(d.pages) === filter.pages) &&
      (skip === 'updated' || filter.updated === null || updatedBand(d.updatedAt) === filter.updated)
    );
  });
}

function sortDocuments(documents: MockDocument[], sort: SortKey): MockDocument[] {
  const copy = [...documents];
  if (sort === 'name') return copy.sort((a, b) => a.title.localeCompare(b.title, 'ja'));
  if (sort === 'updated') return copy.sort((a, b) => b.updatedAt.localeCompare(a.updatedAt));
  if (sort === 'pages') return copy.sort((a, b) => b.pages - a.pages);
  return copy.sort((a, b) => a.source.localeCompare(b.source));
}

interface Option {
  value: string | null;
  label: string;
  count: number;
}

function count(documents: readonly MockDocument[], filter: LibraryFilter, legacy: string | null, facet: Facet, pick: (d: MockDocument) => string | null) {
  const rest = filterDocuments(documents, filter, legacy, facet);
  const byValue = new Map<string, number>();
  for (const d of rest) {
    const value = pick(d);
    if (value !== null) byValue.set(value, (byValue.get(value) ?? 0) + 1);
  }
  return { total: rest.length, byValue };
}

function Select({ id, name, value, options, onChange }: { id: string; name: string; value: string | null; options: Option[]; onChange: (value: string | null) => void }) {
  return (
    <label className="document-filter-field" htmlFor={id}>
      <span className="document-filter-label">{name}</span>
      <select id={id} value={value ?? ''} onChange={(event) => onChange(event.target.value === '' ? null : event.target.value)}>
        {options.map((o) => (
          <option key={o.value ?? ''} value={o.value ?? ''}>
            {`${o.label} (${o.count})`}
          </option>
        ))}
      </select>
    </label>
  );
}

interface LibraryProps {
  filter: LibraryFilter;
  onFilterChange: (next: Partial<LibraryFilter>) => void;
  /** URL の `?collection=` による旧グループのフィルター（無ければ null）。 */
  legacy: string | null;
  onClearLegacy: () => void;
  /** 質問する（対象の識別名。null は「すべての資料」）。 */
  onAsk: (target: string | null) => void;
  /** チャットの現在の質問対象の表示名（一覧の絞り込みとは別のものだと示す）。 */
  chatTargetLabel: string;
  onOpenDocument: (document: MockDocument) => void;
  openId: number | null;
}

/** 資料ライブラリ。主な操作は 検索・対象・資料種別 の 3 つ。状態・ページ数・更新・並び替えは「詳細フィルター」にまとめる。 */
export function LibraryMock({ filter, onFilterChange, legacy, onClearLegacy, onAsk, chatTargetLabel, onOpenDocument, openId }: LibraryProps) {
  const uid = useId();
  const [detailOpenByUser, setDetailOpenByUser] = useState<boolean | null>(null);
  const [text, setText] = useState(filter.query);
  useEffect(() => setText(filter.query), [filter.query]);

  const detailCount = [filter.status, filter.pages, filter.updated].filter((v) => v !== null).length;
  // 条件が有効なときは開いたままにする（見えない条件で一覧が絞られている状態を作らない）
  const detailOpen = detailCount > 0 ? true : (detailOpenByUser ?? false);

  const shown = sortDocuments(filterDocuments(DOCUMENTS, filter, legacy), filter.sort);
  const targetCounts = count(DOCUMENTS, filter, legacy, 'target', (d) => d.target ?? UNCLASSIFIED);
  const typeCounts = count(DOCUMENTS, filter, legacy, 'docType', (d) => d.docType);
  const statusCounts = count(DOCUMENTS, filter, legacy, 'status', (d) => d.status);
  const pagesCounts = count(DOCUMENTS, filter, legacy, 'pages', (d) => pageBand(d.pages));
  const updatedCounts = count(DOCUMENTS, filter, legacy, 'updated', (d) => updatedBand(d.updatedAt));

  const targetOptions: Option[] = [
    { value: null, label: 'すべて', count: targetCounts.total },
    ...TARGETS.map((t) => ({ value: t.name, label: t.label, count: targetCounts.byValue.get(t.name) ?? 0 })),
    ...(DOCUMENTS.some((d) => d.target === null) ? [{ value: UNCLASSIFIED, label: '未分類', count: targetCounts.byValue.get(UNCLASSIFIED) ?? 0 }] : []),
  ];
  const typeOptions: Option[] = [
    { value: null, label: 'すべて', count: typeCounts.total },
    ...DOC_TYPES.map((t) => ({ value: t.name, label: t.label, count: typeCounts.byValue.get(t.name) ?? 0 })),
  ];
  const filtered = filter.query.trim() !== '' || filter.target !== null || filter.docType !== null || detailCount > 0;
  const askTarget = filter.target !== null && filter.target !== UNCLASSIFIED ? filter.target : null;
  const askLabel = askTarget === null ? 'すべての資料' : label(TARGETS, askTarget);

  return (
    <main className="documents cm-library" aria-label="資料一覧">
      <h2 className="docs-title">資料</h2>

      <section className="document-filter cm-filter" aria-label="資料の検索と絞り込み">
        <label className="document-filter-field document-filter-query" htmlFor={`${uid}-q`}>
          <span className="document-filter-label">資料名で検索</span>
          <input
            id={`${uid}-q`}
            type="search"
            value={text}
            placeholder="資料名・ファイル名・対象"
            onChange={(event) => {
              setText(event.target.value);
              onFilterChange({ query: event.target.value });
            }}
          />
        </label>
        <Select id={`${uid}-target`} name="対象" value={filter.target} options={targetOptions} onChange={(target) => onFilterChange({ target })} />
        <Select id={`${uid}-type`} name="資料種別" value={filter.docType} options={typeOptions} onChange={(docType) => onFilterChange({ docType })} />
        <button
          type="button"
          className="btn btn-secondary cm-detail-toggle"
          aria-expanded={detailOpen}
          aria-controls={`${uid}-detail`}
          disabled={detailCount > 0}
          title={detailCount > 0 ? '条件が有効な間は閉じられません' : undefined}
          onClick={() => setDetailOpenByUser(!detailOpen)}
        >
          詳細フィルター
          {detailCount > 0 && <span className="cm-badge" aria-label={`${detailCount} 件の条件が有効`}>{detailCount}</span>}
          <span aria-hidden="true">{detailOpen ? '▴' : '▾'}</span>
        </button>
        <div id={`${uid}-detail`} className="cm-detail" hidden={!detailOpen}>
          <Select
            id={`${uid}-status`}
            name="状態"
            value={filter.status}
            options={[{ value: null, label: 'すべて', count: statusCounts.total }, ...(['ready', 'processing'] as const).map((s) => ({ value: s, label: STATUS_LABELS[s], count: statusCounts.byValue.get(s) ?? 0 }))]}
            onChange={(status) => onFilterChange({ status: status as DocStatus | null })}
          />
          <Select
            id={`${uid}-pages`}
            name="ページ数"
            value={filter.pages}
            options={[{ value: null, label: 'すべて', count: pagesCounts.total }, ...(['one', 'few', 'many'] as const).map((b) => ({ value: b, label: PAGE_LABELS[b], count: pagesCounts.byValue.get(b) ?? 0 }))]}
            onChange={(pages) => onFilterChange({ pages: pages as PageBand | null })}
          />
          <Select
            id={`${uid}-updated`}
            name="更新"
            value={filter.updated}
            options={[{ value: null, label: 'すべて', count: updatedCounts.total }, ...(['week', 'month', 'older'] as const).map((b) => ({ value: b, label: UPDATED_LABELS[b], count: updatedCounts.byValue.get(b) ?? 0 }))]}
            onChange={(updated) => onFilterChange({ updated: updated as UpdatedBand | null })}
          />
          <label className="document-filter-field" htmlFor={`${uid}-sort`}>
            <span className="document-filter-label">並び替え</span>
            <select id={`${uid}-sort`} value={filter.sort} onChange={(event) => onFilterChange({ sort: event.target.value as SortKey })}>
              {(Object.keys(SORT_LABELS) as SortKey[]).map((key) => (
                <option key={key} value={key}>
                  {SORT_LABELS[key]}
                </option>
              ))}
            </select>
          </label>
        </div>
      </section>

      {legacy !== null && (
        <div className="cm-legacy" role="status">
          <p>
            <strong>旧フィルターが有効です:</strong> 旧グループ「{label(GROUPS, legacy)}」の資料だけを表示しています（URL の <code>collection={legacy}</code>）。
            {filter.target !== null && ' 対象の選択とあわせて、両方に当てはまる資料だけが出ます。'}
          </p>
          <button type="button" className="btn btn-secondary btn-small" onClick={onClearLegacy}>
            旧フィルターを解除
          </button>
        </div>
      )}

      <div className="document-filter-result">
        <p role="status">{shown.length > 0 ? `${shown.length} 件の資料が見つかりました` : '該当する資料が見つかりませんでした'}</p>
        {filtered && (
          <button type="button" className="btn btn-secondary btn-small" onClick={() => onFilterChange({ ...EMPTY_FILTER, sort: filter.sort })}>
            条件をクリア
          </button>
        )}
        <div className="document-filter-trailing">
          <div className="docs-ask" role="group" aria-label="質問する">
            {filter.target === UNCLASSIFIED ? (
              <span className="docs-ask-label">未分類の資料は、質問の対象に選べません</span>
            ) : (
              <>
                <span className="docs-ask-target">
                  <span className="docs-ask-label">質問の対象</span>
                  <strong>{askLabel}</strong>
                </span>
                <button type="button" className="btn btn-secondary" onClick={() => onAsk(askTarget)}>
                  {askTarget === null ? 'すべての資料に質問' : 'この対象について質問'}
                </button>
              </>
            )}
          </div>
        </div>
      </div>
      <p className="docs-ask-note">
        一覧の絞り込みは、チャットの質問対象を変えません。質問するには、右のボタンを押します。 現在のチャットの質問対象: {chatTargetLabel}
      </p>

      {shown.length === 0 ? (
        <p className="document-filter-empty">
          {legacy !== null ? '旧フィルターと、ほかの条件の両方に当てはまる資料はありません。旧フィルターを解除するか、条件を変えてください。' : '条件を減らすか、クリアしてください。'}
        </p>
      ) : (
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
              {shown.map((d) => (
                <tr key={d.id} className="documents-row" aria-current={d.id === openId ? 'true' : undefined} onClick={() => onOpenDocument(d)}>
                  <th scope="row">
                    <button
                      type="button"
                      className="document-open"
                      aria-label={`${d.title} を開く`}
                      onClick={(event) => {
                        event.stopPropagation();
                        onOpenDocument(d);
                      }}
                    >
                      <span className="cm-doc-title">{d.title}</span>
                      <span className="cm-doc-source">{d.source}</span>
                    </button>
                  </th>
                  <td>{d.target ? label(TARGETS, d.target) : <span className="doc-missing">未分類</span>}</td>
                  <td>{d.docType ? label(DOC_TYPES, d.docType) : <span className="doc-missing">未分類</span>}</td>
                  <td>
                    <span className={`ws-status ws-status-${d.status === 'ready' ? 'ready' : 'processing'}`}>
                      <span aria-hidden="true">{d.status === 'ready' ? '✓' : '…'}</span>
                      {STATUS_LABELS[d.status]}
                    </span>
                  </td>
                  <td className="col-num">{d.pages}</td>
                  <td>{formatDateTime(d.updatedAt)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </main>
  );
}
