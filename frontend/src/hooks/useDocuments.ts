import { useCallback, useEffect, useState } from 'react';
import { fetchDocumentChunks, fetchDocumentPdf, fetchDocuments } from '../api/documents';
import type { DocumentDetail, DocumentList } from '../api/documents';
import { fetchImpact, fetchRelationGraph } from '../api/graph';
import type { Impact, RelationGraph } from '../api/graph';
import { signalPanelOpen } from '../lib/panelSignal';
import { findSourceChunk } from '../lib/sources';
import { useUrlState } from '../lib/urlState';
import { useAuthedJsonFetch, useLoaded } from './useLoaded';
import type { Loaded } from './useLoaded';

/** 資料の一覧（GET /documents）。`enabled` が false の間は取らない（true になるたびに取り直す）。 */
export function useDocumentList(enabled = true): [Loaded<DocumentList>, () => void] {
  const fetcher = useAuthedJsonFetch();
  const load = useCallback((signal: AbortSignal) => fetchDocuments(fetcher, signal), [fetcher]);
  return useLoaded(enabled ? load : null);
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
 * チャットの出典（ファイル名と抜粋）から、資料を右のパネルに開く。画面（チャット・資料一覧）は移らない。
 * 資料の一覧からファイル名で資料を探し、「抽出テキスト」で根拠のチャンクを強調して開く。
 * - ページは、検索が返した値（knownPage）だけを持たせる。無いときは、推測したページを確定情報にしない（「元のPDF」は先頭から開く）。
 * - 根拠のチャンクが見つからない・チャンクを取れないときも、資料は開く（何も強調しない）。
 * `opener` は押した要素。閉じたときにフォーカスを戻す先になる。
 */
export function useOpenSource(): (
  source: string,
  excerpt: string,
  knownPage?: number,
  opener?: Element | null,
) => Promise<OpenSourceResult> {
  const fetcher = useAuthedJsonFetch();
  const { navigate } = useUrlState();
  return useCallback(
    async (source: string, excerpt: string, knownPage?: number, opener?: Element | null) => {
      try {
        const list = await fetchDocuments(fetcher);
        const document = list.documents.find((d) => d.source === source);
        if (!document) return 'notfound';
        let chunk: number | null = null;
        try {
          chunk = findSourceChunk((await fetchDocumentChunks(fetcher, document.id)).chunks, excerpt, knownPage);
        } catch {
          // 根拠の箇所が分からなくても、資料は開く
        }
        signalPanelOpen(opener ?? null, true, chunk === null ? document.id : null);
        navigate({ doc: document.id, view: 'text', page: knownPage ?? null, chunk, item: null });
        return 'opened';
      } catch {
        return 'error';
      }
    },
    [fetcher, navigate],
  );
}
