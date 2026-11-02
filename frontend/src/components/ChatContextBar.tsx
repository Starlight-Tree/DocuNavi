interface ChatContextBarProps {
  /** 質問の対象の表示名（「すべての資料」または資料グループの表示名）。 */
  targetLabel: string;
  /** 対象の資料の数。分からないとき（グループの一覧を読めなかった、会話の範囲が不明）は渡さない。 */
  documentCount?: number | undefined;
  /** 資料一覧へ戻る。 */
  onBack: () => void;
}

/** チャットの上に固定する、いま何について質問しているかの帯と、資料一覧へ戻る導線。範囲を変える操作は、入力欄の上のチップが持つ。 */
export function ChatContextBar({ targetLabel, documentCount, onBack }: ChatContextBarProps) {
  return (
    <div className="chat-context-bar">
      <button type="button" className="btn-link chat-context-back" onClick={onBack}>
        <span aria-hidden="true">‹ </span>資料一覧へ
      </button>
      <span className="chat-context-target">
        <span className="chat-context-chip">
          {targetLabel}
          {documentCount !== undefined && `（${documentCount}件）`}
        </span>
        について質問
      </span>
    </div>
  );
}
