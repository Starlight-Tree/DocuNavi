import type { MouseEvent, ReactNode } from 'react';
import type { Tab } from '../lib/urlState';

interface AppSidebarProps {
  /** いま開いている画面。 */
  view: Tab;
  /** 動作モード（`/health`）。分からなければ null。 */
  mode: string | null;
  /** ログイン中の利用者の表示名。ログインを使わない構成では渡さない。 */
  userLabel?: string | undefined;
  onSignOut?: (() => void) | undefined;
  onNewConversation: () => void;
  onGoChat: () => void;
  onGoDocuments: () => void;
  /** 会話の履歴（ログイン中だけ）。 */
  history?: ReactNode;
  /** 登録資料の数。分からない間は渡さない。 */
  documentCount?: number | undefined;
}

const MODE_LABELS: Record<string, { label: string; hint: string }> = {
  mock: { label: 'モック', hint: 'LLM を呼ばないダミー応答で動作しています' },
  gemini: { label: 'Gemini', hint: 'Gemini（本物の LLM）で動作しています' },
};

/** 新しいタブで開く操作（Ctrl・Cmd・Shift・中クリック）は、ブラウザーに任せる。ふつうのクリックだけ、画面の中で移る。 */
function inApp(action: () => void) {
  return (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    action();
  };
}

/**
 * 共通の左サイドバー。画面の切り替え（チャット / 資料）と、新しい会話、会話の履歴、利用者の表示を持つ。
 * 画面の切り替えはここだけで行い、上部には同じ目的のタブを置かない。
 */
export function AppSidebar({ view, mode, userLabel, onSignOut, onNewConversation, onGoChat, onGoDocuments, history, documentCount }: AppSidebarProps) {
  const modeInfo = mode ? (MODE_LABELS[mode] ?? { label: mode, hint: `モード: ${mode}` }) : null;
  return (
    <div className="ws-sidebar">
      <h1 className="ws-brand">
        <a href="?tab=documents" onClick={inApp(onGoDocuments)}>
          ドキュナビ
        </a>
      </h1>
      <button type="button" className="btn btn-primary ws-new" onClick={onNewConversation}>
        <span aria-hidden="true">＋ </span>新しい会話
      </button>
      <nav aria-label="画面">
        <ul className="ws-nav">
          <li>
            <a href="?tab=chat" className="ws-nav-item" aria-current={view === 'chat' ? 'page' : undefined} onClick={inApp(onGoChat)}>
              チャット
            </a>
          </li>
          <li>
            <a href="?tab=documents" className="ws-nav-item" aria-current={view === 'documents' ? 'page' : undefined} onClick={inApp(onGoDocuments)}>
              資料
              {documentCount !== undefined && <span className="ws-count">{documentCount}</span>}
            </a>
          </li>
        </ul>
      </nav>
      {history}
      <div className="ws-side-foot">
        {modeInfo && (
          <span className={`mode-badge mode-${mode}`} title={modeInfo.hint}>
            <span className="visually-hidden">動作モード: </span>
            {modeInfo.label}
          </span>
        )}
        {userLabel && <span className="ws-user">{userLabel}</span>}
        {onSignOut && (
          <button type="button" className="btn btn-secondary" onClick={onSignOut}>
            ログアウト
          </button>
        )}
      </div>
    </div>
  );
}
