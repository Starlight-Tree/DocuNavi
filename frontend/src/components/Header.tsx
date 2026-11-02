import type { MouseEvent } from 'react';
import type { Tab } from '../lib/urlState';

export type View = Tab;

interface HeaderProps {
  mode: string | null;
  /** いま開いている画面。資料一覧では「質問する」、チャットでは「新しい会話」を出す。 */
  view?: View | undefined;
  /** 資料一覧から、いまの会話と検索範囲のままチャットへ移る（「質問する」）。渡さなければ、ボタンは出さない。 */
  onAsk?: (() => void) | undefined;
  /** ロゴ（資料一覧へ戻るリンク）。渡さなければ、ただの文字にする。 */
  onGoDocuments?: (() => void) | undefined;
  onNewConversation: () => void;
  /** ログイン中の利用者の表示名。ログインを使わない構成では渡さない。 */
  userLabel?: string | undefined;
  onSignOut?: (() => void) | undefined;
}

const MODE_LABELS: Record<string, { label: string; hint: string }> = {
  mock: { label: 'モック', hint: 'LLM を呼ばないダミー応答で動作しています' },
  gemini: { label: 'Gemini', hint: 'Gemini（本物の LLM）で動作しています' },
};

export function Header({ mode, view = 'chat', onAsk, onGoDocuments, onNewConversation, userLabel, onSignOut }: HeaderProps) {
  const modeInfo = mode ? (MODE_LABELS[mode] ?? { label: mode, hint: `モード: ${mode}` }) : null;

  // 新しいタブで開く操作（Ctrl・Cmd・Shift・中クリック）は、ブラウザーに任せる。ふつうのクリックだけ、画面の中で移る
  const goDocuments = (event: MouseEvent<HTMLAnchorElement>) => {
    if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    onGoDocuments?.();
  };

  return (
    <header className="app-header">
      <h1 className="app-title">
        {onGoDocuments ? (
          <a href="?tab=documents" onClick={goDocuments}>
            ドキュナビ
          </a>
        ) : (
          'ドキュナビ'
        )}
      </h1>
      <div className="app-header-actions">
        {modeInfo && (
          <span className={`mode-badge mode-${mode}`} title={modeInfo.hint}>
            <span className="visually-hidden">動作モード: </span>
            {modeInfo.label}
          </span>
        )}
        {userLabel && <span className="user-label">{userLabel}</span>}
        {view === 'chat' ? (
          <button type="button" className="btn btn-secondary" onClick={onNewConversation}>
            新しい会話
          </button>
        ) : (
          onAsk && (
            <button type="button" className="btn btn-primary" onClick={onAsk}>
              質問する
            </button>
          )
        )}
        {onSignOut && (
          <button type="button" className="btn btn-secondary" onClick={onSignOut}>
            ログアウト
          </button>
        )}
      </div>
    </header>
  );
}
