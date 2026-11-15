import type { MouseEvent, ReactNode, Ref } from 'react';
import type { Tab } from '../lib/urlState';
import { AccountMenu } from './AccountMenu';

interface AppSidebarProps {
  /** いま開いている画面。 */
  view: Tab;
  /** 動作モード（`/health`）。分からなければ null。 */
  mode: string | null;
  /** ログイン中の利用者の表示名。ログインを使わない構成では渡さない。 */
  userLabel?: string | undefined;
  onSignOut?: (() => void) | undefined;
  onNewConversation: () => void;
  /** サイドバーを閉じる（再表示のボタンは、呼び出し側がメインの上に出す）。渡さなければ、閉じるボタンを出さない（UI モック）。 */
  onCollapse?: (() => void) | undefined;
  /** 閉じるボタン。再表示したあとにフォーカスを戻す先。 */
  collapseRef?: Ref<HTMLButtonElement>;
  onGoChat: () => void;
  onGoDocuments: () => void;
  /** 資料一覧（ナレッジ）への入口を、上部の「チャット / 資料」ではなく、左下のメニューに置く。UI モックは渡さない。 */
  knowledgeInMenu?: boolean | undefined;
  /** 会話の履歴（ログイン中だけ）。 */
  history?: ReactNode;
  /** 登録資料の数。分からない間は渡さない。 */
  documentCount?: number | undefined;
  /** 閉じた状態を、アイコンだけの細いレールで見せる（PC）。履歴・文字は隠す。渡さない（スマホ・UI モック）と、閉じたら完全に隠す。 */
  rail?: boolean | undefined;
  /**
   * レールの先頭（ロゴの位置）の「サイドバーを開く」ボタン。閉じたときにフォーカスを移す先。
   * 閉じたレールの空き部分のクリックでも開く。レールの項目・アイコンは、自分のクリックだけを受ける。
   */
  railToggleRef?: Ref<HTMLButtonElement>;
  onExpand?: (() => void) | undefined;
}

const MODE_LABELS: Record<string, { label: string; hint: string }> = {
  mock: { label: 'モック', hint: 'LLM を呼ばないダミー応答で動作しています' },
  gemini: { label: 'Gemini', hint: 'Gemini（本物の LLM）で動作しています' },
};

/** サイドバーを畳む・開くボタンの絵（左に縦線のある四角）。 */
export function SidebarIcon() {
  return (
    <svg width="18" height="18" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" aria-hidden="true">
      <rect x="2.5" y="3.5" width="15" height="13" rx="2" />
      <path d="M8 3.5v13" />
    </svg>
  );
}

/** サイドバー上部のロゴの絵（コンパス）。ファビコン（`app/icon.svg`）と同じ絵・同じ色で、文字の色には合わせない。 */
export function BrandIcon() {
  return (
    <svg className="ws-brand-icon" width="24" height="24" viewBox="0 0 32 32" aria-hidden="true">
      <rect width="32" height="32" rx="7" fill="#2c4fcf" />
      <circle cx="16" cy="16" r="10" fill="none" stroke="#fff" strokeWidth="2.4" />
      <path d="M21.5 10.5l-3.3 7.7-7.7 3.3 3.3-7.7z" fill="#f2a65a" />
      <circle cx="16" cy="16" r="1.6" fill="#fff" />
    </svg>
  );
}

/** レールの「チャット」（吹き出し）。 */
function ChatIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d="M4 4.5h12a1.5 1.5 0 0 1 1.5 1.5v6a1.5 1.5 0 0 1-1.5 1.5H9l-3.5 3v-3H4A1.5 1.5 0 0 1 2.5 12V6A1.5 1.5 0 0 1 4 4.5z" />
    </svg>
  );
}

/** レールの「新しい会話」（線の十字）。 */
function PlusIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" aria-hidden="true">
      <path d="M10 4v12M4 10h12" />
    </svg>
  );
}

/** レールの「ナレッジ」（積んだ資料）。 */
function KnowledgeIcon() {
  return (
    <svg width="20" height="20" viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <rect x="3.5" y="3" width="13" height="4" rx="1.2" />
      <rect x="3.5" y="9" width="13" height="4" rx="1.2" />
      <path d="M3.5 16h13" />
    </svg>
  );
}

