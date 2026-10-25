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
}

/** サーバーが `messageMetadata.sources` で送る、出典ごとのページ（context の出典と同じ順）。 */
export interface SourceRef {
  name: string;
  page: number | null;
  pageEnd: number | null;
  weak: boolean;
  /** その資料の資料グループ。未設定、または古いサーバー（送ってこない）は null。 */
  collection: string | null;
}

// バックエンドは検索結果を "[ファイル名.txt]\n本文"（PDF なら .pdf）を空行で連ねた形で返す。
// 「該当する設定資料は見つかりませんでした。」のような定型文は見出しを持たないので、出典にならない。
const SOURCE_HEADER = /^\[([^\]\n]+\.(?:txt|pdf))\]$/gm;

// 検索ツールを複数回呼ぶと、ヒットした本文の後ろに「該当なし」の定型文が連なることがある。
// 見出しの後ろに続く定型文を、その出典の本文として見せない。
const NOT_FOUND_SENTENCE = /該当する設定資料は見つかりませんでした。(?:別のキーワードで再検索してください。)?/g;

/** context から出典（ファイル名と本文）の一覧を取り出す。見出しが無ければ空配列。 */
export function parseSources(context: string): SourceDoc[] {
  const normalized = context.replace(/\r\n?/g, '\n');
  const headers = [...normalized.matchAll(SOURCE_HEADER)];

  return headers.flatMap((match, index) => {
    const name = match[1];
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

const isPage = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value >= 1;

/**
 * サーバーの `sources`（信用しない入力）を読む。配列でなければ undefined（古い応答。画面は突き合わせにフォールバックする）。
 * ページが 1 以上の整数でなければ null にして表示しない。`page_end` は `page` より大きいときだけ使う。
 */
export function parseSourceRefs(value: unknown): SourceRef[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const refs: SourceRef[] = [];
  for (const item of value) {
    if (typeof item !== 'object' || item === null) return undefined;
    const { name, page, page_end: pageEnd, weak, collection } = item as Record<string, unknown>;
    if (typeof name !== 'string') return undefined;
    const validPage = isPage(page) ? page : null;
    refs.push({
      name,
      page: validPage,
      pageEnd: validPage !== null && isPage(pageEnd) && pageEnd > validPage ? pageEnd : null,
      weak: weak === true,
      collection: typeof collection === 'string' && COLLECTION_NAME_PATTERN.test(collection) ? collection : null,
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
    const withGroup = ref.collection === null ? doc : { ...doc, collection: ref.collection };
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
