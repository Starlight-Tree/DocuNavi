/**
 * 資料一覧の検索・絞り込み・並び替え。React にも通信にも依存しない（`GET /documents` の結果を、画面の中だけで絞る）。
 *
 * - キーワード: 文書名・ファイル名・対象・資料種別の表示名が対象。空白（全角も）で区切り、すべての語を含む資料に絞る（AND）。
 *   全角と半角・大文字と小文字は区別しない（NFKC に揃えて比べる）。本文は探さない（本文の検索は API が要る）。
 * - 絞り込み: 対象・資料種別・状態・ページ数の帯・更新の新しさ（対象・資料種別は `UNCLASSIFIED` で未分類）。選択肢ごとの件数は、「その選択肢以外の条件」で絞った件数にする
 *   （選ぶと何件になるかが分かる）。
 * - 並び替え: `default` は入力の順のまま（サーバーはファイル名順で返す）。
 */
import type { DocumentSummary } from '../api/documents';

export const PAGE_BANDS = ['one', 'few', 'many'] as const;
export type PageBand = (typeof PAGE_BANDS)[number];

export const UPDATED_BANDS = ['week', 'month', 'older'] as const;
export type UpdatedBand = (typeof UPDATED_BANDS)[number];

/**
 * 並び順。表の列名（資料名・対象・種類・ページ数・取り込み日）を押して切り替える。`default` はサーバーの順（ファイル名順）。
 * `name` `target` `type` `updated` `pages` は、その列で最初に押したときの向き（名前・対象は表示名の五十音、種類は一覧の固定の順、更新は新しい順、ページ数は多い順）。
 */
export const SORT_KEYS = ['default', 'name', 'name_desc', 'target', 'target_desc', 'type', 'type_desc', 'updated', 'updated_asc', 'pages', 'pages_asc'] as const;
export type SortKey = (typeof SORT_KEYS)[number];

export type SortColumn = 'name' | 'target' | 'type' | 'updated' | 'pages';

const SORT_CYCLE: Record<SortColumn, readonly [SortKey, SortKey]> = {
  name: ['name', 'name_desc'],
  target: ['target', 'target_desc'],
  type: ['type', 'type_desc'],
  updated: ['updated', 'updated_asc'],
  pages: ['pages', 'pages_asc'],
};

/** 列名を押したときの次の並び順。最初の向き → 逆向き → サーバーの順（既定）。別の列を押したら、その列の最初の向き。 */
export function nextSort(current: SortKey, column: SortColumn): SortKey {
  const [first, second] = SORT_CYCLE[column];
  if (current === first) return second;
  if (current === second) return 'default';
  return first;
}

/** 並べている列と、向き（`aria-sort` の値）。既定のときは null。 */
export function sortState(sort: SortKey): { column: SortColumn; direction: 'ascending' | 'descending' } | null {
  switch (sort) {
    case 'name':
      return { column: 'name', direction: 'ascending' };
    case 'name_desc':
      return { column: 'name', direction: 'descending' };
    case 'target':
      return { column: 'target', direction: 'ascending' };
    case 'target_desc':
      return { column: 'target', direction: 'descending' };
    case 'type':
      return { column: 'type', direction: 'ascending' };
    case 'type_desc':
      return { column: 'type', direction: 'descending' };
    case 'updated':
      return { column: 'updated', direction: 'descending' };
    case 'updated_asc':
      return { column: 'updated', direction: 'ascending' };
    case 'pages':
      return { column: 'pages', direction: 'descending' };
    case 'pages_asc':
      return { column: 'pages', direction: 'ascending' };
    default:
      return null;
  }
}

/** 対象・資料種別の「未分類」（資料の target / doc_type が NULL）を表す値。URL にも載る。識別名の形（先頭は英数字）とは重ならない。 */
export const UNCLASSIFIED = '_none';

/** キーワードの最大の長さ（URL に載るので、長さを絞る）。 */
export const QUERY_MAX_LENGTH = 100;

const DAY_MS = 24 * 60 * 60 * 1000;
const WEEK_DAYS = 7;
const MONTH_DAYS = 30;

