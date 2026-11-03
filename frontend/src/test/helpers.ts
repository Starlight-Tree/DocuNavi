import { vi } from 'vitest';

export const DEFAULT_REPLY = 'ザーハードの大気は主に二酸化炭素です。';

export function jsonResponse(body: unknown, init: ResponseInit = {}): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
    ...init,
  });
}

export function errorResponse(
  status: number,
  detail: unknown,
  headers: Record<string, string> = {},
): Response {
  return jsonResponse({ detail }, { status, headers });
}

// --- UI message stream（SSE）-------------------------------------------------

type Chunk = Record<string, unknown>;

export interface ReplySpec {
  /** 回答の本文。deltas を渡さなければ 1 つの text-delta で送る。 */
  reply?: string;
  /** 本文を分けて送る断片（reply より優先）。 */
  deltas?: string[];
  thread_id?: string;
  mode?: string;
  /** 設定資料。未検索なら空文字。 */
  context?: string;
  /** 出典ごとのページ（サーバーの messageMetadata.sources）。渡さなければ送らない（古いサーバー）。 */
  sources?: unknown;
  /** この会話の検索範囲（messageMetadata.collection。null は「すべて」）。渡さなければ送らない（古いサーバー）。 */
  collection?: string | null;
  /** この会話の質問の対象（messageMetadata.target。null は絞らない）。渡さなければ送らない（古いサーバー）。 */
  target?: string | null;
  /** 今回の検索で拾ったチャンクの数（messageMetadata.hit_count）。渡さなければ送らない（古いサーバー）。 */
  hit_count?: number | null;
}

/** サーバーが返す正常系のチャンクの並び（start → text → message-metadata → finish）。 */
export function replyChunks(spec: ReplySpec = {}): Chunk[] {
  const deltas = spec.deltas ?? [spec.reply ?? DEFAULT_REPLY];
  return [
    {
      type: 'start',
      messageId: 'srv-msg',
      messageMetadata: { thread_id: spec.thread_id ?? 'thread-1', mode: spec.mode ?? 'mock' },
    },
    { type: 'text-start', id: 'text-0' },
    ...deltas.map((delta) => ({ type: 'text-delta', id: 'text-0', delta })),
    { type: 'text-end', id: 'text-0' },
    {
      type: 'message-metadata',
      messageMetadata: {
        context: spec.context ?? '',
        ...(spec.sources !== undefined ? { sources: spec.sources } : {}),
        ...(spec.collection !== undefined ? { collection: spec.collection } : {}),
        ...(spec.target !== undefined ? { target: spec.target } : {}),
        ...(spec.hit_count !== undefined ? { hit_count: spec.hit_count } : {}),
      },
    },
    { type: 'finish' },
  ];
}

export function sseText(chunks: Chunk[], done = true): string {
  const body = chunks.map((chunk) => `data: ${JSON.stringify(chunk)}\n\n`).join('');
  return done ? `${body}data: [DONE]\n\n` : body;
}

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream',
  'x-vercel-ai-ui-message-stream': 'v1',
};

export function sseResponse(chunks: Chunk[], init: ResponseInit = {}): Response {
  return new Response(sseText(chunks), { status: 200, headers: SSE_HEADERS, ...init });
}

/** 正常な回答（SSE）。 */
export function replyResponse(spec: ReplySpec = {}): Response {
  return sseResponse(replyChunks(spec));
}

export interface ControlledSse {
  response: Response;
  /** チャンクを 1 つ送る。 */
  push: (chunk: Chunk) => void;
  /** [DONE] を送って閉じる。 */
  finish: () => void;
}

/** テストが好きなタイミングでチャンクを流せる SSE の応答。 */
export function controlledSse(): ControlledSse {
  const encoder = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    response: new Response(stream, { status: 200, headers: SSE_HEADERS }),
    push: (chunk) => controller.enqueue(encoder.encode(`data: ${JSON.stringify(chunk)}\n\n`)),
    finish: () => {
      controller.enqueue(encoder.encode('data: [DONE]\n\n'));
      controller.close();
    },
  };
}

// --- fetch の差し替え --------------------------------------------------------

type ChatHandler = (
  body: Record<string, unknown>,
  call: number,
  init: RequestInit,
) => Response | Promise<Response>;

export interface FetchHarness {
  fetchMock: ReturnType<typeof vi.fn>;
  /** POST /chat/stream に送られた JSON 本文の一覧（古い順）。 */
  chatBodies: () => Array<Record<string, unknown>>;
  /** POST /chat/stream の呼び出しの init（signal の確認などに使う）。 */
  chatInits: () => RequestInit[];
}

