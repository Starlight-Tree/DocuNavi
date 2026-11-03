import { useSyncExternalStore } from 'react';

/**
 * 右パネルの「利用者の操作で開いた」合図。URL には載せない（戻る・進む・再読み込みで開いたときは、フォーカスも位置も動かさない）。
 * - 開く合図（`openSeq`）: パネルへフォーカスを移す。押した要素（opener）を覚えて、閉じたときに戻す。
 * - 出典の合図（`citeSeq`）: 出典を押すたびに増える。同じ出典をもう一度押しても、保持中の位置より出典の位置を優先する（PDF を開き直す・強調へ移る）。
 */
let openSeq = 0;
let citeSeq = 0;
let opener: HTMLElement | null = null;
let pendingFocusAt = 0;
// 開く操作から、パネルが描かれる（資料の一覧を取るのを待つ）までに許す時間。過ぎた合図では、フォーカスを奪わない
const FOCUS_WINDOW_MS = 10_000;
const listeners = new Set<() => void>();

function emit() {
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** 資料を開く操作をしたとき。`from` は押した要素（無ければ、いま フォーカスのある要素）。出典なら `citation` を true にする。 */
export function signalPanelOpen(from: Element | null, citation = false): void {
  const el = from ?? document.activeElement;
  opener = el instanceof HTMLElement && el !== document.body ? el : null;
  openSeq += 1;
  pendingFocusAt = Date.now();
  if (citation) citeSeq += 1;
  emit();
}

/** 利用者の操作で開いた直後なら true（1 回だけ）。パネルが、自分へフォーカスを移すかの判定に使う。 */
export function takePendingFocus(): boolean {
  const pending = pendingFocusAt > 0 && Date.now() - pendingFocusAt < FOCUS_WINDOW_MS;
  pendingFocusAt = 0;
  return pending;
}

/** 閉じたときに戻す先（覚えていた要素）。取り出すと忘れる。 */
export function takePanelOpener(): HTMLElement | null {
  const el = opener;
  opener = null;
  return el;
}

export function usePanelOpenSeq(): number {
  return useSyncExternalStore(subscribe, () => openSeq, () => 0);
}

export function usePanelCiteSeq(): number {
  return useSyncExternalStore(subscribe, () => citeSeq, () => 0);
}