export interface DocumentFilter {
  /** 入力されたままのキーワード。空白だけ・空は「絞らない」。 */
  query: string;
  /** 対象の識別名（`UNCLASSIFIED` は未分類）。null は絞らない。 */
  target: string | null;
  /** 資料種別の識別名（`UNCLASSIFIED` は未分類）。null は絞らない。 */
  docType: string | null;
  /** 状態（`ready` など）。null は絞らない。 */
  status: string | null;
  pages: PageBand | null;
  updated: UpdatedBand | null;
  sort: SortKey;
}

export const EMPTY_FILTER: DocumentFilter = {
  query: '',
  target: null,
  docType: null,
  status: null,
  pages: null,
  updated: null,
  sort: 'default',
};

export interface FilterContext {
  /** 「7 日以内」などの基準にする、いまの時刻。 */
  now: Date;
  /** 対象の表示名。 */
  targetLabel: (target: string) => string;
  /** 資料種別の表示名。 */
  docTypeLabel: (docType: string) => string;
  /** 選択肢の並び順（識別名の列。一覧の並び順）。載っていない識別名は、その後ろに名前順で並べる。 */
  targetOrder?: readonly string[];
  docTypeOrder?: readonly string[];
}

/** 比べやすい形（NFKC・小文字）にする。全角の英数字と半角、全角の空白と半角の空白が同じになる。 */
function fold(text: string): string {
  return text.normalize('NFKC').toLowerCase();
}

/** キーワードを、空白（全角を含む）で語に分ける。空の語は捨てる。 */
export function splitKeywords(query: string): string[] {
  return fold(query)
    .split(/\s+/)
    .filter((word) => word !== '');
}

export function pageBand(pages: number | null): PageBand | null {
  if (pages === null) return null;
  if (pages <= 1) return 'one';
  return pages <= 4 ? 'few' : 'many';
}

/** 更新が、いまから見てどの帯か。日付が無い・読めないときは null（帯を選ぶと除かれる）。未来の日付は「7 日以内」に含める。 */
export function updatedBand(updatedAt: string | null, now: Date): UpdatedBand | null {
  if (updatedAt === null) return null;
  const time = new Date(updatedAt).getTime();
  if (Number.isNaN(time)) return null;
  const days = (now.getTime() - time) / DAY_MS;
  if (days <= WEEK_DAYS) return 'week';
  return days <= MONTH_DAYS ? 'month' : 'older';
}

function matchesKeywords(document: DocumentSummary, words: string[], context: FilterContext): boolean {
  if (words.length === 0) return true;
  const target = document.target === null ? '' : context.targetLabel(document.target);
  const docType = document.docType === null ? '' : context.docTypeLabel(document.docType);
  const haystack = fold([document.title ?? '', document.source, target, docType].join(' '));
  return words.every((word) => haystack.includes(word));
}

export type FilterFacet = 'target' | 'docType' | 'status' | 'pages' | 'updated';

/** 資料の分類（NULL は未分類）が、選ばれた値に当てはまるか。 */
function matchesClassification(value: string | null, selected: string | null): boolean {
  if (selected === null) return true;
  return selected === UNCLASSIFIED ? value === null : value === selected;
}

/**
 * 条件に合う資料に絞る（入力の順は変えない）。`skip` を渡すと、その条件だけを外す（選択肢ごとの件数に使う）。
 */
export function filterDocuments(
  documents: readonly DocumentSummary[],
  filter: DocumentFilter,
  context: FilterContext,
  skip?: FilterFacet,
): DocumentSummary[] {
  const words = splitKeywords(filter.query);
  return documents.filter(
    (d) =>
      matchesKeywords(d, words, context) &&
      (skip === 'target' || matchesClassification(d.target, filter.target)) &&
      (skip === 'docType' || matchesClassification(d.docType, filter.docType)) &&
      (skip === 'status' || filter.status === null || d.status === filter.status) &&
      (skip === 'pages' || filter.pages === null || pageBand(d.pages) === filter.pages) &&
      (skip === 'updated' || filter.updated === null || updatedBand(d.updatedAt, context.now) === filter.updated),
  );
}

function displayKey(document: DocumentSummary): string {
  return document.title ?? document.source;
}

