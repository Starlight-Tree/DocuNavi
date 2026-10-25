/**
 * 資料グループ（documents.collection。storage/pdf/<名前>/ のディレクトリ名）の表示。
 * 表示名・説明・並び順は、サーバーの GET /collections が持つ。画面は名前を固定で持たない。
 * 一覧を読めなかったとき・一覧に無い名前は、名前（識別名）のまま表示する。
 */
import type { CollectionInfo } from '../api/collections';

/** グループの表示名。一覧（catalog）に無ければ、名前そのもの。 */
export function collectionLabel(collection: string, catalog?: ReadonlyArray<CollectionInfo>): string {
  return catalog?.find((c) => c.name === collection)?.label ?? collection;
}

/** 資料の一覧にあるグループの名前を、名前順で返す（重複なし。未設定（null）は含めない）。一覧を読めなかったときの絞り込みに使う。 */
export function collectionOptions(collections: ReadonlyArray<string | null>): string[] {
  return [...new Set(collections.filter((c): c is string => c !== null))].sort((a, b) => a.localeCompare(b));
}

/** 資料がまだ検索の対象になっていないグループ（準備中）。選べるが、見つかる資料は無い。 */
export function isPreparing(collection: CollectionInfo): boolean {
  return collection.readyCount === 0;
}
