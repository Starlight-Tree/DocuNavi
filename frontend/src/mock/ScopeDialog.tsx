import { useMemo, useState } from 'react';
import { Modal } from './Modal';
import { StatusBadge } from './StatusBadge';
import { UNSELECTABLE_REASON } from './data';
import { useMock } from './MockProvider';
import { activeThread, docById } from './store';
import type { Scope } from './types';

/**
 * 質問対象の選択。「すべての資料」は取り込み済みだけが対象。個別に選べるのは取り込み済みの資料だけ
 * （取り込み中・エラーは理由つきで選べない。ただし、この会話から追加して取り込み待ちの資料は、予約として残す）。
 */
export function ScopeDialog({ onClose }: { onClose: () => void }) {
  const { state, dispatch } = useMock();
  const thread = activeThread(state);
  const initial = thread.scope;
  const [mode, setMode] = useState<'all' | 'selected'>(initial.mode);
  const [picked, setPicked] = useState<Set<string>>(
    () => new Set(initial.mode === 'selected' ? initial.ids.filter((id) => docById(state, id)) : []),
  );
  const [query, setQuery] = useState('');

  const docs = useMemo(() => {
    const q = query.trim().toLowerCase();
    return [...state.docs].filter((d) => q === '' || d.name.toLowerCase().includes(q)).sort((a, b) => b.addedAt.localeCompare(a.addedAt));
  }, [state.docs, query]);

  const readyCount = state.docs.filter((d) => d.status === 'ready').length;
  const selectedDocs = [...picked].flatMap((id) => docById(state, id) ?? []);
  const waiting = selectedDocs.filter((d) => d.status === 'processing').length;
  const canApply = mode === 'all' || picked.size > 0;
  const selectable = (status: string) => status === 'ready';

  const toggle = (id: string) =>
    setPicked((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });

  const apply = () => {
    if (!canApply) return;
    const scope: Scope = mode === 'all' ? { mode: 'all' } : { mode: 'selected', ids: [...picked] };
    dispatch({ type: 'setScope', threadId: thread.id, scope, now: Date.now() });
  };

  return (
    <Modal title="質問対象の資料" description="質問に使う資料を選びます。取り込み中・エラーの資料は選べません。" width="wide" onClose={onClose}>
      <div className="mk-modal-body">
        <fieldset className="mk-radios">
          <legend className="visually-hidden">質問対象の種類</legend>
          <label className="mk-radio">
            <input type="radio" name="mk-scope-mode" checked={mode === 'all'} onChange={() => setMode('all')} />
            <span>
              <strong>すべての資料</strong>
              <span className="mk-hint">取り込み済みの資料 {readyCount}件が対象です（取り込み中・エラーは含みません）</span>
            </span>
          </label>
          <label className="mk-radio">
            <input type="radio" name="mk-scope-mode" checked={mode === 'selected'} onChange={() => setMode('selected')} />
            <span>
              <strong>資料を選択</strong>
              <span className="mk-hint">質問に使う資料を個別に選びます</span>
            </span>
          </label>
        </fieldset>

        <div className="mk-field">
          <label htmlFor="mk-scope-search">資料名で検索</label>
          <input id="mk-scope-search" type="search" className="mk-text" value={query} onChange={(event) => setQuery(event.target.value)} />
        </div>

        {mode === 'selected' && (
          <div className="mk-bulk">
            <button
              type="button"
              className="mk-link"
              onClick={() => setPicked((prev) => new Set([...prev, ...docs.filter((d) => selectable(d.status)).map((d) => d.id)]))}
            >
              表示中の利用可能な資料を選択
            </button>
            <button
              type="button"
              className="mk-link"
              onClick={() => setPicked((prev) => new Set([...prev].filter((id) => !docs.some((d) => d.id === id && selectable(d.status)))))}
            >
              表示中の選択を解除
            </button>
          </div>
        )}

        {docs.length === 0 ? (
          <p className="mk-note">条件に合う資料がありません。</p>
        ) : (
          <ul className="mk-scope-list" aria-label="資料">
            {docs.map((doc) => {
              const reserved = picked.has(doc.id) && doc.status === 'processing';
              const disabled = mode === 'all' || !selectable(doc.status);
              const reason = doc.status === 'ready' ? null : UNSELECTABLE_REASON[doc.status];
              return (
                <li key={doc.id} className="mk-scope-item" data-disabled={disabled ? 'true' : undefined}>
                  <label>
                    <input
                      type="checkbox"
                      checked={mode === 'selected' && picked.has(doc.id)}
                      disabled={disabled}
                      aria-describedby={reason ? `mk-reason-${doc.id}` : undefined}
                      onChange={() => toggle(doc.id)}
                    />
                    <span className="mk-scope-name">{doc.name}</span>
                    <StatusBadge status={doc.status} />
                  </label>
                  {reason && (
                    <p id={`mk-reason-${doc.id}`} className="mk-reason">
                      {reserved ? '選択に入っています。取り込みが終わるまで質問には使えません。' : reason}
                    </p>
                  )}
                </li>
              );
            })}
          </ul>
        )}
      </div>
      <footer className="mk-modal-foot">
        <p className="mk-count-line" role="status">
          {mode === 'all' ? `すべての資料（${readyCount}件）` : `${picked.size}件を選択中${waiting > 0 ? `（取り込み中 ${waiting}件を含む）` : ''}`}
          {!canApply && ' ・ 1件以上選ぶと適用できます'}
        </p>
        <button type="button" className="btn btn-secondary" onClick={onClose}>
          キャンセル
        </button>
        <button type="button" className="btn btn-primary" disabled={!canApply} onClick={apply}>
          適用
        </button>
      </footer>
    </Modal>
  );
}
