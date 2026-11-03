import { createContext, useCallback, useContext, useMemo } from 'react';
import { fetchDocTypes, fetchTargets } from '../api/classifications';
import type { ClassificationCatalog, ClassificationInfo } from '../api/classifications';
import { useAuthedJsonFetch, useLoaded } from './useLoaded';
import type { Loaded } from './useLoaded';

export interface ClassificationCatalogApi {
  targets: Loaded<ClassificationCatalog>;
  docTypes: Loaded<ClassificationCatalog>;
  /** 対象の一覧だけを取り直す（チャットの「再試行」）。 */
  reloadTargets: () => void;
  reloadDocTypes: () => void;
}

/** 対象と資料種別の一覧（GET /targets・GET /doc-types）。画面全体で 1 回ずつ取り、Context で配る。 */
export function useClassificationLoader(): ClassificationCatalogApi {
  const fetcher = useAuthedJsonFetch();
  const loadTargets = useCallback((signal: AbortSignal) => fetchTargets(fetcher, signal), [fetcher]);
  const loadDocTypes = useCallback((signal: AbortSignal) => fetchDocTypes(fetcher, signal), [fetcher]);
  const [targets, reloadTargets] = useLoaded(loadTargets);
  const [docTypes, reloadDocTypes] = useLoaded(loadDocTypes);
  return useMemo(
    () => ({ targets, docTypes, reloadTargets, reloadDocTypes }),
    [targets, docTypes, reloadTargets, reloadDocTypes],
  );
}

/** 読み込み前（Provider の外）の既定。対象・資料種別の機能は何も出ない。 */
export const ClassificationCatalogContext = createContext<ClassificationCatalogApi>({
  targets: { status: 'loading' },
  docTypes: { status: 'loading' },
  reloadTargets: () => undefined,
  reloadDocTypes: () => undefined,
});

export function useClassificationCatalog(): ClassificationCatalogApi {
  return useContext(ClassificationCatalogContext);
}

/**
 * 質問対象を選べるときだけ、対象の一覧を返す。
 * 読み込み中・失敗・サーバーが DB を使っていない（configured:false）・対象が 0 件のときは null（対象の機能は出さない）。
 * 旧 API は /targets が 404 になって失敗するので、対象を選べない（旧 API に target を送らない）。
 */
export function usableClassifications(state: Loaded<ClassificationCatalog>): ClassificationInfo[] | null {
  if (state.status !== 'ready' || !state.data.configured || state.data.items.length === 0) return null;
  return state.data.items;
}

/** 表示名を引くための一覧。読めていないときは undefined（識別名のまま表示する）。 */
export function classificationItems(state: Loaded<ClassificationCatalog>): ClassificationInfo[] | undefined {
  return state.status === 'ready' ? state.data.items : undefined;
}
