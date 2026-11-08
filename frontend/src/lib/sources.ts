import { COLLECTION_NAME_PATTERN } from '../api/collections';

export interface SourceDoc {
  /** 資料のファイル名（例: ザーハード1.txt、cafe_store_guide.pdf） */
  name: string;
  /** そのファイルから検索で拾われた本文 */
  text: string;
  /** 検索が返したページ（1 始まり）。サーバーが送ってこなかった・出典と合わなかったときは無い。 */
  page?: number;
  /** ページをまたぐ表の最終ページ。 */
  pageEnd?: number;
  /** 1 位との類似度の差が大きい 2 件目以降。出典カードには出さない（抜粋の一覧には残す）。サーバーが送らなければ無い。 */
  weak?: boolean;
  /** その資料の資料グループ（検索範囲に渡す名前）。サーバーが送ってこなかった・未設定のときは無い。 */
  collection?: string;
  /** その資料の ID（`documents.id`）。サーバーが送ってこなかった（古い応答）ときは無い。あれば、資料一覧を引かずに開ける。 */
  documentId?: number;
}

/** サーバーが `messageMetadata.sources` で送る、出典ごとのページ（context の出典と同じ順）。 */
export interface SourceRef {
  name: string;
  page: number | null;
  pageEnd: number | null;
  weak: boolean;
  /** その資料の資料グループ。未設定、または古いサーバー（送ってこない）は null。 */
  collection: string | null;
  /** その資料の ID。古いサーバー（送ってこない）・不正な値は null（省略もよい）。 */
  documentId?: number | null;
}

// バックエンドは検索結果を "[番号] ファイル名.pdf\n本文"（.txt もある）を空行で連ねた形で返す。旧形式の "[ファイル名.pdf]\n本文" も読む（保存済みの会話）。
// 「該当する設定資料は見つかりませんでした。」のような定型文は見出しを持たないので、出典にならない。
const SOURCE_HEADER = /^(?:\[\d+\] ([^\n]+\.(?:txt|pdf))|\[([^\]\n]+\.(?:txt|pdf))\])$/gm;

// 検索ツールを複数回呼ぶと、ヒットした本文の後ろに「該当なし」の定型文が連なることがある。
// 見出しの後ろに続く定型文を、その出典の本文として見せない。
const NOT_FOUND_SENTENCE = /該当する設定資料は見つかりませんでした。(?:別のキーワードで再検索してください。)?/g;

/** context から出典（ファイル名と本文）の一覧を取り出す。見出しが無ければ空配列。 */
export function parseSources(context: string): SourceDoc[] {
  const normalized = context.replace(/\r\n?/g, '\n');
  const headers = [...normalized.matchAll(SOURCE_HEADER)];

  return headers.flatMap((match, index) => {
    const name = match[1] ?? match[2];
    if (name === undefined || match.index === undefined) return [];
    const start = match.index + match[0].length;
    const end = headers[index + 1]?.index ?? normalized.length;
    const text = normalized.slice(start, end).replace(NOT_FOUND_SENTENCE, '').trim();
    return text ? [{ name, text }] : [];
  });
}

const squash = (text: string) => text.replace(/\s+/g, '');

/**
 * 出典の抜粋が、資料のどのチャンクから来たかを探して、そのページを返す。見つからなければ null。
 * 検索の結果にはページが付かないので、資料のチャンク（本文とページ）と、空白を除いて突き合わせる。
 * 抜粋は「見出し + 本文」なので、チャンクの本文を含んでいるか、その逆かで判定する。
 */
export function findSourcePage(chunks: ReadonlyArray<{ content: string; page: number | null }>, excerpt: string): number | null {
  const target = squash(excerpt);
  if (target === '') return null;
  const hit = chunks.find((chunk) => {
    const content = squash(chunk.content);
    return content !== '' && (target.includes(content) || content.includes(target));
  });
  return hit?.page ?? null;
}

/** 抜粋とチャンクを突き合わせるときの、短い側の最小の長さ（空白を除く）。短い文は、無関係なチャンクにも含まれてしまう。 */
const MIN_MATCH_LENGTH = 10;

/**
 * 出典の抜粋が、資料のどのチャンクから来たかを探して、そのチャンクの番号（`chunkIndex`）を返す。見つからなければ null。
 * 見つからないときは何も強調しない（近そうなチャンクを当てずっぽうに選ばない）。
 * 複数のチャンクに一致したときは、検索が返したページ（knownPage）と合うものを先にし、無ければ最初の 1 つ。
 */
export function findSourceChunk(
  chunks: ReadonlyArray<{ chunkIndex: number; content: string; page: number | null }>,
  excerpt: string,
  knownPage?: number,
): number | null {
  const target = squash(excerpt);
  if (target.length < MIN_MATCH_LENGTH) return null;
  const hits = chunks.filter((chunk) => {
    const content = squash(chunk.content);
    if (content.length < MIN_MATCH_LENGTH) return false;
    return target.includes(content) || content.includes(target);
  });
  const hit = (knownPage === undefined ? undefined : hits.find((chunk) => chunk.page === knownPage)) ?? hits[0];
  return hit?.chunkIndex ?? null;
}

const isPage = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value >= 1;
const MAX_DOCUMENT_ID = 2_147_483_647;
const isDocumentId = (value: unknown): value is number => isPage(value) && value <= MAX_DOCUMENT_ID;

/**
 * サーバーの `sources`（信用しない入力）を読む。配列でなければ undefined（古い応答。画面は突き合わせにフォールバックする）。
 * ページが 1 以上の整数でなければ null にして表示しない。`page_end` は `page` より大きいときだけ使う。
 */
