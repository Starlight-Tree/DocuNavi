import { useState } from 'react';
import type { ThreadsState } from '../hooks/useThreads';

interface ThreadSidebarProps {
  state: ThreadsState;
  /** いま開いている会話の ID。まだ一覧に無い（始めたばかり）こともある。 */
  currentId: string | null;
  /** 回答の生成中。この間は、開いている会話を削除できず、別の会話にも移れない（生成が止まってしまうため）。 */
  busy: boolean;
  onSelect: (threadId: string) => void;
  /** 削除する。失敗したときは、利用者向けのメッセージを返す。 */
  onDelete: (threadId: string) => Promise<string | null>;
  onRetry: () => void;
}

export const MSG_NO_THREADS = 'まだ会話がありません。質問を送ると、ここに並びます。';
export const MSG_THREADS_UNAVAILABLE = 'ログインを使わない構成では、会話の一覧を表示できません。';

function formatTime(iso: string | null): string {
  if (!iso) return '';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return '';
  return date.toLocaleString('ja-JP', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function ThreadSidebar({ state, currentId, busy, onSelect, onDelete, onRetry }: ThreadSidebarProps) {
  // 削除の確認は、その場で 2 段階にする（誤って押しても消えない）
  const [confirmingId, setConfirmingId] = useState<string | null>(null);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  const confirmDelete = async (threadId: string) => {
    setDeleteError(null);
    setConfirmingId(null);
    setDeleteError(await onDelete(threadId));
  };

  return (
    <aside className="thread-sidebar" aria-label="会話の一覧">
      <h2 className="thread-sidebar-title">会話の履歴</h2>

      {deleteError && (
        <p className="thread-sidebar-error" role="alert">
          {deleteError}
        </p>
      )}

      {state.status === 'unavailable' && <p className="thread-sidebar-note">{MSG_THREADS_UNAVAILABLE}</p>}

      {state.status === 'loading' && (
        <p className="thread-sidebar-note" role="status">
          読み込み中…
        </p>
      )}

      {state.status === 'error' && (
        <div className="thread-sidebar-note">
          <p role="alert">{state.message}</p>
          <button type="button" className="btn btn-secondary btn-small" onClick={onRetry}>
            再読み込み
          </button>
        </div>
      )}

      {state.status === 'ready' && state.threads.length === 0 && <p className="thread-sidebar-note">{MSG_NO_THREADS}</p>}

      {state.status === 'ready' && state.threads.length > 0 && (
        <ul className="thread-list">
          {state.threads.map((thread) => {
            const current = thread.threadId === currentId;
            const confirming = confirmingId === thread.threadId;
            return (
              <li key={thread.threadId} className="thread-item" data-current={current ? 'true' : undefined}>
                <button
                  type="button"
                  className="thread-open"
                  aria-current={current ? 'true' : undefined}
                  title={thread.title}
                  disabled={busy && !current}
                  onClick={() => onSelect(thread.threadId)}
                >
                  <span className="thread-title">{thread.title}</span>
                  <span className="thread-time">{formatTime(thread.updatedAt)}</span>
                </button>
                {confirming ? (
                  <span className="thread-confirm">
                    <button
                      type="button"
                      className="btn btn-small thread-confirm-yes"
                      onClick={() => void confirmDelete(thread.threadId)}
                    >
                      削除する
                    </button>
                    <button type="button" className="btn btn-small" onClick={() => setConfirmingId(null)}>
                      やめる
                    </button>
                  </span>
                ) : (
                  <button
                    type="button"
                    className="thread-delete"
                    aria-label={`「${thread.title}」を削除`}
                    disabled={current && busy}
                    onClick={() => {
                      setDeleteError(null);
                      setConfirmingId(thread.threadId);
                    }}
                  >
                    削除
                  </button>
                )}
              </li>
            );
          })}
        </ul>
      )}
    </aside>
  );
}