export type ThreadsHandler = (url: string, method: string, init: RequestInit) => Response | Promise<Response>;

/** GET /collections への応答。既定は「サーバーが資料の DB を使っていない」（グループの機能が出ない）。 */
export type CollectionsHandler = () => Response | Promise<Response>;

/** GET /collections の正常な応答（4 つのグループ: 設定資料・店舗案内・規程・取扱説明書・準備中の資料・未登録の memo）。 */
export const COLLECTIONS_BODY = {
  configured: true,
  collections: [
    { name: 'zahard', label: '設定資料', description: 'ザーハードに関する設定資料です。', sort_order: 10, registered: true, document_count: 3, ready_count: 3 },
    { name: 'cafe', label: '店舗案内', description: '店舗の案内や利用方法に関する資料です。', sort_order: 20, registered: true, document_count: 1, ready_count: 1 },
    { name: 'draft', label: '準備中の資料', description: '', sort_order: 30, registered: true, document_count: 0, ready_count: 0 },
    { name: 'memo', label: 'memo', description: '', sort_order: null, registered: false, document_count: 1, ready_count: 1 },
  ],
};

/** GET /targets・GET /doc-types への応答。既定は「サーバーが資料の DB を使っていない」（対象の機能が出ない）。 */
export type ClassificationsHandler = () => Response | Promise<Response>;

/** GET /targets の正常な応答（ザーハード 3 件・みなとコーヒー 1 件・準備中の対象）。 */
export const TARGETS_BODY = {
  configured: true,
  targets: [
    { name: 'zahard', label: 'ザーハード', description: '架空の惑星ザーハードの設定資料の対象です。', sort_order: 10, document_count: 3, ready_count: 3 },
    { name: 'minato-coffee', label: 'みなとコーヒー', description: '', sort_order: 20, document_count: 1, ready_count: 1 },
    { name: 'draft', label: '準備中の対象', description: '', sort_order: 30, document_count: 1, ready_count: 0 },
  ],
};

/** GET /doc-types の正常な応答。 */
export const DOC_TYPES_BODY = {
  configured: true,
  doc_types: [
    { name: 'setting', label: '設定資料', description: '', sort_order: 10, document_count: 3, ready_count: 3 },
    { name: 'store-guide', label: '店舗案内', description: '', sort_order: 20, document_count: 1, ready_count: 1 },
  ],
};

/**
 * fetch を差し替える。/health は常に mode=mock を返し、/chat/stream は handler に渡す。
 * /threads 系は threads に渡す（渡さなければ、空の一覧を返す）。
 * handler の第 2 引数は /chat/stream の呼び出し回数（1 始まり）。
 */
export function installFetch(
  handler: ChatHandler,
  threads?: ThreadsHandler,
  collections?: CollectionsHandler,
  classifications?: { targets?: ClassificationsHandler; docTypes?: ClassificationsHandler },
): FetchHarness {
  const bodies: Array<Record<string, unknown>> = [];
  const inits: RequestInit[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/health')) {
      return jsonResponse({ status: 'ok', mode: 'mock' });
    }
    // 資料グループの一覧。チャットの呼び出し回数には数えない
    if (/\/collections$/.test(url)) {
      return collections ? collections() : jsonResponse({ configured: false, collections: [] });
    }
    // 対象と資料種別の一覧。チャットの呼び出し回数には数えない
    if (/\/targets$/.test(url)) {
      return classifications?.targets ? classifications.targets() : jsonResponse({ configured: false, targets: [] });
    }
    if (/\/doc-types$/.test(url)) {
      return classifications?.docTypes ? classifications.docTypes() : jsonResponse({ configured: false, doc_types: [] });
    }
    // 会話の一覧と削除（ログイン中だけ呼ばれる）。チャットの呼び出し回数には数えない
    if (/\/threads(\/|$)/.test(url)) {
      const method = init?.method ?? 'GET';
      return threads ? threads(url, method, init ?? {}) : jsonResponse({ threads: [] });
    }
    const body = JSON.parse(String(init?.body ?? '{}')) as Record<string, unknown>;
    bodies.push(body);
    inits.push(init ?? {});
    return handler(body, bodies.length, init ?? {});
  });
  vi.stubGlobal('fetch', fetchMock);
  return { fetchMock, chatBodies: () => bodies, chatInits: () => inits };
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
