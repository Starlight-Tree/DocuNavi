import { useState } from 'react';
import type { SourceDoc } from '../lib/sources';
import { useOpenSource } from './useDocuments';
import type { OpenSourceResult } from './useDocuments';

export const MSG_NOT_FOUND = '資料一覧に見つかりませんでした。';
export const MSG_ERROR = '資料を開けませんでした。しばらく待ってから、もう一度お試しください。';

/**
 * 参照資料のカードから、資料のパネルを開く。開いている最中のカード（`opening`、`sources` の添字）と、失敗の種類を持つ。
 * 回答の下のカードと、参照資料の専用ページが同じ開き方を使う。
 */
export function useOpenReference(sources: SourceDoc[]) {
  const openSource = useOpenSource();
  const [opening, setOpening] = useState<number | null>(null);
  const [failure, setFailure] = useState<Exclude<OpenSourceResult, 'opened'> | null>(null);

  const open = async (index: number, span: string | null = null, from?: Element | null) => {
    const source = sources[index];
    if (!source || opening !== null) return;
    // 押した要素を覚える（待っている間にボタンが無効になっても、閉じたときに戻す先を失わない）
    const opener = from ?? document.activeElement;
    setOpening(index);
    setFailure(null);
    const result = await openSource(source.name, source.text, source.page, opener, { documentId: source.documentId, span });
    setOpening(null);
    if (result !== 'opened') setFailure(result);
  };

  return { opening, failure, failureMessage: failure === null ? null : failure === 'notfound' ? MSG_NOT_FOUND : MSG_ERROR, open };
}
