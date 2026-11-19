import { describe, expect, it } from 'vitest';
import type { DocumentSummary } from '../api/documents';
import {
  EMPTY_FILTER,
  UNCLASSIFIED,
  detailCount,
  facetCounts,
  facetOptions,
  filterDocuments,
  isFiltered,
  pageBand,
  nextSort,
  sortDocuments,
  sortState,
  splitKeywords,
  statusValues,
  updatedBand,
} from './documentFilter';
import type { DocumentFilter, FilterContext } from './documentFilter';

const NOW = new Date('2026-11-01T00:00:00Z');
const TARGET_LABELS: Record<string, string> = {
  zahard: 'ザーハード',
  'minato-coffee': 'みなとコーヒー',
  'tsubasa-dorm': 'つばさ大学学生寮',
  'aoba-aw820': 'あおば電機 AW-820',
};
const TYPE_LABELS: Record<string, string> = { setting: '設定資料', 'store-guide': '店舗案内', regulation: '規程', manual: '取扱説明書' };
const CONTEXT: FilterContext = {
  now: NOW,
  targetLabel: (name) => TARGET_LABELS[name] ?? name,
  docTypeLabel: (name) => TYPE_LABELS[name] ?? name,
  targetOrder: ['zahard', 'minato-coffee', 'tsubasa-dorm', 'aoba-aw820'],
  docTypeOrder: ['setting', 'store-guide', 'regulation', 'manual'],
};

function daysAgo(days: number): string {
  return new Date(NOW.getTime() - days * 24 * 60 * 60 * 1000).toISOString();
}

function doc(id: number, source: string, extra: Partial<DocumentSummary> = {}): DocumentSummary {
  return {
    id,
    source,
    title: null,
    collection: null,
    target: null,
    docType: null,
    revision: null,
    status: 'ready',
    failureReason: null,
    pages: 3,
    updatedAt: daysAgo(10),
    chunkCount: 5,
    embeddedCount: 5,
    heroImage: false,
    ...extra,
  };
}

// サーバーの順（ファイル名順）
const DOCS = [
  doc(1, 'aoba_aw820_manual.pdf', { collection: 'guides', target: 'aoba-aw820', docType: 'manual', pages: 4, chunkCount: 6, updatedAt: daysAgo(32) }),
  doc(2, 'cafe_store_guide.pdf', { collection: 'cafe', target: 'minato-coffee', docType: 'store-guide', pages: 4, chunkCount: 5, updatedAt: daysAgo(14) }),
  doc(3, 'tsubasa_dorm_rules.pdf', { title: 'つばさ大学学生寮 入寮規程', collection: 'guides', target: 'tsubasa-dorm', docType: 'regulation', pages: 3, chunkCount: 3, updatedAt: daysAgo(14) }),
  doc(4, 'ザーハード1.pdf', { collection: 'zahard', target: 'zahard', docType: 'setting', pages: 1, chunkCount: 2, updatedAt: daysAgo(10) }),
  doc(5, 'ザーハード2.pdf', { collection: 'zahard', target: 'zahard', docType: 'setting', pages: 1, chunkCount: 0, status: 'failed', updatedAt: daysAgo(1) }),
  doc(6, 'loose.pdf', { collection: null, pages: null, updatedAt: null }),
];

const ids = (list: DocumentSummary[]) => list.map((d) => d.id);
const withFilter = (patch: Partial<DocumentFilter>): DocumentFilter => ({ ...EMPTY_FILTER, ...patch });

describe('splitKeywords', () => {
  it('半角・全角の空白で分け、空の語は捨てる', () => {
    expect(splitKeywords('入寮 規程')).toEqual(['入寮', '規程']);
    expect(splitKeywords('入寮　規程')).toEqual(['入寮', '規程']);
    expect(splitKeywords('  入寮 \t 規程  ')).toEqual(['入寮', '規程']);
    expect(splitKeywords('')).toEqual([]);
    expect(splitKeywords(' 　 ')).toEqual([]);
  });

  it('全角の英数字と大文字は、半角・小文字に揃える', () => {
    expect(splitKeywords('ＣＡＦＥ　Ｇｕｉｄｅ１')).toEqual(['cafe', 'guide1']);
  });
});

