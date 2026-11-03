/**
 * 「対象」「資料種別」の UI モックの架空データ。実 API・本体の画面には触れない。
 * 識別名・表示名・対応は、承認された初期分類（docs/configuration.md の「対象と資料種別」）と同じ。ID は本番と同じ 1〜6 に、モック用の 7・8 を足す。
 */

export interface MockTarget {
  name: string;
  label: string;
}

export interface MockDocType {
  name: string;
  label: string;
}

/** 旧グループ（`documents.collection`。権限の単位）。画面では「旧フィルター」「旧グループの会話」としてだけ出る。 */
export interface MockGroup {
  name: string;
  label: string;
}

export type DocStatus = 'ready' | 'processing';

export interface MockDocument {
  id: number;
  /** 資料名（PDF の題名）。 */
  title: string;
  source: string;
  /** 対象の識別名。未分類は null。 */
  target: string | null;
  /** 資料種別の識別名。未分類は null。 */
  docType: string | null;
  /** 旧グループ。 */
  collection: string;
  status: DocStatus;
  pages: number;
  updatedAt: string;
  /** 回答の出典に使う、ページごとの抜粋（モックの回答用）。 */
  excerpt: string;
}

export const TARGETS: readonly MockTarget[] = [
  { name: 'zahard', label: 'ザーハード' },
  { name: 'minato-coffee', label: 'みなとコーヒー' },
  { name: 'tsubasa-dorm', label: 'つばさ大学学生寮' },
  { name: 'aoba-aw820', label: 'あおば電機 AW-820' },
];

export const DOC_TYPES: readonly MockDocType[] = [
  { name: 'setting', label: '設定資料' },
  { name: 'store-guide', label: '店舗案内' },
  { name: 'regulation', label: '規程' },
  { name: 'manual', label: '取扱説明書' },
];

export const GROUPS: readonly MockGroup[] = [
  { name: 'zahard', label: '設定資料' },
  { name: 'cafe', label: '店舗案内' },
  { name: 'guides', label: '規程・取扱説明書' },
];

export const DOCUMENTS: readonly MockDocument[] = [
  { id: 1, title: 'みなとコーヒー 店舗案内', source: 'cafe_store_guide.pdf', target: 'minato-coffee', docType: 'store-guide', collection: 'cafe', status: 'ready', pages: 4, updatedAt: '2026-11-01T09:20:00+09:00', excerpt: '営業時間は朝8時から夜8時までです。定休日は毎週水曜日です。' },
  { id: 2, title: 'あおば電機 洗濯乾燥機 AW-820 取扱説明書', source: 'aoba_aw820_manual.pdf', target: 'aoba-aw820', docType: 'manual', collection: 'guides', status: 'ready', pages: 12, updatedAt: '2026-10-28T15:05:00+09:00', excerpt: '糸くずフィルターは、洗濯のたびに掃除してください。' },
  { id: 3, title: 'つばさ大学 学生寮 入寮規程', source: 'tsubasa_dorm_rules.pdf', target: 'tsubasa-dorm', docType: 'regulation', collection: 'guides', status: 'ready', pages: 8, updatedAt: '2026-10-30T11:40:00+09:00', excerpt: '入寮の申し込み期限は、毎年3月末日です。' },
  { id: 4, title: 'ザーハード 設定資料 1', source: 'ザーハード1.pdf', target: 'zahard', docType: 'setting', collection: 'zahard', status: 'ready', pages: 6, updatedAt: '2026-10-22T10:00:00+09:00', excerpt: 'ザーハードの大気は薄く、二酸化炭素が主成分である。' },
  { id: 5, title: 'ザーハード 設定資料 2', source: 'ザーハード2.pdf', target: 'zahard', docType: 'setting', collection: 'zahard', status: 'ready', pages: 5, updatedAt: '2026-10-22T10:05:00+09:00', excerpt: 'ザーハリアン文明は、遺跡だけを残して姿を消した。' },
  { id: 6, title: 'ザーハード 設定資料 3', source: 'ザーハード3.pdf', target: 'zahard', docType: 'setting', collection: 'zahard', status: 'ready', pages: 7, updatedAt: '2026-10-22T10:10:00+09:00', excerpt: '岩山の内部には、未知のエネルギー源が眠っている可能性がある。' },
  // ここから 2 件は、モック用の追加（取り込み中の資料と、未分類の資料を見せるため）
  { id: 7, title: 'みなとコーヒー メニュー表', source: 'cafe_menu.pdf', target: 'minato-coffee', docType: 'store-guide', collection: 'cafe', status: 'processing', pages: 2, updatedAt: '2026-11-03T08:30:00+09:00', excerpt: '' },
  { id: 8, title: '打ち合わせメモ', source: 'meeting_memo.pdf', target: null, docType: null, collection: 'zahard', status: 'ready', pages: 1, updatedAt: '2026-09-02T13:00:00+09:00', excerpt: '分類の前の資料です。' },
];

/** モックの「今」。更新の新しさの帯を、実行日によらず同じにする。 */
export const NOW = new Date('2026-11-03T12:00:00+09:00');

export const label = (list: readonly { name: string; label: string }[], name: string | null): string =>
  name === null ? '未分類' : (list.find((item) => item.name === name)?.label ?? name);

/** 質問の対象として選べる対象と、見える資料の数（取り込み中も数える）・検索できる数。見える資料が 1 件も無い対象は出さない。 */
export function targetCatalog(documents: readonly MockDocument[] = DOCUMENTS) {
  return TARGETS.flatMap((target) => {
    const mine = documents.filter((d) => d.target === target.name);
    return mine.length === 0
      ? []
      : [{ ...target, documentCount: mine.length, readyCount: mine.filter((d) => d.status === 'ready').length }];
  });
}
