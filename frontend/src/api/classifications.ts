/**
 * 資料の分類の一覧（GET /targets、GET /doc-types）。画面が、対象・資料種別の表示名・説明・資料の数を出すために読む（読み取り専用）。
 * サーバーは、見える資料のある分類だけを、見える資料の数だけで返す。
 * 認証のために、fetch は呼び出し側から受け取る（createAuthedFetch で包んだもの）。
 * 本文はサーバーの値なので信用せず、形を確かめる。形が違えば MSG_BAD_BODY。
 */
import { ChatApiError, MSG_BAD_BODY, isRecord, readEndpoint } from './chat';
import type { chatFetch } from './chat';

type Fetcher = typeof chatFetch;

/** 対象・資料種別の識別名の形（DB の制約・API と同じ）。`POST /chat` の `target`・資料の `target` / `doc_type` に渡す値。 */
export const CLASSIFICATION_NAME_PATTERN = /^[a-z0-9][a-z0-9-]{0,39}$/;

export interface ClassificationInfo {
  /** 識別名。変わらない。 */
  name: string;
  /** 画面に出す名前。 */
  label: string;
  description: string;
  sortOrder: number;
  /** 見える資料の数。 */
  documentCount: number;
  /** 検索の対象になる資料の数。0 は「準備中」（選べるが、見つかる資料はまだ無い）。 */
  readyCount: number;
}

export interface ClassificationCatalog {
  /** false = サーバーが資料の DB を使っていない（モックモードなど）。エラーではない。 */
  configured: boolean;
  items: ClassificationInfo[];
}

const isCount = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value >= 0;

function parseItem(value: unknown): ClassificationInfo | null {
  if (!isRecord(value)) return null;
  const { name, label, description, sort_order: sortOrder, document_count, ready_count } = value;
  if (typeof name !== 'string' || !CLASSIFICATION_NAME_PATTERN.test(name)) return null;
  if (label != null && typeof label !== 'string') return null;
  if (description != null && typeof description !== 'string') return null;
  if (sortOrder != null && (typeof sortOrder !== 'number' || !Number.isInteger(sortOrder))) return null;
  if (!isCount(document_count) || !isCount(ready_count)) return null;
  return {
    name,
    label: label?.trim() || name,
    description: description?.trim() ?? '',
    sortOrder: sortOrder ?? 0,
    documentCount: document_count,
    readyCount: ready_count,
  };
}

async function fetchCatalog(
  fetcher: Fetcher,
  path: string,
  key: 'targets' | 'doc_types',
  signal?: AbortSignal,
): Promise<ClassificationCatalog> {
  const init: RequestInit = { headers: { Accept: 'application/json' } };
  if (signal) init.signal = signal;
  const response = await fetcher(readEndpoint(path), init);
  const body: unknown = await response.json().catch(() => null);
  const list = isRecord(body) ? body[key] : undefined;
  if (!isRecord(body) || typeof body.configured !== 'boolean' || !Array.isArray(list)) {
    throw new ChatApiError(response.status, MSG_BAD_BODY);
  }
  const items: ClassificationInfo[] = [];
  for (const item of list) {
    const parsed = parseItem(item);
    if (!parsed) throw new ChatApiError(response.status, MSG_BAD_BODY);
    items.push(parsed);
  }
  return { configured: body.configured, items };
}

export const fetchTargets = (fetcher: Fetcher, signal?: AbortSignal) => fetchCatalog(fetcher, '/targets', 'targets', signal);

export const fetchDocTypes = (fetcher: Fetcher, signal?: AbortSignal) => fetchCatalog(fetcher, '/doc-types', 'doc_types', signal);
