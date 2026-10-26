import { useCallback, useEffect, useState } from 'react';
import { ChatApiError, isAbortError } from '../api/chat';
import { deleteThread, fetchThreads } from '../api/threads';
import type { ThreadSummary } from '../api/threads';
import { useAuthedJsonFetch } from './useDocuments';

const MSG_LIST_FAILED = '会話の一覧を取得できませんでした。しばらく待ってから再試行してください。';
const MSG_DELETE_FAILED = '会話を削除できませんでした。しばらく待ってから再試行してください。';

export type ThreadsState =
  | { status: 'loading' }
  | { status: 'error'; message: string }
  | { status: 'ready'; threads: ThreadSummary[] };

export interface UseThreads {
  state: ThreadsState;
  /** 一覧を取り直す。取り直している間も、前の一覧を出し続ける（ちらつかせない）。 */
  reload: () => void;
  /** 会話を削除して、一覧を取り直す。失敗したら、利用者向けのメッセージを返す。 */
  remove: (threadId: string) => Promise<string | null>;
}

/** 会話の一覧（GET /threads）。enabled が false の間は何も呼ばない（ログインしていない構成では、一覧を出さない）。 */
export function useThreads(enabled: boolean): UseThreads {
  const fetcher = useAuthedJsonFetch();
  const [state, setState] = useState<ThreadsState>({ status: 'loading' });
  const [round, setRound] = useState(0);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    fetchThreads(fetcher, controller.signal).then(
      (threads) => {
        if (!controller.signal.aborted) setState({ status: 'ready', threads });
      },
      (error: unknown) => {
        if (controller.signal.aborted || isAbortError(error)) return;
        setState({ status: 'error', message: error instanceof ChatApiError ? error.message : MSG_LIST_FAILED });
      },
    );
    return () => controller.abort();
  }, [enabled, fetcher, round]);

  const reload = useCallback(() => setRound((n) => n + 1), []);

  const remove = useCallback(
    async (threadId: string): Promise<string | null> => {
      try {
        await deleteThread(fetcher, threadId);
      } catch (error) {
        return error instanceof ChatApiError ? error.message : MSG_DELETE_FAILED;
      }
      setRound((n) => n + 1);
      return null;
    },
    [fetcher],
  );

  return { state, reload, remove };
}
