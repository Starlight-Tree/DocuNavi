/**
 * 資料グループの一覧（GET /collections）。画面が、グループの表示名・説明・資料の数を出すために読む（読み取り専用）。
 * 認証のために、fetch は呼び出し側から受け取る（createAuthedFetch で包んだもの）。
 * 本文はサーバーの値なので信用せず、形を確かめる。形が違えば MSG_BAD_BODY。
 */
import { ChatApiError, MSG_BAD_BODY, isRecord, readEndpoint } from './chat';
import type { chatFetch } from './chat';

type Fetcher = typeof chatFetch;

/** 資料グループの名前の形（DB の制約・API と同じ）。検索範囲として `POST /chat` の `collection` に渡す値。 */
export const COLLECTION_NAME_PATTERN = /^[A-Za-z0-9_-]{1,40}$/;

export interface CollectionInfo {
  /** 検索範囲に渡す値（documents.collection）。変わらない。 */
  name: string;
  /** 画面に出す名前。未登録のグループは name と同じ。 */
  label: string;
  description: string;
  /** 登録した並び順。未登録は null。 */
  sortOrder: number | null;
  /** document_collections に登録されているか。 */
  registered: boolean;
  documentCount: number;
  /** 検索の対象になる資料の数。0 のグループは「準備中」（選べるが、資料はまだありません）。 */
  readyCount: number;
}

export interface CollectionCatalog {
  /** false = サーバーが資料の DB を使っていない（モックモードなど）。エラーではない。 */
  configured: boolean;
  collections: CollectionInfo[];
}

const isCount = (value: unknown): value is number => typeof value === 'number' && Number.isInteger(value) && value >= 0;

function parseCollection(value: unknown): CollectionInfo | null {
  if (!isRecord(value)) return null;
  const { name, label, description, sort_order: sortOrder, registered, document_count, ready_count } = value;
  if (typeof name !== 'string' || !COLLECTION_NAME_PATTERN.test(name)) return null;
  if (label != null && typeof label !== 'string') return null;
  if (description != null && typeof description !== 'string') return null;
  if (sortOrder != null && (typeof sortOrder !== 'number' || !Number.isInteger(sortOrder))) return null;
  if (typeof registered !== 'boolean') return null;
  if (!isCount(document_count) || !isCount(ready_count)) return null;
  return {
    name,
    label: label?.trim() || name,
    description: description?.trim() ?? '',
    sortOrder: sortOrder ?? null,
    registered,
    documentCount: document_count,
    readyCount: ready_count,
  };
}

export async function fetchCollections(fetcher: Fetcher, signal?: AbortSignal): Promise<CollectionCatalog> {
  const init: RequestInit = { headers: { Accept: 'application/json' } };
  if (signal) init.signal = signal;
  const response = await fetcher(readEndpoint('/collections'), init);
  const body: unknown = await response.json().catch(() => null);
  if (!isRecord(body) || typeof body.configured !== 'boolean' || !Array.isArray(body.collections)) {
    throw new ChatApiError(response.status, MSG_BAD_BODY);
  }
  const collections: CollectionInfo[] = [];
  for (const item of body.collections) {
    const parsed = parseCollection(item);
    if (!parsed) throw new ChatApiError(response.status, MSG_BAD_BODY);
    collections.push(parsed);
  }
  return { configured: body.configured, collections };
}
