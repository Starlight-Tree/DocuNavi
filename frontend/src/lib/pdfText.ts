/**
 * PDF の文字（PDF.js の text layer の項目）の中から、出典の根拠の位置を探す。探すのは、根拠の語句、または根拠のチャンクの本文全体。
 *
 * これらは、サーバーが PDF から取り出した本文（pypdf）の一部。画面の PDF は PDF.js が文字を取り出すので、
 * 改行・空白・全角半角・合字の扱いが違う。そこで両方を、NFKC で正規化し、空白をすべて除いてから照合する。
 * 見つけた範囲は、元の項目の文字位置に戻して返す（画面側が、その項目の中だけを強調する）。
 *
 * 誤った箇所を強調しないことを優先する: 全体がそのまま 1 か所に見つかるときだけ返す。見つからない・複数ある（どれか分からない）ときは何も返さない。
 * 実 PDF（評価用の 6 冊・203 チャンク）では、チャンク全体が 199 件で 1 か所に見つかった。残りは、表の文字の並びが PDF.js と違うもの。
 */

/** 項目（text layer の 1 つの文字列）の中の、強調する範囲。`start` `end` は項目の文字列の位置（UTF-16 の単位）で、`[start, end)`。 */
export interface PdfSpanPart {
  /** 項目の番号（`texts` の添字） */
  item: number;
  start: number;
  end: number;
}

/** 短すぎると、無関係な箇所に当たる。空白を除いたあとの最小の長さ。 */
const MIN_SPAN_LENGTH = 2;

interface Owner {
  item: number;
  start: number;
  end: number;
}

/** 正規化した文字列（空白なし）と、その各文字（UTF-16 の単位）が元のどの項目のどの範囲から来たか。 */
function flatten(texts: readonly string[]): { text: string; owners: Owner[] } {
  let text = '';
  const owners: Owner[] = [];
  texts.forEach((value, item) => {
    let offset = 0;
    for (const ch of value) {
      const start = offset;
      offset += ch.length;
      for (const unit of ch.normalize('NFKC')) {
        if (/\s/.test(unit)) continue;
        text += unit;
        for (let i = 0; i < unit.length; i += 1) owners.push({ item, start, end: offset });
      }
    }
  });
  return { text, owners };
}

function normalizeSpan(span: string): string {
  let out = '';
  for (const ch of span.normalize('NFKC')) {
    if (!/\s/.test(ch)) out += ch;
  }
  return out;
}

function partsOf(owners: readonly Owner[], from: number, length: number): PdfSpanPart[] {
  const parts: PdfSpanPart[] = [];
  for (let i = from; i < from + length; i += 1) {
    const owner = owners[i];
    if (owner === undefined) continue;
    const last = parts[parts.length - 1];
    if (last !== undefined && last.item === owner.item) {
      last.start = Math.min(last.start, owner.start);
      last.end = Math.max(last.end, owner.end);
    } else {
      parts.push({ item: owner.item, start: owner.start, end: owner.end });
    }
  }
  return parts;
}

/**
 * ページの文字（`texts`。項目の順）から、`span`（根拠の語句、またはチャンクの本文）の範囲を探す。
 * 見つからない・複数ある・短すぎるときは null（何も強調しない）。
 */
export function locatePdfSpan(texts: readonly string[], span: string): PdfSpanPart[] | null {
  const target = normalizeSpan(span);
  if (target.length < MIN_SPAN_LENGTH) return null;
  const { text, owners } = flatten(texts);
  const at = text.indexOf(target);
  if (at < 0 || text.indexOf(target, at + 1) >= 0) return null;
  const parts = partsOf(owners, at, target.length);
  return parts.length > 0 ? parts : null;
}
