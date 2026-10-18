import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChatApiError, isAbortError, jsonFetch } from '../api/chat';
import { fetchDocumentChunks, fetchDocumentPdf, fetchDocuments } from '../api/documents';
import type { DocumentDetail, DocumentList } from '../api/documents';
import { fetchImpact, fetchRelationGraph } from '../api/graph';
import type { Impact, RelationGraph } from '../api/graph';
import { useAuth } from '../auth/AuthContext';
import { createAuthedFetch } from '../auth/authFetch';
import { findSourcePage } from '../lib/sources';
import { useUrlState } from '../lib/urlState';

const MSG_FAILED = '資料を取得できませんでした。しばらく待ってから再試行してください。';

export type Loaded<T> =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: T };

/** ログイン中ならトークンを付け、401 なら 1 回更新して送り直す fetch（チャットと同じ仕組み）。 */
function useAuthedJsonFetch() {
  const { client, expire } = useAuth();
  const clientRef = useRef(client);
  const expireRef = useRef(expire);
  clientRef.current = client;
  expireRef.current = expire;
  return useMemo(
    () =>
      createAuthedFetch(
        () => clientRef.current,
        () => expireRef.current(),
        jsonFetch,
      ),
    [],
  );
}

/** load を実行して結果を状態にする。reload で取り直す。画面を離れる・取り直すときは前の取得を中断する。 */
function useLoaded<T>(load: ((signal: AbortSignal) => Promise<T>) | null): [Loaded<T>, () => void] {
  const [state, setState] = useState<Loaded<T>>({ status: 'loading' });
  const [round, setRound] = useState(0);

  useEffect(() => {
    if (!load) return;
    const controller = new AbortController();
    setState({ status: 'loading' });
    load(controller.signal).then(
      (data) => {
        if (!controller.signal.aborted) setState({ status: 'ready', data });
      },
      (error: unknown) => {
        if (controller.signal.aborted || isAbortError(error)) return;
        setState({
          status: 'error',
          message: error instanceof ChatApiError ? error.message : MSG_FAILED,
        });
      },
    );
    return () => controller.abort();
  }, [load, round]);

  const reload = useCallback(() => setRound((n) => n + 1), []);
  return [state, reload];
}

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
 * 資料の一覧からファイル名で資料を探し、チャンクと突き合わせてページを決める。ページが決まらなくても、資料は開く。
 */
export function useOpenSource(): (source: string, excerpt: string) => Promise<OpenSourceResult> {
  const fetcher = useAuthedJsonFetch();
  const { navigate } = useUrlState();
  return useCallback(
    async (source: string, excerpt: string) => {
      try {
        const list = await fetchDocuments(fetcher);
        const document = list.documents.find((d) => d.source === source);
        if (!document) return 'notfound';
        let page: number | null = null;
        try {
          page = findSourcePage((await fetchDocumentChunks(fetcher, document.id)).chunks, excerpt);
        } catch {
          // ページが分からなくても、資料は開く
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
