import { useId } from 'react';
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

/**
 * サイドバー上部のロゴの絵（星あかりの空に立つ大樹。樹冠の枝は白抜きで、頭上に星が弧を描き、足もとにゆるい丘）。色は 1 色で、周りの文字の色（`currentColor`）に合わせる。
 * ファビコン（`app/icon.svg`）は同じ絵で、明るいタブにも暗いタブにも見える中間のグレー。
 */
export function BrandIcon() {
  // 白抜きのマスクの id は、ロゴを同時に 2 つ描いても重ならないよう、インスタンスごとに分ける
  const maskId = `brand-mask-${useId().replace(/:/g, '')}`;
  return (
    <svg className="ws-brand-icon" width="24" height="24" viewBox="1 0.5 30 30" aria-hidden="true">
      <g fill="currentColor">
        <path d="M16 0.5 L16.87 2.53 L18.9 3.4 L16.87 4.27 L16 6.3 L15.13 4.27 L13.1 3.4 L15.13 2.53Z" />
        <path d="M9 3.4 L9.6 4.8 L11 5.4 L9.6 6 L9 7.4 L8.4 6 L7 5.4 L8.4 4.8Z" />
        <path d="M23 3.4 L23.6 4.8 L25 5.4 L23.6 6 L23 7.4 L22.4 6 L21 5.4 L22.4 4.8Z" />
        <path d="M4.2 9.6 L4.62 10.58 L5.6 11 L4.62 11.42 L4.2 12.4 L3.78 11.42 L2.8 11 L3.78 10.58Z" />
        <path d="M27.8 9.6 L28.22 10.58 L29.2 11 L28.22 11.42 L27.8 12.4 L27.38 11.42 L26.4 11 L27.38 10.58Z" />
      </g>
      <g transform="translate(4.16 6.3) scale(0.74)">
        <mask id={maskId}>
          <rect width="32" height="32" fill="#fff" />
          <g fill="none" stroke="#000" strokeWidth="1.5" strokeLinecap="round">
            <path d="M16 19 V9.5" />
            <path d="M16 16.5 L11 11.5" />
            <path d="M16 16.5 L21 11.5" />
          </g>
        </mask>
        <g fill="currentColor" mask={`url(#${maskId})`}>
          <circle cx="16" cy="11.5" r="7.5" />
          <circle cx="9" cy="14" r="5.5" />
          <circle cx="23" cy="14" r="5.5" />
          <circle cx="11.5" cy="8" r="5" />
          <circle cx="20.5" cy="8" r="5" />
          <path d="M14 17 H18 L19.2 28.5 H12.8Z" />
        </g>
      </g>
      <path d="M7 29.4 Q16 26.6 25 29.4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" />
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
