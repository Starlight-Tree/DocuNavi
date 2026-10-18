/**
 * バックエンド (FastAPI) の /chat/stream と /health を呼ぶための部品。
 *
 * /chat/stream は AI SDK の UI message stream（SSE）を返すので、読み取りは @ai-sdk/react の useChat に任せる。
 * ここにあるのは、そのトランスポートに渡す fetch（HTTP エラーを利用者向けの日本語にする）と、/health の取得。
 */

export const MAX_MESSAGE_LENGTH = 2000;
export const MAX_THREAD_ID_LENGTH = 128;
export const THREAD_ID_PATTERN = /^[A-Za-z0-9_-]+$/;

/** 回答の 1 ターンに付くメタデータ（UI message stream の messageMetadata）。届いた分だけが入る。 */
export interface ChatMetadata {
  /** 会話 ID。最初のチャンク（start）で届く。 */
  thread_id?: string;
  /** "mock" = ダミー応答 / "gemini" = 本物の LLM。start で届く。 */
  mode?: string;
  /** 回答の根拠として検索された設定資料。未検索なら空文字。完了の直前に届く。 */
  context?: string;
}

export interface HealthResponse {
  status: string;
  mode: string;
}

export const CHAT_STREAM_PATH = '/chat/stream';

const MSG_NETWORK =
  'サーバーに接続できません。ネットワークやサーバーの起動状態を確認して、もう一度お試しください。';
const MSG_TOO_LONG = `メッセージが長すぎます。${MAX_MESSAGE_LENGTH} 文字以内にしてください。`;
const MSG_EMPTY = 'メッセージを入力してください。';
const MSG_BAD_THREAD =
  '会話 ID が正しくありません。「新しい会話」を押して、最初からやり直してください。';
const MSG_INVALID_INPUT = '入力内容に問題があります。内容を確認して、もう一度お試しください。';
export const MSG_BAD_BODY = 'サーバーから想定外の応答が返りました。しばらく待ってから再試行してください。';

/** API が返したエラー。message は利用者にそのまま見せられる日本語。 */
export class ChatApiError extends Error {
  /** HTTP ステータス。ネットワーク失敗など、応答が無い場合は 0。 */
  readonly status: number;
  /** 429 のときの待ち時間（秒）。Retry-After ヘッダが整数で付いていた場合のみ。 */
  readonly retryAfterSeconds?: number;

  constructor(status: number, message: string, retryAfterSeconds?: number) {
    super(message);
    this.name = 'ChatApiError';
    this.status = status;
    if (retryAfterSeconds !== undefined) {
      this.retryAfterSeconds = retryAfterSeconds;
    }
  }
}

export function endpoint(path: string): string {
  const base = (import.meta.env.VITE_API_BASE_URL ?? '').replace(/\/+$/, '');
  return `${base}${path}`;
}

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** fetch を AbortSignal で中断したときの例外か。DOMException は realm によって Error を継承しないため名前で見る。 */
export function isAbortError(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    (error as { name?: unknown }).name === 'AbortError'
  );
}

/** FastAPI の 422（detail が配列）を、利用者向けの 1 文にする。 */
function validationMessage(detail: unknown): string {
  if (Array.isArray(detail)) {
    for (const item of detail) {
      if (!isRecord(item)) continue;
      const loc = Array.isArray(item.loc) ? item.loc.map(String) : [];
      const type = typeof item.type === 'string' ? item.type : '';
      if (loc.includes('thread_id')) return MSG_BAD_THREAD;
      if (loc.includes('message')) {
        if (type.includes('too_long')) return MSG_TOO_LONG;
        if (type.includes('too_short') || type === 'missing') return MSG_EMPTY;
      }
    }
  }
  return MSG_INVALID_INPUT;
}

// バックエンドが意図して detail（固定の日本語メッセージ）を返すステータス。
// それ以外（404/405 などの "Not Found" や、ゲートウェイの英語メッセージ）は利用者に見せない
const SERVER_DETAIL_STATUSES = new Set([401, 429, 500, 502, 503, 504]);
const JAPANESE_TEXT = /[぀-ヿ㐀-鿿]/;

