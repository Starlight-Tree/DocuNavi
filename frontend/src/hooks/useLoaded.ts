import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ChatApiError, isAbortError, jsonFetch } from '../api/chat';
import { useAuth } from '../auth/AuthContext';
import { createAuthedFetch } from '../auth/authFetch';

const MSG_FAILED = '資料を取得できませんでした。しばらく待ってから再試行してください。';

export type Loaded<T> =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; data: T };

/** ログイン中ならトークンを付け、401 なら 1 回更新して送り直す fetch（チャットと同じ仕組み）。 */
export function useAuthedJsonFetch() {
  const { client, expire, denyAccess } = useAuth();
  const clientRef = useRef(client);
  const expireRef = useRef(expire);
  const denyRef = useRef(denyAccess);
  clientRef.current = client;
  expireRef.current = expire;
  denyRef.current = denyAccess;
  return useMemo(
    () =>
      createAuthedFetch(
        () => clientRef.current,
        () => expireRef.current(),
        jsonFetch,
        () => denyRef.current(),
      ),
    [],
  );
}

/** load を実行して結果を状態にする。reload で取り直す。画面を離れる・取り直すときは前の取得を中断する。 */
export function useLoaded<T>(load: ((signal: AbortSignal) => Promise<T>) | null): [Loaded<T>, () => void] {
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
