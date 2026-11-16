/**
 * 回答のあとに出す「続けて聞く」候補。回答の根拠になった資料ごとに、取り込みのときに作って保存した質問
 * （`documents.suggested_questions`。サーバーが出典の `suggested_questions` に載せる）を並べる。聞いたばかりの質問は出さない。
 * 質問が無い出典（保存する前の古い会話、まだ作っていない資料）は、候補を出さない。
 */

/** 出典。`suggestedQuestions` は、その資料に答えがある質問（最大 3 件）。 */
export interface FollowUpSource {
  suggestedQuestions?: readonly string[];
}

export function followUpQuestions(
  sources: readonly FollowUpSource[],
  asked: readonly string[],
  limit = 3,
): string[] {
  const seen = new Set(asked.map((q) => q.trim()));
  const result: string[] = [];
  for (const source of sources) {
    for (const question of source.suggestedQuestions ?? []) {
      if (!seen.has(question)) {
        seen.add(question);
        result.push(question);
      }
    }
  }
  return result.slice(0, limit);
}