export function parseSourceRefs(value: unknown): SourceRef[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const refs: SourceRef[] = [];
  for (const item of value) {
    if (typeof item !== 'object' || item === null) return undefined;
    const { name, page, page_end: pageEnd, weak, collection, document_id: documentId } = item as Record<string, unknown>;
    if (typeof name !== 'string') return undefined;
    const validPage = isPage(page) ? page : null;
    refs.push({
      name,
      page: validPage,
      pageEnd: validPage !== null && isPage(pageEnd) && pageEnd > validPage ? pageEnd : null,
      weak: weak === true,
      collection: typeof collection === 'string' && COLLECTION_NAME_PATTERN.test(collection) ? collection : null,
      documentId: isDocumentId(documentId) ? documentId : null,
    });
  }
  return refs;
}

/**
 * context から読んだ出典に、サーバーが送ったページを付ける。
 * 件数とファイル名が 1 件ずつ合わないとき（見出しに見える行が本文にあった等）は、ずれを避けてページも weak も付けない。
 */
export function withPages(docs: SourceDoc[], refs: SourceRef[] | undefined): SourceDoc[] {
  if (!refs || refs.length !== docs.length || docs.some((doc, i) => doc.name !== refs[i]?.name)) return docs;
  return docs.map((doc, i) => {
    const ref = refs[i];
    if (!ref) return doc;
    const withId = ref.documentId == null ? doc : { ...doc, documentId: ref.documentId };
    const withGroup = ref.collection === null ? withId : { ...withId, collection: ref.collection };
    const withWeak = ref.weak ? { ...withGroup, weak: true } : withGroup;
    if (ref.page === null) return withWeak;
    return ref.pageEnd === null ? { ...withWeak, page: ref.page } : { ...withWeak, page: ref.page, pageEnd: ref.pageEnd };
  });
}

/** 出典カードに出すページの表記。ページが無ければ null。「p.2」、ページをまたぐなら「p.2–3」。 */
export function formatPages(page: number | undefined, pageEnd: number | undefined): string | null {
  if (page === undefined) return null;
  return pageEnd === undefined ? `p.${page}` : `p.${page}–${pageEnd}`;
}

/** 回答の下の「参照資料」に、最初から並べる数。残りは「ほか n 件」で開く。 */
export const REFERENCE_LIST_LIMIT = 5;

/** 参照資料として見せる出典（`weak` を除く）。`index` は、元の出典の並びでの添字（番号は `index + 1`）。 */
export function referenceCards(sources: SourceDoc[]): Array<{ source: SourceDoc; index: number }> {
  return sources.map((source, index) => ({ source, index })).filter(({ source }) => !source.weak);
}

const referenceKey = (source: SourceDoc) => [source.name, source.page ?? '', source.pageEnd ?? '', squash(source.text)].join('\u0000');

/**
 * 回答の下の「参照資料」に出す出典（`weak` を除く）。資料名・ページ・抜粋のすべてが同じものだけを 1 つにまとめる。
 * 同じ資料でも、ページか抜粋が違えば別の根拠なので、まとめない。`index` は、元の出典の並びでの添字。
 * 出典は回答全体に対するもので、どの文の根拠かは分からない（文との対応は、ここでは作らない）。
 */
export function referenceItems(sources: SourceDoc[]): Array<{ source: SourceDoc; index: number }> {
  const seen = new Set<string>();
  return referenceCards(sources).filter(({ source }) => {
    const key = referenceKey(source);
    if (seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}

/**
 * 回答の文末の引用番号（`[n]`、出典の並びの n 番目）が指す、参照資料の項目の添字。
 * 参照資料に出ない出典（`weak`）は含めない（その番号は画面に出さない）。同じ根拠でまとめられた出典は、最初の 1 件の添字になる。
 */
export function referenceTargets(sources: SourceDoc[]): Map<number, number> {
  const first = new Map<string, number>();
  const targets = new Map<number, number>();
  sources.forEach((source, index) => {
    if (source.weak) return;
    const key = referenceKey(source);
    const head = first.get(key) ?? index;
    first.set(key, head);
    targets.set(index, head);
  });
  return targets;
}

/** 根拠の語句として、最低限の長さ（空白を除く）。短すぎる語句は、どこにでも一致してしまう。 */
const MIN_SPAN_LENGTH = 2;

/** 語句が、出典の本文にそのまま（空白を除いて）含まれるか。含まれなければ、強調に使わない（言い換え・作り話を根拠の位置にしない）。 */
export function isVerifiedSpan(span: string, sourceText: string): boolean {
  const target = squash(span);
  return target.length >= MIN_SPAN_LENGTH && squash(sourceText).includes(target);
}

/**
 * 本文（改行・空白を含む）の中で、語句（空白を除いて照合）がある範囲 `[start, end)` を返す。無ければ null。
 * 抜粋とチャンクの表示で改行・空白が違っても、同じ箇所に当てるためのもの。
 */
export function locateSpan(content: string, span: string): [number, number] | null {
  const target = squash(span);
  if (target.length < MIN_SPAN_LENGTH) return null;
  const positions: number[] = [];
  let squashed = '';
  for (let i = 0; i < content.length; i += 1) {
    const ch = content[i] ?? '';
    if (/\s/.test(ch)) continue;
    positions.push(i);
    squashed += ch;
  }
  const at = squashed.indexOf(target);
  if (at < 0) return null;
  const start = positions[at];
  const last = positions[at + target.length - 1];
  return start === undefined || last === undefined ? null : [start, last + 1];
}
