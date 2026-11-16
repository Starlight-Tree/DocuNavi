/**
 * 回答のあとに出す「続けて聞く」候補。回答の根拠になった資料ごとに、その資料に答えがある質問を用意してある
 * （質問は評価用の質問集と、資料の本文から選んだもの）。聞いたばかりの質問は出さない。
 */
const BY_SOURCE: Record<string, readonly string[]> = {
  'tsubasa_dorm_rules.pdf': [
    '入寮できるのは誰ですか？',
    '寮費はいくらですか？',
    '門限は何時ですか？',
    '入寮の申込みはいつまでにすればよいですか？',
  ],
  'aoba_aw820_manual.pdf': [
    'この洗濯乾燥機の本体の質量は、どのくらいですか。',
    'エラーコード E05 は、何が原因で出ますか。',
    '洗剤を自動で入れるタンクには、洗剤をどのくらい入れられますか。',
  ],
  'cafe_store_guide.pdf': [
    '火曜日が定休日になっている店舗を教えてください。',
    'ポイントがたまると、100ポイントでどんな特典と交換できますか。',
    '犬を連れて店に入りたいのですが、店内の席に座れますか。',
  ],
};

const ZAHARD = ['ザーハリアンって何者？', '昼と夜の気温差について教えて', 'ザーハードの大気の酸素濃度はどれくらい？'] as const;

/** PDF の題名（`documents.title`）から、上の表のキーへ。ファイル名が表と合わない資料（コレクション名つきのパス、別名で取り込んだ複製）でも候補を出す。 */
const TITLE_TO_KEY: Record<string, string> = {
  'つばさ大学学生寮 入寮規程': 'tsubasa_dorm_rules.pdf',
  'あおば電機 全自動洗濯乾燥機 AW-820 取扱説明書': 'aoba_aw820_manual.pdf',
  'みなとコーヒー 店舗案内': 'cafe_store_guide.pdf',
};
const ZAHARD_TITLES: ReadonlySet<string> = new Set(['惑星ザーハード概説', 'ザーハリアン文明と遺跡', 'ザーハードのエネルギー源']);

/** 出典。`name` は資料のファイル名（本番ではコレクション名つきのパスのことがある）、`title` は資料の題名。 */
export interface FollowUpSource {
  name: string;
  title?: string | null;
}

function questionsFor({ name, title }: FollowUpSource): readonly string[] {
  const base = name.split('/').pop() ?? name;
  if (/^ザーハード\d*\.(pdf|txt)$/.test(base)) return ZAHARD;
  const byName = BY_SOURCE[base];
  if (byName) return byName;
  const key = title ? TITLE_TO_KEY[title.trim()] : undefined;
  if (key) return BY_SOURCE[key] ?? [];
  if (title && ZAHARD_TITLES.has(title.trim())) return ZAHARD;
  return [];
}

export function followUpQuestions(sources: ReadonlyArray<string | FollowUpSource>, asked: readonly string[], limit = 3): string[] {
  const seen = new Set(asked.map((q) => q.trim()));
  const result: string[] = [];
  for (const source of sources) {
    for (const question of questionsFor(typeof source === 'string' ? { name: source } : source)) {
      if (!seen.has(question)) {
        seen.add(question);
        result.push(question);
      }
    }
  }
  return result.slice(0, limit);
}
