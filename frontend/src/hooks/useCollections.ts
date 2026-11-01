import { createContext, useCallback, useContext } from 'react';
import { fetchCollections } from '../api/collections';
import type { CollectionCatalog, CollectionInfo } from '../api/collections';
import { useAuthedJsonFetch, useLoaded } from './useLoaded';
import type { Loaded } from './useLoaded';

/** 資料グループの一覧（GET /collections）。画面全体で 1 回だけ取り、Context で配る。 */
export function useCollectionCatalogLoader(): CollectionCatalogApi {
  const fetcher = useAuthedJsonFetch();
  const load = useCallback((signal: AbortSignal) => fetchCollections(fetcher, signal), [fetcher]);
  const [state, reload] = useLoaded(load);
  return { state, reload };
}

export interface CollectionCatalogApi {
  state: Loaded<CollectionCatalog>;
  reload: () => void;
}

/** 読み込み前（Provider の外）の既定。グループの機能は何も出ない。 */
export const CollectionCatalogContext = createContext<CollectionCatalogApi>({
  state: { status: 'loading' },
  reload: () => undefined,
});

export function useCollectionCatalog(): CollectionCatalogApi {
  return useContext(CollectionCatalogContext);
}

/**
 * 検索範囲・グループのカードを出せるときだけ、グループの一覧を返す。
 * 読み込み中・失敗・サーバーが DB を使っていない（configured:false）・グループが 0 件のときは null（グループの機能は出さない）。
 * 旧 API は /collections が 404 になって失敗するので、範囲を選べない（旧 API に範囲を送らない）。
 */
export function usableCollections(state: Loaded<CollectionCatalog>): CollectionInfo[] | null {
  if (state.status !== 'ready' || !state.data.configured || state.data.collections.length === 0) return null;
  return state.data.collections;
}
