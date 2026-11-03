import { historyThreads } from './store';
import { useMock } from './MockProvider';
import { formatDateTime } from './data';

/** 共通の左サイドバー。画面の切り替えは、ここだけで行う（上部にタブは置かない）。 */
export function Sidebar() {
  const { state, dispatch } = useMock();
  const history = historyThreads(state);
  const processing = state.docs.filter((d) => d.status === 'processing').length;
  const chatActive = state.view === 'chat';

  return (
    <div className="mk-sidebar">
      <div className="mk-brand">docunavi</div>
      <button type="button" className="btn btn-primary mk-new" onClick={() => dispatch({ type: 'newConversation', now: Date.now() })}>
        ＋ 新しい会話
      </button>
      <nav aria-label="画面">
        <ul className="mk-nav">
          <li>
            <button type="button" className="mk-nav-item" aria-current={chatActive ? 'page' : undefined} onClick={() => dispatch({ type: 'setView', view: 'chat' })}>
              チャット
            </button>
          </li>
          <li>
            <button
              type="button"
              className="mk-nav-item"
              aria-current={!chatActive ? 'page' : undefined}
              onClick={() => dispatch({ type: 'setView', view: 'documents' })}
            >
              資料
              <span className="mk-count" aria-label={`${state.docs.length}件${processing > 0 ? `、うち取り込み中${processing}件` : ''}`}>
                {state.docs.length}
                {processing > 0 && <span className="mk-spinner" aria-hidden="true" />}
              </span>
            </button>
          </li>
        </ul>
      </nav>

      <h2 className="mk-side-title">会話履歴</h2>
      {history.length === 0 ? (
        <p className="mk-side-note">まだ会話がありません。</p>
      ) : (
        <ul className="mk-history">
          {history.map((thread) => (
            <li key={thread.id}>
              <button
                type="button"
                className="mk-history-item"
                aria-current={chatActive && thread.id === state.activeId ? 'true' : undefined}
                onClick={() => dispatch({ type: 'openThread', id: thread.id })}
              >
                <span className="mk-history-title">{thread.title}</span>
                <span className="mk-history-time">{formatDateTime(thread.updatedAt)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="mk-side-foot">
        <button type="button" className="mk-nav-item" onClick={() => dispatch({ type: 'openDialog', dialog: { type: 'settings' } })}>
          設定
        </button>
        <p className="mk-mock-note">
          <strong>UI モック</strong>
          実データ・AI・外部サービスには接続していません。
        </p>
      </div>
    </div>
  );
}
