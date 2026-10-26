import { mkdirSync, writeFileSync } from 'node:fs';
import { expect, test } from '@playwright/test';
import type { APIRequestContext } from '@playwright/test';

// 実 API の検証（`npm run test:e2e:real-api`）。**本物の Gemini を呼ぶ（課金）。** 通常の `npm run test:e2e` と CI には入らない。
// 主モデルで始めた会話を、フォールバックのモデルで続けられるか（逆方向も）を、アプリの /chat/stream で確かめる。
// 本物の 503 は狙って起こせないので、主モデルへの要求だけを 503 に差し替える（tests/real_api_server.py。本物と同じ SDK の経路を通る）。
// 検索は偽物（DB・埋め込みの API は使わない）。ブラウザーは使わない（HTTP でアプリを叩く）。
// 結果は test-results/real-api-fallback.json に残す（モデル名・ステータス・秒数・エラー。API キーや本文の全体は残さない）。

interface GeminiCall {
  seq: number;
  model: string;
  endpoint: string;
  faked: boolean;
  status: number | null;
  seconds_to_response: number;
  error?: string; // 応答が届かなかったとき（ReadTimeout など）の例外名
  error_code?: number; // 失敗した応答の error.code
  error_status?: string; // 失敗した応答の error.status（UNAVAILABLE / RESOURCE_EXHAUSTED など）
  headers?: Record<string, string>; // 失敗した応答の、切り分け用のヘッダーだけ（retry-after、x-ratelimit-*）
}

interface SearchRecord {
  seq: number;
  query_head: string; // モデルが検索ツールに渡した検索語の先頭だけ（全文は残さない）
  query_chars: number;
  query_sha256: string; // 検索語の全文の SHA-256 の先頭 8 桁
  hits: number; // 偽の検索が返した件数
}

interface ServerState {
  primary: string;
  fallback: string;
  primary_fail: boolean;
  mock_mode: boolean;
  calls: GeminiCall[];
  searches: SearchRecord[];
}

interface Turn {
  label: string;
  question: string;
  primaryFailing: boolean;
  httpStatus: number;
  seconds: number;
  completed: boolean; // 最後に [DONE] が届いた
  errorChunk: string | null; // error チャンクの本文（固定メッセージ）
  searched: boolean; // 検索の出典が付いた（最後の message-metadata の sources が空でない）
  sourcesCount: number; // 最後の message-metadata の sources の件数
  searches: SearchRecord[]; // このターンで検索ツールが受けた検索語（先頭だけ）とヒット数
  replyLength: number;
  replyHead: string;
  threadId: string | null;
  calls: GeminiCall[]; // このターンで Gemini に出た要求
  usedPrimary: number; // 本物の主モデルへの要求
  fakedPrimary: number; // 503 に差し替えた主モデルへの要求
  usedFallback: number; // フォールバックのモデルへの要求
}

/** そのモデルの要求が 1 回でも本物で成功（200）したか。 */
function answeredBy(turn: Turn, model: string): boolean {
  return turn.calls.some((c) => c.model === model && !c.faked && c.status === 200);
}

async function state(request: APIRequestContext): Promise<ServerState> {
  const response = await request.get('/__real_api/state');
  expect(response.ok()).toBe(true);
  return (await response.json()) as ServerState;
}

async function setPrimaryFail(request: APIRequestContext, on: boolean) {
  const response = await request.post('/__real_api/primary-fail', { data: { on } });
  expect(response.ok()).toBe(true);
}

/** /chat/stream に 1 回送り、SSE を最後まで読んで要点を返す。 */
async function ask(
  request: APIRequestContext,
  label: string,
  question: string,
  threadId: string | null,
  primaryFailing: boolean,
): Promise<Turn> {
  const initialState = await state(request);
  const before = initialState.calls.length;
  const beforeSearches = initialState.searches.length;
  const started = Date.now();
  const response = await request.post('/chat/stream', {
    data: threadId ? { message: question, thread_id: threadId } : { message: question },
    headers: { Accept: 'text/event-stream' },
    timeout: 90_000,
  });
  const body = await response.text();
  const seconds = Math.round((Date.now() - started) / 100) / 10;

  let reply = '';
  let errorChunk: string | null = null;
  let sourcesCount = 0;
  let newThreadId: string | null = null;
  let completed = false;
  for (const line of body.split('\n')) {
    if (!line.startsWith('data: ')) continue;
    const payload = line.slice('data: '.length);
    if (payload === '[DONE]') {
      completed = true;
      continue;
    }
    const chunk = JSON.parse(payload) as {
      type?: string;
      delta?: string;
      errorText?: string;
      messageMetadata?: { thread_id?: string; sources?: unknown[] };
    };
    if (chunk.type === 'text-delta' && chunk.delta) reply += chunk.delta;
    if (chunk.type === 'error') errorChunk = chunk.errorText ?? '';
    if (chunk.messageMetadata?.thread_id) newThreadId = chunk.messageMetadata.thread_id;
    if (Array.isArray(chunk.messageMetadata?.sources)) sourcesCount = chunk.messageMetadata.sources.length;
  }

  const { primary, fallback, calls: all, searches: allSearches } = await state(request);
  const calls = all.slice(before);
  const searches = allSearches.slice(beforeSearches);
  return {
    label,
    question,
    primaryFailing,
    httpStatus: response.status(),
    seconds,
    completed,
    errorChunk,
    searched: sourcesCount > 0,
    sourcesCount,
    searches,
    replyLength: reply.length,
    replyHead: reply.slice(0, 80),
    threadId: newThreadId ?? threadId,
    calls,
    usedPrimary: calls.filter((c) => c.model === primary && !c.faked).length,
    fakedPrimary: calls.filter((c) => c.model === primary && c.faked).length,
    usedFallback: calls.filter((c) => c.model === fallback).length,
  };
}

