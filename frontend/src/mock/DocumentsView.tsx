import { useEffect, useId, useMemo, useRef, useState } from 'react';
import type { KeyboardEvent } from 'react';
import { STATUS_LABEL, StatusBadge } from '../components/StatusBadge';
import { formatDateTime } from './data';
import { useMock } from './MockProvider';
import type { DocStatus, MockDoc } from './types';

type StatusFilter = 'all' | DocStatus;

const FILTERS: Array<{ value: StatusFilter; label: string }> = [
  { value: 'all', label: 'すべての状態' },
  { value: 'ready', label: STATUS_LABEL.ready },
  { value: 'processing', label: STATUS_LABEL.processing },
  { value: 'failed', label: STATUS_LABEL.failed },
];

/** 検索語と状態で絞り、追加日時の新しい順に並べる。 */
export function filterDocs(docs: MockDoc[], query: string, status: StatusFilter): MockDoc[] {
  const q = query.trim().toLowerCase();
  return docs
    .filter((d) => (status === 'all' || d.status === status) && (q === '' || d.name.toLowerCase().includes(q)))
    .sort((a, b) => b.addedAt.localeCompare(a.addedAt));
}

interface MenuAction {
  key: string;
  label: string;
  hint?: string;
  disabled?: boolean;
  danger?: boolean;
  run: () => void;
}

/** 行の操作メニュー。画面の端で切れないよう、押した位置から fixed で出す。矢印キーで動き、Esc で閉じて元のボタンへ戻る。 */
function RowMenu({ doc, actions }: { doc: MockDoc; actions: MenuAction[] }) {
  const [pos, setPos] = useState<{ top: number; right: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const menuRef = useRef<HTMLDivElement>(null);
  const menuId = useId();
  const open = pos !== null;

  const close = (restore: boolean) => {
    setPos(null);
    if (restore) buttonRef.current?.focus();
  };

  useEffect(() => {
    if (!open) return;
    menuRef.current?.querySelector<HTMLElement>('[role="menuitem"]:not([disabled])')?.focus();
    const onPointer = (event: MouseEvent) => {
      const target = event.target as Node;
      if (!menuRef.current?.contains(target) && !buttonRef.current?.contains(target)) setPos(null);
    };
    const onDismiss = () => setPos(null);
    document.addEventListener('mousedown', onPointer);
    window.addEventListener('resize', onDismiss);
    window.addEventListener('scroll', onDismiss, true);
    return () => {
      document.removeEventListener('mousedown', onPointer);
      window.removeEventListener('resize', onDismiss);
      window.removeEventListener('scroll', onDismiss, true);
    };
  }, [open]);

  const toggle = () => {
    if (open) {
      close(false);
      return;
    }
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) setPos({ top: rect.bottom + 4, right: window.innerWidth - rect.right });
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    const items = [...(menuRef.current?.querySelectorAll<HTMLElement>('[role="menuitem"]:not([disabled])') ?? [])];
    const index = items.findIndex((el) => el === document.activeElement);
    if (event.key === 'Escape') {
      event.preventDefault();
      event.stopPropagation();
      close(true);
    } else if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      event.preventDefault();
      const step = event.key === 'ArrowDown' ? 1 : -1;
      items[(index + step + items.length) % items.length]?.focus();
    } else if (event.key === 'Home' || event.key === 'End') {
      event.preventDefault();
      (event.key === 'Home' ? items[0] : items[items.length - 1])?.focus();
    } else if (event.key === 'Tab') {
      close(false);
    }
  };

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        className="ws-icon-btn ws-row-menu-btn"
        aria-label={`「${doc.name}」の操作`}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-controls={open ? menuId : undefined}
        onClick={toggle}
      >
        ⋯
      </button>
      {pos && (
        <div
          ref={menuRef}
          id={menuId}
          role="menu"
          aria-label={`「${doc.name}」の操作`}
          className="ws-menu"
          style={{ top: pos.top, right: pos.right }}
          onKeyDown={handleKeyDown}
        >
          {actions.map((action) => (
            <button
              key={action.key}
              type="button"
              role="menuitem"
              className={action.danger ? 'ws-menu-item ws-menu-danger' : 'ws-menu-item'}
              disabled={action.disabled}
              onClick={() => {
                // ダイアログが閉じたとき、この行のボタンへ戻れるよう、先にフォーカスを戻す
                close(true);
                action.run();
              }}
            >
              {action.label}
              {action.hint && <span className="ws-menu-hint">{action.hint}</span>}
            </button>
          ))}
        </div>
      )}
    </>
  );
}

