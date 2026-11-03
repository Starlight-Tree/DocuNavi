/**
 * 資料一覧（GET /documents、GET /documents/{id}/chunks）。DB に保存された資料を読むだけ。
 * 認証が要る構成のために、fetch は呼び出し側から受け取る（createAuthedFetch で包んだもの）。
 */
import { ChatApiError, MSG_BAD_BODY, endpoint, isRecord } from './chat';
import type { chatFetch } from './chat';

type Fetcher = typeof chatFetch;

export interface DocumentSummary {
  id: number;
  source: string;
  /** PDF の題名。無ければ null（画面はファイル名で代える）。 */
  title: string | null;
  /** 資料群の名前（zahard / cafe / guides など）。未設定、または項目が無い応答（古いサーバー）は null。 */
  collection: string | null;
  /** 対象の識別名（zahard / minato-coffee など）。未分類、または項目が無い応答（古いサーバー）は null。 */
  target: string | null;
  /** 資料種別の識別名（setting / manual など）。未分類、または項目が無い応答（古いサーバー）は null。 */
  docType: string | null;
  status: string;
  failureReason: string | null;
  pages: number | null;
  updatedAt: string | null;
  chunkCount: number;
  embeddedCount: number;
  /** 承認済みのヒーロー画像があるか（GET /documents/{id}/hero で取れる）。項目が無い応答（古いサーバー）は false。 */
  heroImage: boolean;
}

export interface DocumentList {
  /** false = サーバーに資料の DB が設定されていない（エラーではない）。documents が空なのとは別。 */
  configured: boolean;
  documents: DocumentSummary[];
}

export interface ChunkItem {
  chunkIndex: number;
  page: number | null;
  pageEnd: number | null;
  kind: string | null;
  heading: string | null;
  content: string;
  embedded: boolean;
  embeddingModel: string | null;
}

export interface DocumentDetail {
  document: DocumentSummary;
  chunks: ChunkItem[];
  /** 元の PDF を GET /documents/{id}/file で取れるか。項目が無い応答（古いサーバー）は false として読む。 */
  originalPdf: boolean;
}

export const MSG_PDF_UNAVAILABLE = '元PDFは利用できません。';

function bad(status: number): ChatApiError {
  return new ChatApiError(status, MSG_BAD_BODY);
}

function nullableNumber(value: unknown): number | null | undefined {
  if (value === null || value === undefined) return null;
  return typeof value === 'number' ? value : undefined;
}

function nullableString(value: unknown): string | null | undefined {
  if (value === null || value === undefined) return null;
  return typeof value === 'string' ? value : undefined;
}

function parseSummary(value: unknown): DocumentSummary | null {
  if (!isRecord(value)) return null;
  const pages = nullableNumber(value.pages);
  const title = nullableString(value.title);
  const collection = nullableString(value.collection);
  const target = nullableString(value.target);
  const docType = nullableString(value.doc_type);
  const failureReason = nullableString(value.failure_reason);
  const updatedAt = nullableString(value.updated_at);
  if (
    typeof value.id !== 'number' ||
    typeof value.source !== 'string' ||
    typeof value.status !== 'string' ||
    typeof value.chunk_count !== 'number' ||
    typeof value.embedded_count !== 'number' ||
    pages === undefined ||
    title === undefined ||
    collection === undefined ||
    target === undefined ||
    docType === undefined ||
    failureReason === undefined ||
    updatedAt === undefined
  ) {
    return null;
  }
  return {
    id: value.id,
    source: value.source,
    title: title?.trim() || null,
    collection: collection?.trim() || null,
    target: target?.trim() || null,
    docType: docType?.trim() || null,
    status: value.status,
    failureReason,
    pages,
    updatedAt,
    chunkCount: value.chunk_count,
    embeddedCount: value.embedded_count,
    heroImage: value.hero_image === true,
  };
}

function parseChunk(value: unknown): ChunkItem | null {
  if (!isRecord(value)) return null;
  const page = nullableNumber(value.page);
  const pageEnd = nullableNumber(value.page_end);
  const kind = nullableString(value.kind);
  const heading = nullableString(value.heading);
  const embeddingModel = nullableString(value.embedding_model);
  if (
    typeof value.chunk_index !== 'number' ||
    typeof value.content !== 'string' ||
    typeof value.embedded !== 'boolean' ||
    page === undefined ||
    pageEnd === undefined ||
    kind === undefined ||
    heading === undefined ||
    embeddingModel === undefined
  ) {
    return null;
  }
  return {
    chunkIndex: value.chunk_index,
    page,
    pageEnd,
    kind,
    heading,
    content: value.content,
    embedded: value.embedded,
    embeddingModel,
  };
}

async function getJson(fetcher: Fetcher, path: string, signal?: AbortSignal): Promise<[number, unknown]> {
  const init: RequestInit = { headers: { Accept: 'application/json' } };
  if (signal) init.signal = signal;
  const response = await fetcher(endpoint(path), init);
  const body: unknown = await response.json().catch(() => null);
  return [response.status, body];
}

export async function fetchDocuments(fetcher: Fetcher, signal?: AbortSignal): Promise<DocumentList> {
  const [status, body] = await getJson(fetcher, '/documents', signal);
  if (!isRecord(body) || typeof body.configured !== 'boolean' || !Array.isArray(body.documents)) {
    throw bad(status);
  }
  const documents: DocumentSummary[] = [];
  for (const item of body.documents) {
    const parsed = parseSummary(item);
    if (!parsed) throw bad(status);
    documents.push(parsed);
  }
  return { configured: body.configured, documents };
}

export async function fetchDocumentChunks(
  fetcher: Fetcher,
  id: number,
  signal?: AbortSignal,
): Promise<DocumentDetail> {
  const [status, body] = await getJson(fetcher, `/documents/${id}/chunks`, signal);
  if (!isRecord(body) || !Array.isArray(body.chunks)) throw bad(status);
  if (body.original_pdf !== undefined && typeof body.original_pdf !== 'boolean') throw bad(status);
  const document = parseSummary(body.document);
  if (!document) throw bad(status);
  const chunks: ChunkItem[] = [];
  for (const item of body.chunks) {
    const parsed = parseChunk(item);
    if (!parsed) throw bad(status);
    chunks.push(parsed);
  }
  return { document, chunks, originalPdf: body.original_pdf === true };
}

/**
 * 元の PDF（GET /documents/{id}/file）。認証付きの fetch で取り、Blob で返す（<iframe src> は Authorization を送れない）。
 * 404（ファイルが無い）と 409（内容が DB と違う）は、サーバーの文言ではなく「元PDFは利用できません」にする。
 */
export async function fetchDocumentPdf(fetcher: Fetcher, id: number, signal?: AbortSignal): Promise<Blob> {
  const init: RequestInit = { headers: { Accept: 'application/pdf' } };
  if (signal) init.signal = signal;
  let response: Response;
  try {
    response = await fetcher(endpoint(`/documents/${id}/file`), init);
  } catch (error) {
    if (error instanceof ChatApiError && (error.status === 404 || error.status === 409)) {
      throw new ChatApiError(error.status, MSG_PDF_UNAVAILABLE);
    }
    throw error;
  }
  if (!(response.headers.get('Content-Type') ?? '').includes('application/pdf')) throw bad(response.status);
  return response.blob();
}
