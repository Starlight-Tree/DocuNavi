import { STATUS_LABEL } from './data';
import type { DocStatus } from './types';

/** 状態の表示。色だけに頼らず、記号と文字を添える。 */
export function StatusBadge({ status }: { status: DocStatus }) {
  const glyph = status === 'ready' ? '✓' : status === 'failed' ? '!' : '';
  return (
    <span className={`mk-status mk-status-${status}`}>
      {status === 'processing' ? <span className="mk-spinner" aria-hidden="true" /> : <span aria-hidden="true">{glyph}</span>}
      {STATUS_LABEL[status]}
    </span>
  );
}