/**
 * 並び替えた写しを返す。`default` は入力の順のまま。同じ値は入力の順を保つ。
 * 更新日・ページ数が無い資料と、未分類の資料は、どちらの向きでも最後に置く。
 * 対象は表示名の五十音、種類は一覧（`context.docTypeOrder`）の固定の順。一覧が無いときは、入力の順。
 */
export function sortDocuments(documents: readonly DocumentSummary[], sort: SortKey, context?: Pick<FilterContext, 'targetLabel' | 'docTypeOrder'>): DocumentSummary[] {
  const copy = [...documents];
  const byName = (a: DocumentSummary, b: DocumentSummary) => displayKey(a).localeCompare(displayKey(b), 'ja');
  // 値が無い資料は、向きに関係なく最後
  const byNumber = (value: (d: DocumentSummary) => number | null, descending: boolean) => (a: DocumentSummary, b: DocumentSummary) => {
    const x = value(a);
    const y = value(b);
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;
    return descending ? y - x : x - y;
  };
  const updated = (d: DocumentSummary) => {
    const time = timeOf(d.updatedAt);
    return Number.isFinite(time) ? time : null;
  };
  const pages = (d: DocumentSummary) => d.pages ?? null;
  // 未分類（null）は、向きに関係なく最後。同じ値は入力の順
  const byClassification = (key: (d: DocumentSummary) => string | number | null, descending: boolean) => (a: DocumentSummary, b: DocumentSummary) => {
    const x = key(a);
    const y = key(b);
    if (x === null || y === null) return x === y ? 0 : x === null ? 1 : -1;
    const order = typeof x === 'number' && typeof y === 'number' ? x - y : String(x).localeCompare(String(y), 'ja');
    return descending ? -order : order;
  };
  const targetKey = (d: DocumentSummary) => (d.target === null ? null : (context?.targetLabel(d.target) ?? d.target));
  const typeKey = (d: DocumentSummary) => {
    if (d.docType === null) return null;
    const position = context?.docTypeOrder?.indexOf(d.docType) ?? -1;
    return position >= 0 ? position : Number.MAX_SAFE_INTEGER; // 一覧に無い種類は、一覧の後ろ
  };
  switch (sort) {
    case 'target':
      return copy.sort(byClassification(targetKey, false));
    case 'target_desc':
      return copy.sort(byClassification(targetKey, true));
    case 'type':
      return copy.sort(byClassification(typeKey, false));
    case 'type_desc':
      return copy.sort(byClassification(typeKey, true));
    case 'name':
      return copy.sort(byName);
    case 'name_desc':
      return copy.sort((a, b) => byName(b, a));
    case 'pages':
      return copy.sort(byNumber(pages, true));
    case 'pages_asc':
      return copy.sort(byNumber(pages, false));
    case 'updated':
      return copy.sort(byNumber(updated, true));
    case 'updated_asc':
      return copy.sort(byNumber(updated, false));
    default:
      return copy;
  }
}

function timeOf(value: string | null): number {
  if (value === null) return Number.NEGATIVE_INFINITY;
  const time = new Date(value).getTime();
  return Number.isNaN(time) ? Number.NEGATIVE_INFINITY : time;
}

/** 資料にある状態を、`ready` を先頭に、残りを名前順で返す（重複なし）。 */
export function statusValues(documents: readonly DocumentSummary[]): string[] {
  const values = [...new Set(documents.map((d) => d.status))];
  return values.sort((a, b) => (a === 'ready' ? -1 : b === 'ready' ? 1 : a.localeCompare(b)));
}

/** 条件で絞り込んでいるか（並び替えは数えない）。 */
export function isFiltered(filter: DocumentFilter): boolean {
  return (
    splitKeywords(filter.query).length > 0 ||
    filter.target !== null ||
    filter.docType !== null ||
    detailCount(filter) > 0
  );
}

/** 「フィルター」の中（状態・ページ数・更新）で、有効な条件の数。 */
export function detailCount(filter: DocumentFilter): number {
  return [filter.status, filter.pages, filter.updated].filter((v) => v !== null).length;
}

