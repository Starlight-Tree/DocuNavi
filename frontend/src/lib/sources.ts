export interface SourceDoc {
  /** 資料のファイル名（例: ザーハード1.txt、cafe_store_guide.pdf） */
  name: string;
  /** そのファイルから検索で拾われた本文 */
  text: string;
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
