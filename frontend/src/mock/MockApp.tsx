import { useEffect, useRef } from 'react';
import { ChatView } from './ChatView';
import { DocumentPanel, PANEL_ID } from './DocumentPanel';
import { DocumentsView } from './DocumentsView';
import { MockProvider, useMock } from './MockProvider';
import { ScopeDialog } from './ScopeDialog';
import { DeleteDialog, SettingsDialog } from './SimpleDialogs';
import { Sidebar } from './Sidebar';
import { UploadDialog } from './UploadDialog';
import type { View } from './types';
import './mock.css';

/** ?view=documents で、資料画面から開ける（再読み込みしても、いまの画面に戻れる）。 */
function readInitialView(): View {
  if (typeof window === 'undefined') return 'chat';
  return new URLSearchParams(window.location.search).get('view') === 'documents' ? 'documents' : 'chat';
}

function Shell() {
  const { state, dispatch } = useMock();
  const { view, panel, dialog } = state;

  // いまの画面を URL に残す（履歴は増やさない）
  useEffect(() => {
    const url = new URL(window.location.href);
    if (view === 'documents') url.searchParams.set('view', 'documents');
    else url.searchParams.delete('view');
    window.history.replaceState(null, '', url);
  }, [view]);

  // 右パネル: 開いたらパネルへフォーカスを移し、閉じたら開いた要素へ戻す
  const panelOpen = panel !== null;
  const panelKey = panel ? `${panel.docId}|${panel.focus?.page ?? ''}|${panel.focus?.para ?? ''}` : '';
  const opener = useRef<HTMLElement | null>(null);
  const wasOpen = useRef(false);
  useEffect(() => {
    if (panelOpen && !wasOpen.current) {
      opener.current = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    } else if (!panelOpen && wasOpen.current) {
      if (opener.current?.isConnected) opener.current.focus();
      opener.current = null;
    }
    wasOpen.current = panelOpen;
  }, [panelOpen]);
  useEffect(() => {
    if (panelKey) document.getElementById(PANEL_ID)?.focus({ preventScroll: true });
  }, [panelKey]);

  const close = () => dispatch({ type: 'closeDialog' });

  return (
    <>
      <div className={panel ? 'mk-shell mk-shell-panel' : 'mk-shell'} inert={dialog !== null}>
        <Sidebar />
        <main className="mk-main">
          <div className="mk-view" hidden={view !== 'chat'}>
            <ChatView />
          </div>
          {view === 'documents' && (
            <div className="mk-view">
              <DocumentsView />
            </div>
          )}
        </main>
        {panel && <DocumentPanel key={panelKey} target={panel} />}
      </div>

      <div className="mk-toasts" role="status" aria-live="polite">
        {state.toasts.map((toast) => (
          <p key={toast.id} className={`mk-toast mk-toast-${toast.tone}`}>
            <span>{toast.text}</span>
            <button type="button" className="mk-icon-btn" aria-label="通知を閉じる" onClick={() => dispatch({ type: 'dismissToast', id: toast.id })}>
              ×
            </button>
          </p>
        ))}
      </div>

      {dialog?.type === 'upload' && <UploadDialog origin={dialog.origin} onClose={close} />}
      {dialog?.type === 'scope' && <ScopeDialog onClose={close} />}
      {dialog?.type === 'delete' && <DeleteDialog docId={dialog.docId} onClose={close} />}
      {dialog?.type === 'settings' && <SettingsDialog onClose={close} />}
    </>
  );
}

/** UI モックの入口。サンプルデータだけで動き、通信・保存・認証には触れない。 */
export default function MockApp() {
  return (
    <MockProvider initialView={readInitialView()}>
      <Shell />
    </MockProvider>
  );
}
