/**
 * 資料群（documents.collection。storage/pdf/<名前>/ のディレクトリ名）の表示名と並び順。
 * DB には英数字の名前だけがあり、日本語の表示名は画面が持つ。知らない名前は、そのまま表示する。
 */

const LABELS: Record<string, string> = {
  zahard: '設定資料',
  cafe: '店舗案内',
  guides: '規程・取扱説明書',
};

// 絞り込みの並び。ここに無い名前は、その後ろに名前順
const ORDER = Object.keys(LABELS);

export function collectionLabel(collection: string): string {
  return Object.hasOwn(LABELS, collection) ? (LABELS[collection] as string) : collection;
}

/** 一覧にある資料群を、絞り込みの並びで返す（重複なし。未設定（null）は含めない）。 */
export function collectionOptions(collections: ReadonlyArray<string | null>): string[] {
  const present = [...new Set(collections.filter((c): c is string => c !== null))];
  const rank = (name: string) => {
    const index = ORDER.indexOf(name);
    return index === -1 ? ORDER.length : index;
  };
  return present.sort((a, b) => rank(a) - rank(b) || a.localeCompare(b));
}
