import type { Loaded } from '../hooks/useDocuments';

/** 読み込み中・失敗（再試行つき）の表示。読み込めたとき（ready）は何も出さない。 */
export function Status<T>({ state, onRetry }: { state: Loaded<T>; onRetry: () => void }) {
  if (state.status === 'loading') return <p role="status">読み込み中…</p>;
  if (state.status === 'error') {
    return (
      <div className="documents-error" role="alert">
        <p>{state.message}</p>
        <button type="button" className="btn btn-secondary" onClick={onRetry}>
          再試行
        </button>
      </div>
    );
  }
  return null;
}