/** サーバーが固定の日本語メッセージとして送った文か。error チャンクの本文をそのまま見せてよいかの判定に使う。 */
export function isJapaneseMessage(text: string): boolean {
  return JAPANESE_TEXT.test(text.trim());
}

function serverDetailMessage(status: number, detail: unknown): string | null {
  if (typeof detail !== 'string' || !SERVER_DETAIL_STATUSES.has(status)) return null;
  const text = detail.trim();
  return JAPANESE_TEXT.test(text) ? text : null;
}

/** 本文がエラー JSON として読めない・detail が無いときの、ステータス別の一般的な文言。 */
function genericMessage(status: number): string {
  if (status === 404) {
    return `API が見つかりません（HTTP ${status}）。サーバーの設定を確認してください。`;
  }
  if (status === 502 || status === 503 || status === 504) {
    return `サーバーが一時的に応答できません（HTTP ${status}）。しばらく待ってから再試行してください。`;
  }
  if (status >= 500) {
    return `サーバーでエラーが発生しました（HTTP ${status}）。しばらく待ってから再試行してください。`;
  }
  return `リクエストを処理できませんでした（HTTP ${status}）。`;
}

function parseRetryAfter(response: Response): number | undefined {
  const raw = response.headers.get('Retry-After')?.trim() ?? '';
  // HTTP-date 形式は扱わない。バックエンドは整数秒だけを返す
  if (!/^\d+$/.test(raw)) return undefined;
  const seconds = Number(raw);
  return seconds > 0 ? seconds : undefined;
}

async function toApiError(response: Response): Promise<ChatApiError> {
  const { status } = response;
  let detail: unknown;
  try {
    const body: unknown = await response.json();
    detail = isRecord(body) ? body.detail : undefined;
  } catch {
    detail = undefined; // JSON ではない本文（プロキシの HTML エラーページなど）
  }

  if (status === 422) {
    return new ChatApiError(status, validationMessage(detail));
  }
  const message = serverDetailMessage(status, detail) ?? genericMessage(status);
  return new ChatApiError(
    status,
    message,
    status === 429 ? parseRetryAfter(response) : undefined,
  );
}

async function request(url: string, init?: RequestInit): Promise<Response> {
  try {
    return await fetch(url, init);
  } catch (error) {
    if (isAbortError(error)) throw error; // 呼び出し側が中断したものはそのまま伝える
    throw new ChatApiError(0, MSG_NETWORK);
  }
}

/**
 * AI SDK のトランスポートに渡す fetch。
 *
 * - ネットワーク失敗は、日本語の ChatApiError（status 0）にする
 * - HTTP エラー（本文を送り始める前の失敗）は、固定メッセージ・Retry-After つきの ChatApiError にする
 * - 成功なのに SSE ではない応答（プロキシの HTML など）は、想定外の応答として扱う
 * 中断（AbortSignal）は AbortError のまま伝える。
 */
export async function chatFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await request(String(input), init);
  if (!response.ok) throw await toApiError(response);
  const type = response.headers.get('Content-Type') ?? '';
  if (!type.includes('text/event-stream')) {
    throw new ChatApiError(response.status, MSG_BAD_BODY);
  }
  return response;
}

/**
 * JSON を返す API 用の fetch（資料一覧など）。chatFetch と同じ形なので、createAuthedFetch の baseFetch に渡せる。
 * ネットワーク失敗と HTTP エラーは chatFetch と同じ ChatApiError にする。本文の形は呼び出し側が確かめる。
 */
export async function jsonFetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response> {
  const response = await request(String(input), init);
  if (!response.ok) throw await toApiError(response);
  return response;
}

/** GET /health。モードバッジ用なので、失敗しても致命的ではない。 */
export async function fetchHealth(signal?: AbortSignal): Promise<HealthResponse> {
  const init: RequestInit = { headers: { Accept: 'application/json' } };
  if (signal) init.signal = signal;

  const response = await request(endpoint('/health'), init);
  if (!response.ok) throw await toApiError(response);
  const body: unknown = await response.json().catch(() => null);
  if (!isRecord(body) || typeof body.status !== 'string' || typeof body.mode !== 'string') {
    throw new ChatApiError(response.status, MSG_BAD_BODY);
  }
  return { status: body.status, mode: body.mode };
}
