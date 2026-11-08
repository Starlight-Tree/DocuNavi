import { isVerifiedSpan } from './sources';
import type { SourceDoc } from './sources';

/**
 * 回答の文末の引用 `[1]` / `[1「根拠の語句」]` を読む。純粋な関数だけ（React に依存しない）。
 *
 * LLM が文末に付ける番号は、検索結果の見出し「[番号] ファイル名」の番号で、画面の出典（`sources`）の並びの n 番目を指す。
 * LLM の出力は信用しない: 存在しない番号・参照資料に出ない出典（`weak`）の番号は消す。
 * 根拠の語句がある引用は、語句が出典の本文にそのまま含まれるときだけ残し、含まれなければ番号ごと消す
 * （作り話の語句を、根拠があるように見せない。チャンク単位の強調にも戻さない）。語句の無い `[n]` は、番号だけの引用として残す。
 * 「その文を出典が支持するか」までは、ここでは確かめられない（番号が実在するかまで）。
 */

export interface CitationRef {
  /** 画面に出す番号（参照資料のカードの番号と同じ）。同じ根拠でまとめられた出典は、最初の 1 件の番号。 */
  number: number;
  /** 出典の添字（`n - 1` ではなく、まとめた先の出典）。 */
  index: number;
  /** 出典の本文に含まれると確かめた根拠の語句。無ければ null。 */
  span: string | null;
}

export type CitationPart = string | { type: 'cite'; refs: CitationRef[] };

// 1 つの引用。番号は 1〜3 桁、語句は 1 行・200 文字まで（暴走した出力で、本文を飲み込まない）
const MARKER = /\[(\d{1,3})(?:「([^」\n]{1,200})」)?\]/g;
// 生成の途中の、閉じていない引用（末尾の `[` `[1` `[1「語` `[1「語」`）
const OPEN_TAIL = /\[(?:\d{1,3}(?:「[^」\n]*」?)?)?$/;

/** 生成の途中の本文から、末尾の閉じていない引用を除く（`[1「…` が一瞬だけ文字として見えるのを防ぐ）。 */
export function hideOpenCitation(text: string): string {
  return text.replace(OPEN_TAIL, '');
}

/** 引用を消した本文（コピー用）。引用は、画面の出典と対になるもので、コピーした文章には要らない。 */
export function stripCitations(text: string): string {
  return text.replace(/\[\d{1,3}(?:「[^」\n]{1,200}」)?\]/g, '');
}

/**
 * 1 つのテキストを、本文と引用に分ける。`sources` は出典の全件（並びのまま）、`targets` は `referenceTargets` の結果。
 * 使えない引用（範囲外の番号・`weak`・原文に無い語句）は、本文にも出さず消す。
 */
export function splitCitations(text: string, sources: SourceDoc[], targets: ReadonlyMap<number, number>): CitationPart[] {
  const parts: CitationPart[] = [];
  let cursor = 0;
  let group: CitationRef[] | null = null;
  let groupEnd = -1;
  const flushGroup = () => {
    if (group && group.length > 0) parts.push({ type: 'cite', refs: group });
    group = null;
  };

  for (const match of text.matchAll(MARKER)) {
    const start = match.index ?? 0;
    const end = start + match[0].length;
    const number = Number(match[1]);
    const index = number >= 1 ? targets.get(number - 1) : undefined;
    const source = index === undefined ? undefined : sources[number - 1];
    // 隣り合う引用（[1][2]）は、1 つのまとまりにする。間に本文があれば別のまとまり
    if (group && start !== groupEnd) flushGroup();
    if (!group) {
      if (start > cursor) parts.push(text.slice(cursor, start));
      group = [];
    }
    cursor = end;
    groupEnd = end;
    if (index === undefined || !source) continue;
    if (match[2] !== undefined && !isVerifiedSpan(match[2], source.text)) continue; // 原文に無い語句は番号ごと消す
    const span = match[2] ?? null;
    const ref: CitationRef = { number: index + 1, index, span };
    if (!group.some((r) => r.index === ref.index && r.span === ref.span)) group.push(ref);
  }
  flushGroup();
  if (cursor < text.length) parts.push(text.slice(cursor));
  return parts;
}