/** 選択肢ごとの件数: その条件以外で絞った資料のうち、選択肢に当てはまる数。 */
export function facetCounts(
  documents: readonly DocumentSummary[],
  filter: DocumentFilter,
  context: FilterContext,
  facet: FilterFacet,
): { total: number; byValue: Map<string, number> } {
  const rest = filterDocuments(documents, filter, context, facet);
  const byValue = new Map<string, number>();
  for (const d of rest) {
    const value =
      facet === 'target'
        ? (d.target ?? UNCLASSIFIED)
        : facet === 'docType'
          ? (d.docType ?? UNCLASSIFIED)
          : facet === 'status'
            ? d.status
            : facet === 'pages'
              ? pageBand(d.pages)
              : updatedBand(d.updatedAt, context.now);
    if (value !== null) byValue.set(value, (byValue.get(value) ?? 0) + 1);
  }
  return { total: rest.length, byValue };
}

export const UNCLASSIFIED_LABEL = '未分類';
export const PAGE_BAND_LABELS: Record<PageBand, string> = { one: '1ページ', few: '2〜4ページ', many: '5ページ以上' };
export const UPDATED_BAND_LABELS: Record<UpdatedBand, string> = { week: '7日以内', month: '30日以内', older: 'それ以前' };

/** 絞り込みの選択肢。`value` が null のものは「すべて」。 */
export interface FacetOption<T extends string = string> {
  value: T | null;
  label: string;
  count: number;
}

function options<T extends string>(
  values: readonly T[],
  labelOf: (value: T) => string,
  counts: { total: number; byValue: Map<string, number> },
): FacetOption<T>[] {
  return [
    { value: null, label: 'すべて', count: counts.total },
    ...values.map((value) => ({ value, label: labelOf(value), count: counts.byValue.get(value) ?? 0 })),
  ];
}

/** 資料にある分類の識別名を、`order` の順（無いものは後ろに名前順）で返す。未分類の資料があれば、末尾に `UNCLASSIFIED`。選ばれた値が資料に無くても残す。 */
function classificationValues(
  values: ReadonlyArray<string | null>,
  order: readonly string[] | undefined,
  selected: string | null,
): string[] {
  const named = [...new Set(values.filter((v): v is string => v !== null))];
  if (selected !== null && selected !== UNCLASSIFIED && !named.includes(selected)) named.push(selected);
  const rank = (name: string) => {
    const index = order?.indexOf(name) ?? -1;
    return index === -1 ? Number.MAX_SAFE_INTEGER : index;
  };
  named.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
  if (values.includes(null) || selected === UNCLASSIFIED) named.push(UNCLASSIFIED);
  return named;
}

/**
 * 対象・資料種別・状態・ページ数・更新の選択肢と、それぞれの件数。
 * 状態の選択肢は、資料にある状態だけ（URL で指定された、資料に無い状態は、件数 0 の選択肢として残す。選択が見えなくならないように）。
 */
export function facetOptions(
  documents: readonly DocumentSummary[],
  filter: DocumentFilter,
  context: FilterContext,
  statusLabel: (status: string) => string,
): {
  target: FacetOption[];
  docType: FacetOption[];
  status: FacetOption[];
  pages: FacetOption<PageBand>[];
  updated: FacetOption<UpdatedBand>[];
} {
  const targets = classificationValues(documents.map((d) => d.target), context.targetOrder, filter.target);
  const docTypes = classificationValues(documents.map((d) => d.docType), context.docTypeOrder, filter.docType);
  const statuses = statusValues(documents);
  if (filter.status !== null && !statuses.includes(filter.status)) statuses.push(filter.status);
  return {
    target: options(targets, (v) => (v === UNCLASSIFIED ? UNCLASSIFIED_LABEL : context.targetLabel(v)), facetCounts(documents, filter, context, 'target')),
    docType: options(docTypes, (v) => (v === UNCLASSIFIED ? UNCLASSIFIED_LABEL : context.docTypeLabel(v)), facetCounts(documents, filter, context, 'docType')),
    status: options(statuses, statusLabel, facetCounts(documents, filter, context, 'status')),
    pages: options(PAGE_BANDS, (b) => PAGE_BAND_LABELS[b], facetCounts(documents, filter, context, 'pages')),
    updated: options(UPDATED_BANDS, (b) => UPDATED_BAND_LABELS[b], facetCounts(documents, filter, context, 'updated')),
  };
}