describe('pageBand / updatedBand', () => {
  it('ページ数の帯: 1 / 2〜4 / 5 以上。ページ数が無ければ帯なし', () => {
    expect([1, 2, 4, 5, 100].map(pageBand)).toEqual(['one', 'few', 'few', 'many', 'many']);
    expect(pageBand(null)).toBeNull();
  });

  it('更新の帯: 7 日以内 / 30 日以内 / それ以前。境界は含める。未来は 7 日以内。日付が無い・読めなければ帯なし', () => {
    expect(updatedBand(daysAgo(0), NOW)).toBe('week');
    expect(updatedBand(daysAgo(7), NOW)).toBe('week');
    expect(updatedBand(new Date(NOW.getTime() - (7 * 24 * 60 * 60 + 1) * 1000).toISOString(), NOW)).toBe('month');
    expect(updatedBand(daysAgo(30), NOW)).toBe('month');
    expect(updatedBand(daysAgo(31), NOW)).toBe('older');
    expect(updatedBand(daysAgo(-3), NOW)).toBe('week');
    expect(updatedBand(null, NOW)).toBeNull();
    expect(updatedBand('not a date', NOW)).toBeNull();
  });
});

describe('filterDocuments', () => {
  it('条件が無ければ、全件を入力の順で返す', () => {
    expect(ids(filterDocuments(DOCS, EMPTY_FILTER, CONTEXT))).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it('キーワードは、題名・ファイル名・対象と資料種別の表示名を探す', () => {
    expect(ids(filterDocuments(DOCS, withFilter({ query: '入寮' }), CONTEXT))).toEqual([3]); // 題名
    expect(ids(filterDocuments(DOCS, withFilter({ query: 'cafe_store' }), CONTEXT))).toEqual([2]); // ファイル名
    expect(ids(filterDocuments(DOCS, withFilter({ query: '店舗案内' }), CONTEXT))).toEqual([2]); // 資料種別の表示名（識別名の store-guide ではない）
    expect(ids(filterDocuments(DOCS, withFilter({ query: '設定資料' }), CONTEXT))).toEqual([4, 5]);
    expect(ids(filterDocuments(DOCS, withFilter({ query: 'みなと' }), CONTEXT))).toEqual([2]); // 対象の表示名
    expect(ids(filterDocuments(DOCS, withFilter({ query: 'guides' }), CONTEXT))).toEqual([]); // 旧グループの名前は探さない
  });

  it('語は AND。全角・半角、大文字・小文字は区別しない', () => {
    expect(ids(filterDocuments(DOCS, withFilter({ query: '規程 つばさ' }), CONTEXT))).toEqual([3]);
    expect(ids(filterDocuments(DOCS, withFilter({ query: '取扱　あおば' }), CONTEXT))).toEqual([1]); // 資料種別「取扱説明書」と対象「あおば電機」
    expect(ids(filterDocuments(DOCS, withFilter({ query: '規程 cafe' }), CONTEXT))).toEqual([]);
    expect(ids(filterDocuments(DOCS, withFilter({ query: 'ＡＯＢＡ' }), CONTEXT))).toEqual([1]);
    expect(ids(filterDocuments(DOCS, withFilter({ query: 'AOBA_AW820' }), CONTEXT))).toEqual([1]);
  });

  it('未分類の資料は、分類の語では見つからない（「null」などの文字を探さない）', () => {
    expect(ids(filterDocuments(DOCS, withFilter({ query: 'null' }), CONTEXT))).toEqual([]);
    expect(ids(filterDocuments(DOCS, withFilter({ query: 'loose' }), CONTEXT))).toEqual([6]);
  });

  it('状態・ページ数・更新で絞る。ページ数や日付が無い資料は、帯を選ぶと除かれる', () => {
    expect(ids(filterDocuments(DOCS, withFilter({ status: 'failed' }), CONTEXT))).toEqual([5]);
    expect(ids(filterDocuments(DOCS, withFilter({ pages: 'one' }), CONTEXT))).toEqual([4, 5]);
    expect(ids(filterDocuments(DOCS, withFilter({ pages: 'few' }), CONTEXT))).toEqual([1, 2, 3]);
    expect(ids(filterDocuments(DOCS, withFilter({ pages: 'many' }), CONTEXT))).toEqual([]);
    expect(ids(filterDocuments(DOCS, withFilter({ updated: 'week' }), CONTEXT))).toEqual([5]);
    expect(ids(filterDocuments(DOCS, withFilter({ updated: 'month' }), CONTEXT))).toEqual([2, 3, 4]);
    expect(ids(filterDocuments(DOCS, withFilter({ updated: 'older' }), CONTEXT))).toEqual([1]);
  });

  it('対象・資料種別で絞る。UNCLASSIFIED は、未分類（NULL）の資料だけ', () => {
    expect(ids(filterDocuments(DOCS, withFilter({ target: 'zahard' }), CONTEXT))).toEqual([4, 5]);
    expect(ids(filterDocuments(DOCS, withFilter({ docType: 'regulation' }), CONTEXT))).toEqual([3]);
    expect(ids(filterDocuments(DOCS, withFilter({ target: UNCLASSIFIED }), CONTEXT))).toEqual([6]);
    expect(ids(filterDocuments(DOCS, withFilter({ docType: UNCLASSIFIED }), CONTEXT))).toEqual([6]);
    expect(ids(filterDocuments(DOCS, withFilter({ target: 'nothing' }), CONTEXT))).toEqual([]); // 一覧に無い対象は 0 件（全件に戻さない）
    expect(ids(filterDocuments(DOCS, withFilter({ target: 'zahard', docType: 'manual' }), CONTEXT))).toEqual([]);
  });

  it('条件は重ねて AND になる', () => {
    expect(ids(filterDocuments(DOCS, withFilter({ query: 'ザーハード', status: 'ready' }), CONTEXT))).toEqual([4]);
    expect(ids(filterDocuments(DOCS, withFilter({ pages: 'few', updated: 'month' }), CONTEXT))).toEqual([2, 3]);
  });

  it('skip を渡すと、その条件だけを外す', () => {
    const filter = withFilter({ status: 'failed', pages: 'few' });
    expect(ids(filterDocuments(DOCS, withFilter({ target: 'zahard' }), CONTEXT, 'target'))).toEqual([1, 2, 3, 4, 5, 6]);
    expect(ids(filterDocuments(DOCS, withFilter({ docType: 'manual' }), CONTEXT, 'docType'))).toEqual([1, 2, 3, 4, 5, 6]);
    expect(ids(filterDocuments(DOCS, filter, CONTEXT))).toEqual([]);
    expect(ids(filterDocuments(DOCS, filter, CONTEXT, 'status'))).toEqual([1, 2, 3]);
    expect(ids(filterDocuments(DOCS, filter, CONTEXT, 'pages'))).toEqual([5]);
  });
});

describe('sortDocuments', () => {
  it('default は入力の順のまま。入力は変えない', () => {
    const input = [...DOCS];
    expect(ids(sortDocuments(input, 'default'))).toEqual([1, 2, 3, 4, 5, 6]);
    expect(sortDocuments(input, 'default')).not.toBe(input);
    expect(ids(input)).toEqual([1, 2, 3, 4, 5, 6]);
  });

  it('name は、題名があれば題名、無ければファイル名で、日本語の順に並べる', () => {
    // aoba / cafe / loose（ラテン文字）→ ザーハード 1・2（さ）→ つばさ大学…（題名。つ）。かなは五十音順（ひらがなとカタカナの区別なし）
    expect(ids(sortDocuments(DOCS, 'name'))).toEqual([1, 2, 6, 4, 5, 3]);
  });

  it('updated は新しい順。日付が無い資料は最後。同じ日付は入力の順を保つ', () => {
    expect(ids(sortDocuments(DOCS, 'updated'))).toEqual([5, 4, 2, 3, 1, 6]);
  });

  it('pages は多い順。ページ数が無い資料は最後', () => {
    expect(ids(sortDocuments(DOCS, 'pages'))).toEqual([1, 2, 3, 4, 5, 6]); // 4,4,3,1,1,null（同じ値は入力の順）
  });

  it('逆向き（name_desc・updated_asc・pages_asc）。値が無い資料は、逆向きでも最後', () => {
    expect(ids(sortDocuments(DOCS, 'name_desc'))).toEqual([3, 5, 4, 6, 2, 1]);
    expect(ids(sortDocuments(DOCS, 'pages_asc'))).toEqual([4, 5, 3, 1, 2, 6]); // 1,1,3,4,4,null
    expect(ids(sortDocuments(DOCS, 'updated_asc')).at(-1)).toBe(6); // 日付が無い資料は最後
    expect(ids(sortDocuments(DOCS, 'updated_asc'))).toEqual([1, 2, 3, 4, 5, 6]); // 古い順。同じ日付の 2 と 3 は入力の順
  });
});

describe('nextSort / sortState', () => {
  it('列名を押すと「最初の向き → 逆向き → 既定」。別の列を押したら、その列の最初の向き', () => {
    expect(nextSort('default', 'name')).toBe('name');
    expect(nextSort('name', 'name')).toBe('name_desc');
    expect(nextSort('name_desc', 'name')).toBe('default');
    expect(nextSort('default', 'updated')).toBe('updated'); // 更新は新しい順から
    expect(nextSort('updated', 'updated')).toBe('updated_asc');
    expect(nextSort('pages_asc', 'pages')).toBe('default');
    expect(nextSort('name_desc', 'pages')).toBe('pages');
  });

  it('並べている列と向きを返す。既定は null', () => {
    expect(sortState('default')).toBeNull();
    expect(sortState('name')).toEqual({ column: 'name', direction: 'ascending' });
    expect(sortState('updated')).toEqual({ column: 'updated', direction: 'descending' });
    expect(sortState('pages_asc')).toEqual({ column: 'pages', direction: 'ascending' });
  });
});

describe('statusValues / isFiltered', () => {
  it('状態は、ready を先頭に、残りを名前順で、重複なしに返す', () => {
    expect(statusValues(DOCS)).toEqual(['ready', 'failed']);
    expect(statusValues([doc(1, 'a.pdf', { status: 'processing' }), doc(2, 'b.pdf', { status: 'failed' }), doc(3, 'c.pdf')])).toEqual([
      'ready',
      'failed',
      'processing',
    ]);
    expect(statusValues([])).toEqual([]);
  });

  it('絞り込みの有無は、並び替えと、空白だけのキーワードを数えない', () => {
    expect(isFiltered(EMPTY_FILTER)).toBe(false);
    expect(isFiltered(withFilter({ sort: 'pages' }))).toBe(false);
    expect(isFiltered(withFilter({ query: ' 　 ' }))).toBe(false);
    expect(isFiltered(withFilter({ query: 'a' }))).toBe(true);
    expect(isFiltered(withFilter({ target: 'zahard' }))).toBe(true);
    expect(isFiltered(withFilter({ docType: UNCLASSIFIED }))).toBe(true);
    expect(isFiltered(withFilter({ status: 'ready' }))).toBe(true);
    expect(isFiltered(withFilter({ pages: 'one' }))).toBe(true);
    expect(isFiltered(withFilter({ updated: 'week' }))).toBe(true);
  });
});

describe('detailCount', () => {
  it('詳細フィルター（状態・ページ数・更新）の有効な条件だけを数える。対象・資料種別・キーワード・並び替えは数えない', () => {
    expect(detailCount(EMPTY_FILTER)).toBe(0);
    expect(detailCount(withFilter({ target: 'zahard', docType: 'setting', query: 'a', sort: 'pages' }))).toBe(0);
    expect(detailCount(withFilter({ status: 'ready', pages: 'one', updated: 'week' }))).toBe(3);
  });
});

describe('facetCounts / facetOptions', () => {
  it('対象・資料種別の選択肢は、一覧の順に並べ、未分類の資料があれば末尾に「未分類」を足す。件数は、その条件以外で絞った数', () => {
    const options = facetOptions(DOCS, withFilter({ target: 'zahard' }), CONTEXT, (s) => s);
    // 資料種別の件数は、対象の条件（zahard）を残す: 設定資料 2 件。未分類（loose）は対象 zahard ではないので 0
    expect(options.docType.map((o) => [o.value, o.label, o.count])).toEqual([
      [null, 'すべて', 2],
      ['setting', '設定資料', 2],
      ['store-guide', '店舗案内', 0],
      ['regulation', '規程', 0],
      ['manual', '取扱説明書', 0],
      [UNCLASSIFIED, '未分類', 0],
    ]);
    // 対象の件数は、対象の条件を外す: 全件
    expect(options.target.map((o) => [o.value, o.count])).toEqual([
      [null, 6],
      ['zahard', 2],
      ['minato-coffee', 1],
      ['tsubasa-dorm', 1],
      ['aoba-aw820', 1],
      [UNCLASSIFIED, 1],
    ]);
  });

  it('未分類の資料が無ければ「未分類」の選択肢は出さない。URL で選ばれた、資料に無い対象は件数 0 で残す', () => {
    const classified = DOCS.filter((d) => d.target !== null);
    expect(facetOptions(classified, EMPTY_FILTER, CONTEXT, (s) => s).target.some((o) => o.value === UNCLASSIFIED)).toBe(false);
    const options = facetOptions(classified, withFilter({ target: 'gone' }), CONTEXT, (s) => s);
    expect(options.target.at(-1)).toEqual({ value: 'gone', label: 'gone', count: 0 });
  });

  it('選択肢の件数は、その条件以外で絞った資料の数', () => {
    const filter = withFilter({ status: 'failed', query: 'ザーハード' });
    // 状態の件数は、状態の条件を外す: ザーハードは 2 件（ready 1・failed 1）
    expect(facetCounts(DOCS, filter, CONTEXT, 'status')).toEqual({ total: 2, byValue: new Map([['ready', 1], ['failed', 1]]) });
    // ページ数の件数は、状態の条件（failed）を残す: ザーハード 2 のみ（1 ページ）
    expect(facetCounts(DOCS, filter, CONTEXT, 'pages')).toEqual({ total: 1, byValue: new Map([['one', 1]]) });
  });

  it('帯の無い資料（ページ数なし・日付なし）は、帯の件数に数えず、「すべて」には数える', () => {
    const counts = facetCounts(DOCS, EMPTY_FILTER, CONTEXT, 'pages');
    expect(counts.total).toBe(6);
    expect([...counts.byValue.values()].reduce((a, b) => a + b, 0)).toBe(5);
  });

  it('選択肢は、「すべて」の次に、資料にある状態・全ての帯を並べる', () => {
    const options = facetOptions(DOCS, EMPTY_FILTER, CONTEXT, (s) => s);
    expect(options.status).toEqual([
      { value: null, label: 'すべて', count: 6 },
      { value: 'ready', label: 'ready', count: 5 },
      { value: 'failed', label: 'failed', count: 1 },
    ]);
    expect(options.pages.map((o) => [o.value, o.count])).toEqual([[null, 6], ['one', 2], ['few', 3], ['many', 0]]);
    expect(options.updated.map((o) => [o.value, o.count])).toEqual([[null, 6], ['week', 1], ['month', 3], ['older', 1]]);
  });

  it('URL で指定された、資料に無い状態も、件数 0 の選択肢として残す（選択が見えなくならない）', () => {
    const options = facetOptions(DOCS, withFilter({ status: 'processing' }), CONTEXT, (s) => s);
    expect(options.status.at(-1)).toEqual({ value: 'processing', label: 'processing', count: 0 });
  });
});
