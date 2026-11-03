import { useEffect } from 'react';
import type { DocumentList } from '../api/documents';
import type { Loaded } from '../hooks/useLoaded';
import { takePanelOpener } from '../lib/panelSignal';
import { useUrlState } from '../lib/urlState';
import { DocumentPanel, PANEL_ID } from './DocumentPanel';
import { Status } from './Status';

interface PanelHostProps {
  /** 資料の一覧（GET /documents）。開く資料は、ここにあるものだけ（権限はサーバーが決めた一覧に従う）。 */
  list: Loaded<DocumentList>;
  onReload: () => void;
  expanded: boolean;
  onToggleExpand: () => void;
  /** 閉じたとき（拡大の解除など）。 */
  onClosed: () => void;
}

/**
 * URL の `doc` に合わせて、右のパネルを出す。チャットでも資料一覧でも、同じ場所に出る（タブを移っても作り直さない）。
 * 一覧に無い資料の ID（消えた・権限が無い・手で書き換えた）は、履歴を増やさずに閉じる。
 */
export function PanelHost({ list, onReload, expanded, onToggleExpand, onClosed }: PanelHostProps) {
  const { doc, view, page, chunk, navigate } = useUrlState();
  const selected = doc !== null && list.status === 'ready' ? (list.data.documents.find((d) => d.id === doc) ?? null) : null;
  const unknownDoc = doc !== null && list.status === 'ready' && selected === null;

  useEffect(() => {
    if (unknownDoc) navigate({ doc: null }, 'replace');
  }, [unknownDoc, navigate]);

  // 閉じる。開く前に押していた要素が残っていれば、フォーカスを戻す
  const close = () => {
    navigate({ doc: null });
    onClosed();
    const opener = takePanelOpener();
    if (opener?.isConnected) opener.focus();
  };

  if (doc === null || unknownDoc) return null;
  if (selected === null) {
    // 一覧を取っている間・取れなかったとき。一覧が取れないと資料を開けないので、その旨と再試行を出す
    return (
      <aside id={PANEL_ID} className="ws-panel" aria-label="資料" tabIndex={-1}>
        <header className="ws-panel-head">
          <span className="ws-eyebrow">資料</span>
          <button type="button" className="ws-icon-btn" aria-label="パネルを閉じる" onClick={close}>
            ×
          </button>
        </header>
        <Status state={list} onRetry={onReload} />
      </aside>
    );
  }

  return (
    <DocumentPanel
      key={selected.id}
      document={selected}
      view={view}
      page={page}
      chunk={chunk}
      expanded={expanded}
      onToggleExpand={onToggleExpand}
      onViewChange={(next) => navigate({ view: next })}
      onClose={close}
    />
  );
}
