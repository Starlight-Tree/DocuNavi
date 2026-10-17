interface HeaderProps {
  mode: string | null;
  onNewConversation: () => void;
  /** ログイン中の利用者の表示名。ログインを使わない構成では渡さない。 */
  userLabel?: string | undefined;
  onSignOut?: (() => void) | undefined;
}

const MODE_LABELS: Record<string, { label: string; hint: string }> = {
  mock: { label: 'モック', hint: 'LLM を呼ばないダミー応答で動作しています' },
  gemini: { label: 'Gemini', hint: 'Gemini（本物の LLM）で動作しています' },
};

export function Header({ mode, onNewConversation, userLabel, onSignOut }: HeaderProps) {
  const modeInfo = mode ? (MODE_LABELS[mode] ?? { label: mode, hint: `モード: ${mode}` }) : null;

  return (
    <header className="app-header">
      <h1 className="app-title">資料チャット</h1>
      <div className="app-header-actions">
        {modeInfo && (
          <span className={`mode-badge mode-${mode}`} title={modeInfo.hint}>
            <span className="visually-hidden">動作モード: </span>
            {modeInfo.label}
          </span>
        )}
        {userLabel && <span className="user-label">{userLabel}</span>}
        <button type="button" className="btn btn-secondary" onClick={onNewConversation}>
          新しい会話
        </button>
        {onSignOut && (
          <button type="button" className="btn btn-secondary" onClick={onSignOut}>
            ログアウト
          </button>
        )}
      </div>
    </header>
  );
}
