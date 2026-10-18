import { useCallback, useMemo, useSyncExternalStore } from 'react';

/**
 * 画面の状態（タブ・資料・表示の切り替え・関係図で選んだ項）を、URL のクエリに持たせる。
 * 戻る・進む・再読み込み・URL の共有で、同じ画面に戻れる。
 *
 * 例: /?tab=documents&doc=3&view=graph&item=a11p1 、/?tab=documents&doc=3&view=pdf&page=2 、/?tab=documents&collection=cafe
 * 入力途中の文・開閉・スクロール位置など、一時的な状態は URL に置かない（トークンなどの秘密も置かない）。
 */

export type Tab = 'chat' | 'documents';
export type DetailView = 'pdf' | 'text' | 'graph';

export interface UrlState {
  tab: Tab;
  /** 開いている資料の ID。一覧を見ているときは null。 */
  doc: number | null;
  /** 資料の詳細の表示。指定が無ければ null（画面側が既定を決める）。 */
  view: DetailView | null;
  /** 関係図で選んだ項の ID。 */
  item: string | null;
  /** 元の PDF で開くページ（1 始まり）。表示が PDF のときだけ意味がある。 */
  page: number | null;
  /** 一覧の絞り込み（資料群の名前）。一覧を見ているときだけ意味がある。 */
  collection: string | null;
}

const VIEWS: readonly DetailView[] = ['pdf', 'text', 'graph'];
// 項の ID（例: a11p1、fn1）の形。長さと文字を絞り、変な値を状態に持ち込まない
const ITEM_PATTERN = /^[A-Za-z0-9_-]{1,40}$/;
const DOC_PATTERN = /^[1-9]\d{0,9}$/;
const PAGE_PATTERN = /^[1-9]\d{0,3}$/;
// 資料群の名前の形（DB の制約と同じ）
const COLLECTION_PATTERN = /^[A-Za-z0-9_-]{1,40}$/;

/** 状態として意味が通るように整える。資料が無ければ表示も項も無く、関係図でなければ項は無い。絞り込みは一覧だけ。 */
function normalize(state: UrlState): UrlState {
  const doc = state.tab === 'documents' ? state.doc : null;
  const view = doc === null ? null : state.view;
  const item = view === 'graph' ? state.item : null;
  const page = view === 'pdf' ? state.page : null;
  const collection = state.tab === 'documents' && doc === null ? state.collection : null;
  return { tab: state.tab, doc, view, item, page, collection };
}

/** URL のクエリ（`?tab=…` 形式の文字列）から状態を読む。不正な値は、安全な既定値（チャット・一覧）にする。 */
export function parseUrlState(search: string): UrlState {
  const params = new URLSearchParams(search);
  const tab: Tab = params.get('tab') === 'documents' ? 'documents' : 'chat';
  const docParam = params.get('doc');
  const viewParam = params.get('view');
  const itemParam = params.get('item');
  const pageParam = params.get('page');
  const collectionParam = params.get('collection');
  return normalize({
    tab,
    doc: docParam !== null && DOC_PATTERN.test(docParam) ? Number(docParam) : null,
    view: VIEWS.find((v) => v === viewParam) ?? null,
    item: itemParam !== null && ITEM_PATTERN.test(itemParam) ? itemParam : null,
    page: pageParam !== null && PAGE_PATTERN.test(pageParam) ? Number(pageParam) : null,
    collection: collectionParam !== null && COLLECTION_PATTERN.test(collectionParam) ? collectionParam : null,
  });
}

/** いまのクエリに `next` を重ねた、新しいクエリを作る。この画面が扱わないパラメータは、そのまま残す。 */
export function buildSearch(current: string, next: Partial<UrlState>): string {
  const state = normalize({ ...parseUrlState(current), ...next });
  const params = new URLSearchParams(current);
  params.set('tab', state.tab);
  const set = (key: string, value: string | number | null) => {
    if (value === null) params.delete(key);
    else params.set(key, String(value));
  };
  set('doc', state.doc);
  set('view', state.view);
  set('item', state.item);
  set('page', state.page);
  set('collection', state.collection);
  return `?${params.toString()}`;
}

const CHANGE_EVENT = 'docunavi:urlchange';

function subscribe(onChange: () => void): () => void {
  window.addEventListener('popstate', onChange);
  window.addEventListener(CHANGE_EVENT, onChange);
  return () => {
    window.removeEventListener('popstate', onChange);
    window.removeEventListener(CHANGE_EVENT, onChange);
  };
}

/**
 * いまの URL の画面の状態を、整えたクエリ（`?tab=…`）にして返す。チャットが既定のときと、不正な値だけのときは null。
 * この画面が扱わないパラメータ（ログインの `code` など）は含めない。
 */
export function currentStateSearch(): string | null {
  const state = parseUrlState(window.location.search);
  return state.tab === 'chat' ? null : buildSearch('', state);
}

/**
 * クエリを、履歴を増やさずに URL へ書く（ログイン後に、ログイン前の画面へ戻すときに使う）。
 * `search` は、整えた状態を作る `currentStateSearch` の戻り値を想定する。ここでも読み直して整える。
 */
export function replaceUrlState(search: string): void {
  const target = buildSearch('', parseUrlState(search));
  const url = `${window.location.pathname}${target}${window.location.hash}`;
  window.history.replaceState(window.history.state, '', url);
  window.dispatchEvent(new Event(CHANGE_EVENT));
}

const getSearch = () => window.location.search;
const getServerSearch = () => '';

export type NavigateMode = 'push' | 'replace';

/**
 * URL を画面の状態の唯一の出どころとして読み、書き換える。
 * 意味のある遷移（タブ・資料・表示・項）は push（戻るで元に戻る）。不正な値を直すときだけ replace（履歴を増やさない）。
 */
export function useUrlState(): UrlState & {
  navigate: (next: Partial<UrlState>, mode?: NavigateMode) => void;
} {
  const search = useSyncExternalStore(subscribe, getSearch, getServerSearch);
  const state = useMemo(() => parseUrlState(search), [search]);

  const navigate = useCallback((next: Partial<UrlState>, mode: NavigateMode = 'push') => {
    const current = window.location.search;
    const target = buildSearch(current, next);
    if (target === current) return; // 同じ画面への移動で、履歴を増やさない
    const url = `${window.location.pathname}${target}${window.location.hash}`;
    if (mode === 'replace') window.history.replaceState(window.history.state, '', url);
    else window.history.pushState(window.history.state, '', url);
    window.dispatchEvent(new Event(CHANGE_EVENT));
  }, []);

  return { ...state, navigate };
}
