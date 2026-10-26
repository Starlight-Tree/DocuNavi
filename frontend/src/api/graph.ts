/**
 * 入寮規程の関係図（GET /documents/{id}/graph、GET /documents/{id}/impact）。読むだけ。
 * 辺の向きは「src が dst に依存する」。dst を変えると src に影響が及ぶ。
 */
import { ChatApiError, MSG_BAD_BODY, endpoint, isRecord } from './chat';
import type { chatFetch } from './chat';

type Fetcher = typeof chatFetch;

/** 関係図に対応している資料（サーバーが対応するのは入寮規程だけ）。 */
export const GRAPH_SOURCE = 'tsubasa_dorm_rules.pdf';

export type NodeType = 'chapter' | 'article' | 'paragraph' | 'footnote';
export type EdgeType = 'references' | 'exception_of' | 'same_chapter' | 'uses_term';

export interface GraphNode {
  id: string;
  type: NodeType;
  label: string;
  text: string | null;
  parent: string | null;
  page: number | null;
}

export interface GraphEdge {
  type: EdgeType;
  src: string;
  dst: string;
  evidence: string;
  page: number;
}

export interface RelationGraph {
  title: string | null;
  nodes: GraphNode[];
  edges: GraphEdge[];
}

export interface ImpactedNode {
  id: string;
  hops: number;
}

export interface Impact {
  node: string;
  hops: number;
  start: string[];
  impacted: ImpactedNode[];
  /** 影響が及ぶ条（起点の条を除く）。 */
  articles: string[];
  edges: GraphEdge[];
}

/** 影響をたどる段数。画面では 2 に固定する（直接 = 1、間接 = 2）。 */
export const IMPACT_HOPS = 2;

const NODE_TYPES: readonly string[] = ['chapter', 'article', 'paragraph', 'footnote'];
const EDGE_TYPES: readonly string[] = ['references', 'exception_of', 'same_chapter', 'uses_term'];

function bad(status: number): ChatApiError {
  return new ChatApiError(status, MSG_BAD_BODY);
}

function nullableString(value: unknown): string | null | undefined {
  if (value === null || value === undefined) return null;
  return typeof value === 'string' ? value : undefined;
}

function nullableNumber(value: unknown): number | null | undefined {
  if (value === null || value === undefined) return null;
  return typeof value === 'number' ? value : undefined;
}

function parseNode(value: unknown): GraphNode | null {
  if (!isRecord(value)) return null;
  const text = nullableString(value.text);
  const parent = nullableString(value.parent);
  const page = nullableNumber(value.page);
  if (
    typeof value.id !== 'string' ||
    typeof value.type !== 'string' ||
    !NODE_TYPES.includes(value.type) ||
    typeof value.label !== 'string' ||
    text === undefined ||
    parent === undefined ||
    page === undefined
  ) {
    return null;
  }
  return { id: value.id, type: value.type as NodeType, label: value.label, text, parent, page };
}

function parseEdge(value: unknown): GraphEdge | null {
  if (!isRecord(value)) return null;
  if (
    typeof value.type !== 'string' ||
    !EDGE_TYPES.includes(value.type) ||
    typeof value.src !== 'string' ||
    typeof value.dst !== 'string' ||
    typeof value.evidence !== 'string' ||
    typeof value.page !== 'number'
  ) {
    return null;
  }
  return { type: value.type as EdgeType, src: value.src, dst: value.dst, evidence: value.evidence, page: value.page };
}

function parseList<T>(value: unknown, parse: (item: unknown) => T | null, status: number): T[] {
  if (!Array.isArray(value)) throw bad(status);
  return value.map((item) => {
    const parsed = parse(item);
    if (!parsed) throw bad(status);
    return parsed;
  });
}

async function getJson(fetcher: Fetcher, path: string, signal?: AbortSignal): Promise<[number, unknown]> {
  const init: RequestInit = { headers: { Accept: 'application/json' } };
  if (signal) init.signal = signal;
  const response = await fetcher(endpoint(path), init);
  const body: unknown = await response.json().catch(() => null);
  return [response.status, body];
}

export async function fetchRelationGraph(
  fetcher: Fetcher,
  id: number,
  signal?: AbortSignal,
): Promise<RelationGraph> {
  const [status, body] = await getJson(fetcher, `/documents/${id}/graph`, signal);
  if (!isRecord(body)) throw bad(status);
  const title = nullableString(body.title);
  if (title === undefined) throw bad(status);
  return {
    title,
    nodes: parseList(body.nodes, parseNode, status),
    edges: parseList(body.edges, parseEdge, status),
  };
}

export async function fetchImpact(
  fetcher: Fetcher,
  id: number,
  node: string,
  signal?: AbortSignal,
): Promise<Impact> {
  const query = `node=${encodeURIComponent(node)}&hops=${IMPACT_HOPS}`;
  const [status, body] = await getJson(fetcher, `/documents/${id}/impact?${query}`, signal);
  if (
    !isRecord(body) ||
    typeof body.node !== 'string' ||
    typeof body.hops !== 'number' ||
    !Array.isArray(body.start) ||
    !body.start.every((s) => typeof s === 'string') ||
    !Array.isArray(body.articles) ||
    !body.articles.every((a) => typeof a === 'string')
  ) {
    throw bad(status);
  }
  const impacted = parseList(
    body.impacted,
    (item) =>
      isRecord(item) && typeof item.id === 'string' && typeof item.hops === 'number'
        ? { id: item.id, hops: item.hops }
        : null,
    status,
  );
  return {
    node: body.node,
    hops: body.hops,
    start: body.start as string[],
    impacted,
    articles: body.articles as string[],
    edges: parseList(body.edges, parseEdge, status),
  };
}
