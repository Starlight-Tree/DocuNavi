import { vi } from 'vitest';
import type { ChatResponse } from '../api/chat';

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

export function chatBody(overrides: Partial<ChatResponse> = {}): ChatResponse {
  return {
    reply: 'ザーハードの大気は主に二酸化炭素です。',
    thread_id: 'thread-1',
    context: '',
    mode: 'mock',
    ...overrides,
  };
}

export function errorResponse(
  status: number,
  detail: unknown,
  headers: Record<string, string> = {},
): Response {
  return jsonResponse({ detail }, { status, headers });
}

type ChatHandler = (body: Record<string, unknown>, call: number) => Response | Promise<Response>;

export interface FetchHarness {
  fetchMock: ReturnType<typeof vi.fn>;
  /** POST /chat に送られた JSON 本文の一覧（古い順）。 */
  chatBodies: () => Array<Record<string, unknown>>;
}

/**
 * fetch を差し替える。/health は常に mode=mock を返し、/chat は handler に渡す。
 * handler の第 2 引数は /chat の呼び出し回数（1 始まり）。
 */
export function installFetch(handler: ChatHandler): FetchHarness {
  const bodies: Array<Record<string, unknown>> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/health')) {
      return jsonResponse({ status: 'ok', mode: 'mock' });
    }
    const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
    bodies.push(body);
    return handler(body, bodies.length);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, chatBodies: () => bodies };
}

export function deferred<T>() {
  let resolve!: (value: T) => void;
  let reject!: (reason?: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}