test('主モデルで始めた会話を、フォールバックのモデルで続けられ、主モデルに戻しても続く', async ({ request }) => {
  test.setTimeout(5 * 60_000);

  const initial = await state(request);
  test.skip(initial.mock_mode, 'GEMINI_API_KEY が無い（モックモード）ため、本物の Gemini を呼べません。.env にキーを入れてください。');

  await request.post('/__real_api/reset');
  const turns: Turn[] = [];
  // 本物の主モデルが、いま混んでいて失敗する（503・タイムアウト）ことがある。その回は、主モデルが作った履歴を
  // フォールバックが引き継ぐ検証ができない。失敗ではなく「確かめられなかった」として記録し、スキップする
  const inconclusive: string[] = [];
  // 失敗にはしないが、結果に残す気づき（引き継ぎの検証とは別の件）
  const notes: string[] = [];
  const report = () => {
    mkdirSync('test-results', { recursive: true });
    writeFileSync(
      'test-results/real-api-fallback.json',
      JSON.stringify({ primary: initial.primary, fallback: initial.fallback, inconclusive, notes, turns }, null, 2),
    );
  };

  try {
    // 1) 主モデルで始める（本物。検索のツール呼び出しを含む履歴ができる）
    const t1 = await ask(request, '1 主モデルで開始', 'ザーハードの大気について教えて', null, false);
    turns.push(t1);
    expect(t1.httpStatus).toBe(200);
    expect(t1.errorChunk).toBeNull();
    expect(t1.completed).toBe(true);
    expect(t1.replyLength).toBeGreaterThan(0);
    expect(t1.threadId).not.toBeNull();
    // 出典（sources）の有無は、フォールバックの引き継ぎとは別の件なので、失敗にはせず記録だけにする
    if (!t1.searched) {
      // 出典が空になる条件は 3 つ（ツール未呼び出し / 検索が 0 件 / 最後の検索が 0 件）。どれかを検索の記録から示す
      const last = t1.searches.at(-1);
      const cause =
        last === undefined
          ? '検索ツールが呼ばれなかった'
          : last.hits === 0
            ? `最後の検索が 0 件だった（検索 ${t1.searches.length} 回、検索語の先頭「${last.query_head}」）`
            : '検索は当たったのに sources が空（アプリの問題の可能性）';
      notes.push(`1 ターン目: 検索の出典（sources）が付かなかった。${cause}。引き継ぎの検証とは別の件。`);
      test.info().annotations.push({ type: 'note', description: notes[notes.length - 1] });
    }
    if (!answeredBy(t1, initial.primary)) {
      inconclusive.push(
        '1 ターン目: 本物の主モデルが成功しなかった（503・タイムアウトなど）。回答はフォールバックが作ったので、主モデルが作った履歴の引き継ぎは確かめられない。',
      );
      report();
      test.skip(true, inconclusive[0]);
    }

    // 2) 主モデルを 503 にして、同じ会話を続ける → フォールバックのモデルが、1 の履歴を引き継ぐ
    await setPrimaryFail(request, true);
    const t2 = await ask(request, '2 フォールバックで継続', 'さっきの大気の主成分は何でしたか？', t1.threadId, true);
    turns.push(t2);
    expect(t2.httpStatus).toBe(200);
    expect(t2.errorChunk).toBeNull();
    expect(t2.completed).toBe(true);
    expect(t2.replyLength).toBeGreaterThan(0);
    expect(t2.fakedPrimary, '主モデルへの要求が 503 に差し替えられていない').toBeGreaterThan(0);
    expect(t2.usedPrimary, '503 のはずの主モデルが本物に届いた').toBe(0);
    expect(t2.usedFallback, 'フォールバックが発火していない').toBeGreaterThan(0);
    expect(t2.calls.filter((c) => c.model === initial.fallback).every((c) => c.status === 200)).toBe(true);

    // 3) 主モデルに戻して、続ける → 2 でフォールバックが足した履歴も引き継げる（逆方向）
    await setPrimaryFail(request, false);
    const t3 = await ask(request, '3 主モデルに戻って継続', '最初に私が聞いたことを、短く言い直してください。', t1.threadId, false);
    turns.push(t3);
    expect(t3.httpStatus).toBe(200);
    expect(t3.errorChunk).toBeNull();
    expect(t3.completed).toBe(true);
    expect(t3.replyLength).toBeGreaterThan(0);
    if (!answeredBy(t3, initial.primary)) {
      inconclusive.push('3 ターン目: 本物の主モデルが成功しなかった。主モデルへ戻って続く（逆方向）は確かめられない。');
      report();
      test.skip(true, inconclusive[0]);
    }
    expect(t3.usedFallback).toBe(0);
  } finally {
    report();
    await setPrimaryFail(request, false);
  }
});
