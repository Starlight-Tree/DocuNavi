export type View = 'chat' | 'documents';

interface HeaderProps {
  mode: string | null;
  /** いま開いている画面。onViewChange と一緒に渡すと、「チャット / 資料一覧」の切り替えを出す。 */
  view?: View | undefined;
  onViewChange?: ((view: View) => void) | undefined;
  onNewConversation: () => void;
  /** ログイン中の利用者の表示名。ログインを使わない構成では渡さない。 */
  userLabel?: string | undefined;
  onSignOut?: (() => void) | undefined;
}

const VIEWS: Array<{ id: View; label: string }> = [
  { id: 'chat', label: 'チャット' },
  { id: 'documents', label: '資料一覧' },
];

const MODE_LABELS: Record<string, { label: string; hint: string }> = {
  mock: { label: 'モック', hint: 'LLM を呼ばないダミー応答で動作しています' },
  gemini: { label: 'Gemini', hint: 'Gemini（本物の LLM）で動作しています' },
};

export function Header({ mode, view = 'chat', onViewChange, onNewConversation, userLabel, onSignOut }: HeaderProps) {
  const modeInfo = mode ? (MODE_LABELS[mode] ?? { label: mode, hint: `モード: ${mode}` }) : null;

  return (
    <header className="app-header">
      <h1 className="app-title">ドキュナビ</h1>
      {onViewChange && (
        <nav className="view-tabs" aria-label="表示する画面">
          {VIEWS.map(({ id, label }) => (
            <button
              key={id}
              type="button"
              className="view-tab"
              aria-current={view === id ? 'page' : undefined}
              onClick={() => onViewChange(id)}
            >
              {label}
            </button>
          ))}
        </nav>
      )}
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
          // 資料一覧では押せないが、場所は空けておく（画面を切り替えても、ヘッダーの高さとタブの位置が動かない）
          <span className="btn btn-secondary header-placeholder" aria-hidden="true">
            新しい会話
          </span>
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
