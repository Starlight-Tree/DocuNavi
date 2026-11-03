import { useCallback, useEffect, useState } from 'react';
import { ChatApiError, isAbortError } from '../api/chat';
import { deleteThread, fetchThreads } from '../api/threads';
import type { ThreadSummary } from '../api/threads';
import { useAuthedJsonFetch } from './useLoaded';

const MSG_LIST_FAILED = '会話の一覧を取得できませんでした。しばらく待ってから再試行してください。';
const MSG_DELETE_FAILED = '会話を削除できませんでした。しばらく待ってから再試行してください。';

export type ThreadsState =
  /** 会話の一覧を出せない構成（ログインを使わない。サーバーも一覧を返さない）。読み込み中ではない。 */
  | { status: 'unavailable' }
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

/** 会話の一覧（GET /threads）。enabled が false の間は何も呼ばず、unavailable を返す（ログインを使わない構成では、一覧を出せない）。 */
export function useThreads(enabled: boolean): UseThreads {
  const fetcher = useAuthedJsonFetch();
  const [loaded, setState] = useState<ThreadsState>({ status: 'loading' });
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

  const state: ThreadsState = enabled ? loaded : { status: 'unavailable' };
  return { state, reload, remove };
}
