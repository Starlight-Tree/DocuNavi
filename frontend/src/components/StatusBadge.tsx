export type BadgeStatus = 'processing' | 'ready' | 'failed';

export const STATUS_LABEL: Record<BadgeStatus, string> = {
  ready: '利用可能',
  processing: '取り込み中',
  failed: 'エラー',
};

/** 資料の状態の表示。色だけに頼らず、記号と文字を添える。 */
export function StatusBadge({ status }: { status: BadgeStatus }) {
  const glyph = status === 'ready' ? '✓' : status === 'failed' ? '!' : '';
  return (
    <span className={`ws-status ws-status-${status}`}>
      {status === 'processing' ? <span className="ws-spinner" aria-hidden="true" /> : <span aria-hidden="true">{glyph}</span>}
      {STATUS_LABEL[status]}
    </span>
  );
}
