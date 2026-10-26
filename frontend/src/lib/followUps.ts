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

function questionsFor(source: string): readonly string[] {
  if (/^ザーハード\d*\.(pdf|txt)$/.test(source)) return ZAHARD;
  return BY_SOURCE[source] ?? [];
}

export function followUpQuestions(sources: readonly string[], asked: readonly string[], limit = 3): string[] {
  const seen = new Set(asked.map((q) => q.trim()));
  const result: string[] = [];
  for (const source of sources) {
    for (const question of questionsFor(source)) {
      if (!seen.has(question)) {
        seen.add(question);
        result.push(question);
      }
    }
  }
  return result.slice(0, limit);
}