/** 新しいタブで開く操作（Ctrl・Cmd・Shift・中クリック）は、ブラウザーに任せる。ふつうのクリックだけ、画面の中で移る。 */
function inApp(action: () => void) {
  return (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    action();
  };
}

/**
 * 共通の左サイドバー。新しい会話、会話の履歴、アカウントのメニュー（ナレッジ・ログアウト）を持つ。閉じるボタンで畳める。閉じたレールは、ロゴの位置のボタンと、空き部分のクリックで開く。
 * `knowledgeInMenu` でないとき（UI モック）は、上部に画面の切り替え（チャット / 資料）を出す。
 */
export function AppSidebar({ view, mode, userLabel, onSignOut, onNewConversation, onCollapse, collapseRef, onGoChat, onGoDocuments, knowledgeInMenu, history, documentCount, rail, railToggleRef, onExpand }: AppSidebarProps) {
  const modeInfo = mode ? (MODE_LABELS[mode] ?? { label: mode, hint: `モード: ${mode}` }) : null;
  return (
    <div className="ws-sidebar">
      {rail && onExpand && (
        <nav className="ws-rail" aria-label="サイドバー（縮小表示）">
          {/* 空き部分の下敷き（マウス用。読み上げ・Tab の対象にはしない。キーボードと読み上げは、下のロゴの位置のボタンが受ける）。
              レールの項目・アイコンは、この上に載り、自分のクリックだけを受ける（shell.css の pointer-events） */}
          <button type="button" className="ws-rail-open" aria-hidden="true" tabIndex={-1} onClick={onExpand} />
          {/* 閉じているときのロゴの位置は、開くボタン。カーソル・フォーカスで、ロゴが展開の絵に変わる（開いているときのロゴは、チャットへ移るリンクのまま） */}
          <button ref={railToggleRef} type="button" className="ws-rail-btn ws-rail-logo" aria-label="サイドバーを開く" data-tip="サイドバーを開く" onClick={onExpand}>
            <BrandIcon />
            <span className="ws-rail-logo-open">
              <SidebarIcon />
            </span>
          </button>
          <a href="?tab=chat" className="ws-rail-btn" aria-label="チャット" data-tip="チャット" aria-current={view === 'chat' ? 'page' : undefined} onClick={inApp(onGoChat)}>
            <ChatIcon />
          </a>
          <button type="button" className="ws-rail-btn" aria-label="新しい会話" data-tip="新しい会話" onClick={onNewConversation}>
            <PlusIcon />
          </button>
          <a href="?tab=documents" className="ws-rail-btn" aria-label="ナレッジ" data-tip="ナレッジ" aria-current={view === 'documents' ? 'page' : undefined} onClick={inApp(onGoDocuments)}>
            <KnowledgeIcon />
          </a>
          <div className="ws-rail-foot">
            <AccountMenu label={userLabel} onSignOut={onSignOut} onOpenKnowledge={onGoDocuments} knowledgeCurrent={view === 'documents'} compact />
          </div>
        </nav>
      )}
      <div className="ws-side-full">
        <div className="ws-brand-row">
          <h1 className="ws-brand">
            <a href="?tab=chat" onClick={inApp(onGoChat)}>
              <BrandIcon />
              DocuNavi
            </a>
          </h1>
          {onCollapse && (
            <button ref={collapseRef} type="button" className="ws-icon-btn" aria-label="サイドバーを閉じる" onClick={onCollapse}>
              <SidebarIcon />
            </button>
          )}
        </div>
        <button type="button" className="btn btn-primary ws-new" onClick={onNewConversation}>
          <span aria-hidden="true">＋ </span>新しい会話
        </button>
        {!knowledgeInMenu && (
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
        )}
        {history}
        <div className="ws-side-foot">
          {modeInfo && (
            <span className={`mode-badge mode-${mode}`} title={modeInfo.hint}>
              <span className="visually-hidden">動作モード: </span>
              {modeInfo.label}
            </span>
          )}
          {knowledgeInMenu ? (
            <AccountMenu label={userLabel} onSignOut={onSignOut} onOpenKnowledge={onGoDocuments} knowledgeCurrent={view === 'documents'} />
          ) : (
            userLabel && onSignOut && <AccountMenu label={userLabel} onSignOut={onSignOut} onOpenKnowledge={onGoDocuments} knowledgeCurrent={view === 'documents'} />
          )}
        </div>
      </div>
    </div>
  );
}
