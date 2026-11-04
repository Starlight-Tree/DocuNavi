/**
 * 回答に使う資料の選択（資料単位）。React にも通信にも依存しない。
 *
 * - 選択は「資料 ID の集合」か、null（資料で絞らない）。null のとき、どの資料にチェックが付くかは、会話の範囲（すべて / 対象 / 旧グループ）が決める。
 * - 選べるのは、検索できる資料（`ready`）だけ。取り込み中・失敗の資料は、選択に入らない。
 * - 一覧の検索・絞り込みは、選択を変えない。全選択・解除は、いま表示している資料だけに効く。
 */
import type { DocumentSummary } from '../api/documents';

/** 会話の範囲（`ChatScope` と同じ形。lib は hooks に依存しないので、ここで形だけ持つ）。 */
export type ScopeShape = { kind: 'all' } | { kind: 'target' | 'collection'; name: string };

/** 回答に使える資料か（検索の対象になるのは `ready` だけ）。 */
export function isSelectable(document: DocumentSummary): boolean {
  return document.status === 'ready';
}

/** 選択が無いとき（null）に、その範囲で回答に使われる資料か。範囲が分からない会話（`unknown`）は「すべて」として扱う。 */
function inScope(document: DocumentSummary, scope: ScopeShape, unknown: boolean): boolean {
  if (unknown || scope.kind === 'all') return true;
  return scope.kind === 'target' ? document.target === scope.name : document.collection === scope.name;
}

/** いま回答に使われる資料の ID（検索できる資料だけ）。`selection` が null なら、会話の範囲で決まる。 */
export function effectiveSelection(
  documents: readonly DocumentSummary[],
  scope: ScopeShape,
  unknown: boolean,
  selection: readonly number[] | null,
): Set<number> {
  const chosen = selection === null ? null : new Set(selection);
  const result = new Set<number>();
  for (const document of documents) {
    if (!isSelectable(document)) continue;
    if (chosen === null ? inScope(document, scope, unknown) : chosen.has(document.id)) result.add(document.id);
  }
  return result;
}

/**
 * 画面で作った選択（ID の集合）を、保存する形に直す。範囲が「すべて」で、検索できる資料のすべてが選ばれているときは、
 * 「資料で絞らない」（null）に戻す。対象・旧グループの会話では、集合のまま持つ（null にすると、別の資料に変わるため）。
 */
export function normalizeSelection(
  ids: ReadonlySet<number>,
  documents: readonly DocumentSummary[],
  scope: ScopeShape,
  unknown: boolean,
): number[] | null {
  const selectable = documents.filter(isSelectable);
  const everything = selectable.length > 0 && selectable.every((d) => ids.has(d.id));
  if (everything && (unknown || scope.kind === 'all')) return null;
  return [...ids].sort((a, b) => a - b);
}

/** 表示中の資料（`shown`）のチェックを、まとめて付ける・外す。表示していない資料の選択は変えない。取り込み中・失敗の資料は入れない。 */
export function setShown(current: ReadonlySet<number>, shown: readonly DocumentSummary[], checked: boolean): Set<number> {
  const next = new Set(current);
  for (const document of shown) {
    if (!isSelectable(document)) continue;
    if (checked) next.add(document.id);
    else next.delete(document.id);
  }
  return next;
}

/** 1 件のチェックを付ける・外す。 */
export function toggleOne(current: ReadonlySet<number>, id: number): Set<number> {
  const next = new Set(current);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  return next;
}

export interface ShownState {
  /** 表示中の、選べる資料の数。 */
  selectable: number;
  /** そのうち、チェックが付いている数。 */
  checked: number;
}

/** 見出し行のチェック（全選択・解除）の状態。表示中の資料だけで数える。 */
export function shownState(shown: readonly DocumentSummary[], selected: ReadonlySet<number>): ShownState {
  const selectable = shown.filter(isSelectable);
  return { selectable: selectable.length, checked: selectable.filter((d) => selected.has(d.id)).length };
}
