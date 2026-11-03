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
    <div className="ws-sidebar">
      <div className="ws-brand">docunavi</div>
      <button type="button" className="btn btn-primary ws-new" onClick={() => dispatch({ type: 'newConversation', now: Date.now() })}>
        ＋ 新しい会話
      </button>
      <nav aria-label="画面">
        <ul className="ws-nav">
          <li>
            <button type="button" className="ws-nav-item" aria-current={chatActive ? 'page' : undefined} onClick={() => dispatch({ type: 'setView', view: 'chat' })}>
              チャット
            </button>
          </li>
          <li>
            <button
              type="button"
              className="ws-nav-item"
              aria-current={!chatActive ? 'page' : undefined}
              onClick={() => dispatch({ type: 'setView', view: 'documents' })}
            >
              資料
              <span className="ws-count" aria-label={`${state.docs.length}件${processing > 0 ? `、うち取り込み中${processing}件` : ''}`}>
                {state.docs.length}
                {processing > 0 && <span className="ws-spinner" aria-hidden="true" />}
              </span>
            </button>
          </li>
        </ul>
      </nav>

      <h2 className="ws-side-title">会話履歴</h2>
      {history.length === 0 ? (
        <p className="ws-side-note">まだ会話がありません。</p>
      ) : (
        <ul className="ws-history">
          {history.map((thread) => (
            <li key={thread.id}>
              <button
                type="button"
                className="ws-history-item"
                aria-current={chatActive && thread.id === state.activeId ? 'true' : undefined}
                onClick={() => dispatch({ type: 'openThread', id: thread.id })}
              >
                <span className="ws-history-title">{thread.title}</span>
                <span className="ws-history-time">{formatDateTime(thread.updatedAt)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="ws-side-foot">
        <button type="button" className="ws-nav-item" onClick={() => dispatch({ type: 'openDialog', dialog: { type: 'settings' } })}>
          設定
        </button>
        <p className="ws-mock-note">
          <strong>UI モック</strong>
          実データ・AI・外部サービスには接続していません。
        </p>
      </div>
    </div>
  );
}
