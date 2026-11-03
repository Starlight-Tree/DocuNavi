/**
 * 対象（documents.target）と資料種別（documents.doc_type）の表示。
 * 表示名・説明・並び順は、サーバーの GET /targets・GET /doc-types が持つ。画面は名前を固定で持たない。
 * 一覧を読めなかったとき・一覧に無い名前は、識別名のまま表示する。
 */
import type { ClassificationInfo } from '../api/classifications';

/** 一覧の表示名。一覧（catalog）に無ければ、識別名そのもの。 */
export function classificationLabel(name: string, catalog?: ReadonlyArray<ClassificationInfo>): string {
  return catalog?.find((c) => c.name === name)?.label ?? name;
}

/** 検索できる資料がまだ無い対象（準備中）。選べるが、見つかる資料は無い。 */
export function isPreparing(item: ClassificationInfo): boolean {
  return item.readyCount === 0;
}