export function DocumentsView() {
  const { state, dispatch } = useMock();
  const [query, setQuery] = useState('');
  const [status, setStatus] = useState<StatusFilter>('all');
  const rows = useMemo(() => filterDocs(state.docs, query, status), [state.docs, query, status]);
  const total = state.docs.length;
  const filtered = query.trim() !== '' || status !== 'all';

  const open = (doc: MockDoc) => dispatch({ type: 'openPanel', target: { docId: doc.id, docName: doc.name } });
  const actionsFor = (doc: MockDoc): MenuAction[] => [
    { key: 'open', label: '詳細を開く', run: () => open(doc) },
    {
      key: 'ask',
      label: 'この資料で質問',
      disabled: doc.status !== 'ready',
      ...(doc.status !== 'ready' ? { hint: doc.status === 'processing' ? '取り込み完了後に使えます' : 'エラーのため使えません' } : {}),
      run: () => dispatch({ type: 'askWithDoc', docId: doc.id, now: Date.now() }),
    },
    ...(doc.status === 'failed'
      ? [{ key: 'retry', label: '再試行', run: () => dispatch({ type: 'retry', docId: doc.id, now: Date.now() }) }]
      : []),
    { key: 'delete', label: '削除…', danger: true, run: () => dispatch({ type: 'openDialog', dialog: { type: 'delete', docId: doc.id } }) },
  ];

  return (
    <div className="ws-docs">
      <header className="ws-docs-head">
        <h1 className="ws-page-title">資料</h1>
        <button type="button" className="btn btn-primary" onClick={() => dispatch({ type: 'openDialog', dialog: { type: 'upload', origin: 'documents' } })}>
          ＋ 資料を追加
        </button>
      </header>

      <div className="ws-toolbar">
        <div className="ws-field">
          <label htmlFor="ws-doc-search">資料名で検索</label>
          <input id="ws-doc-search" type="search" className="ws-text" value={query} placeholder="例: 契約" onChange={(event) => setQuery(event.target.value)} />
        </div>
        <div className="ws-field">
          <label htmlFor="ws-doc-status">状態</label>
          <select id="ws-doc-status" className="ws-text" value={status} onChange={(event) => setStatus(event.target.value as StatusFilter)}>
            {FILTERS.map((f) => (
              <option key={f.value} value={f.value}>
                {f.label}
              </option>
            ))}
          </select>
        </div>
        <p className="ws-result-count" role="status">
          {total === 0 ? '' : `${total}件中 ${rows.length}件を表示`}
        </p>
      </div>

      {total === 0 ? (
        <div className="ws-empty-state">
          <p className="ws-empty-title">まだ資料がありません</p>
          <p>資料を追加すると、取り込みのあとで、チャットから質問できます。</p>
          <button type="button" className="btn btn-primary" onClick={() => dispatch({ type: 'openDialog', dialog: { type: 'upload', origin: 'documents' } })}>
            ＋ 資料を追加
          </button>
        </div>
      ) : rows.length === 0 ? (
        <div className="ws-empty-state">
          <p className="ws-empty-title">条件に合う資料がありません</p>
          <p>検索語や状態を変えてみてください。</p>
          {filtered && (
            <button
              type="button"
              className="btn btn-secondary"
              onClick={() => {
                setQuery('');
                setStatus('all');
              }}
            >
              条件をクリア
            </button>
          )}
        </div>
      ) : (
        <div className="ws-table-wrap">
          <table className="ws-table">
            <caption className="visually-hidden">登録済みの資料（追加日時の新しい順）</caption>
            <thead>
              <tr>
                <th scope="col">資料名</th>
                <th scope="col">ファイル種別</th>
                <th scope="col">状態</th>
                <th scope="col">追加日時</th>
                <th scope="col" className="ws-col-actions">
                  <span className="visually-hidden">操作</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((doc) => {
                const selected = state.panel?.docId === doc.id;
                return (
                  <tr key={doc.id} data-selected={selected ? 'true' : undefined}>
                    <th scope="row">
                      <button type="button" className="ws-link" aria-current={selected ? 'true' : undefined} onClick={() => open(doc)}>
                        {doc.name}
                      </button>
                    </th>
                    <td>{doc.kind}</td>
                    <td>
                      <StatusBadge status={doc.status} />
                      {doc.status === 'failed' && (
                        <span className="ws-row-error">
                          {doc.failureReason ?? '取り込みに失敗しました。'}
                          <button type="button" className="ws-link ws-retry" onClick={() => dispatch({ type: 'retry', docId: doc.id, now: Date.now() })}>
                            再試行
                          </button>
                        </span>
                      )}
                    </td>
                    <td>{formatDateTime(doc.addedAt)}</td>
                    <td className="ws-col-actions">
                      <RowMenu doc={doc} actions={actionsFor(doc)} />
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
