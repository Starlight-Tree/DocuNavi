/**
 * 会話の一覧と削除（GET /threads、DELETE /threads/{id}）。ログイン中（AUTH_MODE=required）の本人の会話だけが対象。
 * 認証のために、fetch は呼び出し側から受け取る（createAuthedFetch で包んだもの）。
 */
import { CLASSIFICATION_NAME_PATTERN } from './classifications';
import { COLLECTION_NAME_PATTERN } from './collections';
import { ChatApiError, MSG_BAD_BODY, endpoint, isRecord } from './chat';
import type { chatFetch } from './chat';

type Fetcher = typeof chatFetch;

export interface ThreadSummary {
  threadId: string;
  /** 最初の発言の先頭（サーバーが切り詰めたもの）。 */
  title: string;
  createdAt: string | null;
  updatedAt: string | null;
}

function parseThread(value: unknown): ThreadSummary | null {
  if (!isRecord(value)) return null;
  const { thread_id: threadId, title, created_at: createdAt, updated_at: updatedAt } = value;
  if (typeof threadId !== 'string' || typeof title !== 'string') return null;
  if (createdAt != null && typeof createdAt !== 'string') return null;
  if (updatedAt != null && typeof updatedAt !== 'string') return null;
  return { threadId, title, createdAt: createdAt ?? null, updatedAt: updatedAt ?? null };
}

export async function fetchThreads(fetcher: Fetcher, signal?: AbortSignal): Promise<ThreadSummary[]> {
  const init: RequestInit = { headers: { Accept: 'application/json' } };
  if (signal) init.signal = signal;
  const response = await fetcher(endpoint('/threads'), init);
  const body: unknown = await response.json().catch(() => null);
  if (!isRecord(body) || !Array.isArray(body.threads)) throw new ChatApiError(response.status, MSG_BAD_BODY);
  const threads: ThreadSummary[] = [];
  for (const item of body.threads) {
    const parsed = parseThread(item);
    if (!parsed) throw new ChatApiError(response.status, MSG_BAD_BODY);
    threads.push(parsed);
  }
  return threads;
}

export interface ThreadMessage {
  role: 'user' | 'assistant';
  text: string;
}

export interface ThreadHistory {
  messages: ThreadMessage[];
  /** この会話の旧グループの範囲（資料グループの名前）。null は絞らない（範囲の項目が無い古い会話・古い API も同じ）。 */
  collection: string | null;
  /** この会話の質問の対象（対象の識別名）。null は絞らない（対象の項目が無い古い会話・古い API も同じ）。 */
  target: string | null;
  /** 保存された範囲（旧グループか対象）が、いま利用できない（見えない・資料が無い）。このとき collection と target は null。古い API は false。 */
  scopeUnavailable: boolean;
}

/** 会話の発言を古い順に、この会話の検索範囲といっしょに取得する（GET /threads/{id}/messages）。形が違えば MSG_BAD_BODY。 */
export async function fetchThreadMessages(
  fetcher: Fetcher,
  threadId: string,
  signal?: AbortSignal,
): Promise<ThreadHistory> {
  const init: RequestInit = { headers: { Accept: 'application/json' } };
  if (signal) init.signal = signal;
  const response = await fetcher(endpoint(`/threads/${encodeURIComponent(threadId)}/messages`), init);
  const body: unknown = await response.json().catch(() => null);
  if (!isRecord(body) || !Array.isArray(body.messages)) throw new ChatApiError(response.status, MSG_BAD_BODY);
  const messages: ThreadMessage[] = [];
  for (const item of body.messages) {
    if (!isRecord(item) || (item.role !== 'user' && item.role !== 'assistant') || typeof item.text !== 'string') {
      throw new ChatApiError(response.status, MSG_BAD_BODY);
    }
    messages.push({ role: item.role, text: item.text });
  }
  // 範囲の形が合わない値は、主張しない（null）。送るときは省略になり、サーバーが会話の範囲を使う
  const { collection, target } = body;
  return {
    messages,
    collection: typeof collection === 'string' && COLLECTION_NAME_PATTERN.test(collection) ? collection : null,
    target: typeof target === 'string' && CLASSIFICATION_NAME_PATTERN.test(target) ? target : null,
    scopeUnavailable: body.scope_unavailable === true,
  };
}

/** 会話を削除する。すでに無い（404）ときは成功として扱う（目的の状態になっている）。 */
export async function deleteThread(fetcher: Fetcher, threadId: string): Promise<void> {
  try {
    await fetcher(endpoint(`/threads/${encodeURIComponent(threadId)}`), { method: 'DELETE' });
  } catch (error) {
    if (error instanceof ChatApiError && error.status === 404) return;
    throw error;
  }
}
