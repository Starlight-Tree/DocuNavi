import { useCallback, useEffect, useState } from 'react';
import { fetchDocumentChunks, fetchDocumentHero, fetchDocumentPdf, fetchDocuments } from '../api/documents';
import type { DocumentDetail, DocumentList } from '../api/documents';
import { fetchImpact, fetchRelationGraph } from '../api/graph';
import type { Impact, RelationGraph } from '../api/graph';
import { findSourcePage } from '../lib/sources';
import { useUrlState } from '../lib/urlState';
import { useAuthedJsonFetch, useLoaded } from './useLoaded';
import type { Loaded } from './useLoaded';

/** 資料の一覧（GET /documents）。 */
export function useDocumentList(): [Loaded<DocumentList>, () => void] {
  const fetcher = useAuthedJsonFetch();
  const load = useCallback((signal: AbortSignal) => fetchDocuments(fetcher, signal), [fetcher]);
  return useLoaded(load);
}

/** 1 つの資料のチャンク（GET /documents/{id}/chunks）。id が null の間は何も取らない。 */
export function useDocumentChunks(id: number | null): [Loaded<DocumentDetail>, () => void] {
  const fetcher = useAuthedJsonFetch();
  const load = useCallback(
    (signal: AbortSignal) => fetchDocumentChunks(fetcher, id as number, signal),
    [fetcher, id],
  );
  return useLoaded(id === null ? null : load);
}

/**
 * 元の PDF（GET /documents/{id}/file）を取って、表示用の Blob URL にする。id が null の間は何も取らない。
 * Blob URL は、PDF を取り直すとき・画面を離れるときに解放する（読み込み直後には解放しない。表示中のものが読めなくなる）。
 */
export function useDocumentPdf(id: number | null): [Loaded<{ url: string }>, () => void] {
  const fetcher = useAuthedJsonFetch();
  const load = useCallback((signal: AbortSignal) => fetchDocumentPdf(fetcher, id as number, signal), [fetcher, id]);
  const [state, reload] = useLoaded(id === null ? null : load);
  const blob = state.status === 'ready' ? state.data : null;
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!blob) {
      setUrl(null);
      return;
    }
    const created = URL.createObjectURL(blob);
    setUrl(created);
    return () => URL.revokeObjectURL(created);
  }, [blob]);

  if (state.status === 'ready') {
    return [url ? { status: 'ready', data: { url } } : { status: 'loading' }, reload];
  }
  return [state, reload];
}

/**
 * 資料のヒーロー画像（GET /documents/{id}/hero）を取って、表示用の Blob URL にする。画像が無い・取れない間は null
 * （失敗は画面に出さず、呼び出し側が灰色の代替表示のままにする）。Blob URL は、取り直すとき・画面を離れるときに解放する。
 */
export function useHeroImage(id: number): string | null {
  const fetcher = useAuthedJsonFetch();
  const load = useCallback((signal: AbortSignal) => fetchDocumentHero(fetcher, id, signal), [fetcher, id]);
  const [state] = useLoaded(load);
  const blob = state.status === 'ready' ? state.data : null;
  const [url, setUrl] = useState<string | null>(null);

  useEffect(() => {
    if (!blob) {
      setUrl(null);
      return;
    }
    const created = URL.createObjectURL(blob);
    setUrl(created);
    return () => URL.revokeObjectURL(created);
  }, [blob]);

  return url;
}

/** 入寮規程の関係図（GET /documents/{id}/graph）。 */
export function useRelationGraph(id: number): [Loaded<RelationGraph>, () => void] {
  const fetcher = useAuthedJsonFetch();
  const load = useCallback((signal: AbortSignal) => fetchRelationGraph(fetcher, id, signal), [fetcher, id]);
  return useLoaded(load);
}

/** あるノードを変えたときの影響（GET /documents/{id}/impact）。node が null の間は何も取らない。 */
export function useImpact(id: number, node: string | null): [Loaded<Impact>, () => void] {
  const fetcher = useAuthedJsonFetch();
  const load = useCallback(
    (signal: AbortSignal) => fetchImpact(fetcher, id, node as string, signal),
    [fetcher, id, node],
  );
  return useLoaded(node === null ? null : load);
}

export type OpenSourceResult = 'opened' | 'notfound' | 'error';

/**
 * チャットの出典（ファイル名と抜粋）から、資料の元PDF を、その箇所のページで開く。
 * 資料の一覧からファイル名で資料を探す。ページは、検索が返した値（knownPage）を使い、無ければチャンクと突き合わせて決める。
 * ページが決まらなくても、資料は開く。
 */
export function useOpenSource(): (
  source: string,
  excerpt: string,
  knownPage?: number,
) => Promise<OpenSourceResult> {
  const fetcher = useAuthedJsonFetch();
  const { navigate } = useUrlState();
  return useCallback(
    async (source: string, excerpt: string, knownPage?: number) => {
      try {
        const list = await fetchDocuments(fetcher);
        const document = list.documents.find((d) => d.source === source);
        if (!document) return 'notfound';
        // サーバーが検索で返したページがあればそれを使う。無いとき（古い応答）だけ、抜粋とチャンクの突き合わせで探す
        let page: number | null = knownPage ?? null;
        if (knownPage === undefined) {
          try {
            page = findSourcePage((await fetchDocumentChunks(fetcher, document.id)).chunks, excerpt);
          } catch {
            // ページが分からなくても、資料は開く
          }
        }
        navigate({ tab: 'documents', doc: document.id, view: 'pdf', page });
        return 'opened';
      } catch {
        return 'error';
      }
    },
    [fetcher, navigate],
  );
}
