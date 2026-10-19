/**
 * 会話の一覧と削除（GET /threads、DELETE /threads/{id}）。ログイン中（AUTH_MODE=required）の本人の会話だけが対象。
 * 認証のために、fetch は呼び出し側から受け取る（createAuthedFetch で包んだもの）。
 */
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

/** 会話を削除する。すでに無い（404）ときは成功として扱う（目的の状態になっている）。 */
export async function deleteThread(fetcher: Fetcher, threadId: string): Promise<void> {
  try {
    await fetcher(endpoint(`/threads/${encodeURIComponent(threadId)}`), { method: 'DELETE' });
  } catch (error) {
    if (error instanceof ChatApiError && error.status === 404) return;
    throw error;
  }
}
