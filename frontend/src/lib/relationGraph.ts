/**
 * 関係図の、画面に依らない部分（どのノードを描くか・並べ方・影響の分類）。
 * 描くのは、依存の辺（references / exception_of / uses_term）に出てくる項・脚注だけ。
 * 依存の無い項は図に出さず、選択欄から選べる。`same_chapter`（所属）は依存ではないので描かない。
 */
import dagre from '@dagrejs/dagre';
import type { EdgeType, GraphEdge, GraphNode, Impact, RelationGraph } from '../api/graph';

export const DEPENDENCY_TYPES: readonly EdgeType[] = ['references', 'exception_of', 'uses_term'];

export const EDGE_LABELS: Record<EdgeType, string> = {
  references: '参照',
  exception_of: '例外',
  uses_term: '用語',
  same_chapter: '所属',
};

/** 影響の分類。start = 選んだ項、direct = 1 ホップ、indirect = 2 ホップ以上。 */
export type ImpactKind = 'start' | 'direct' | 'indirect';

export const IMPACT_LABELS: Record<ImpactKind, string> = {
  start: '起点',
  direct: '直接',
  indirect: '間接',
};

export const NODE_WIDTH = 188;
export const NODE_HEIGHT = 72;

export function edgeKey(edge: Pick<GraphEdge, 'type' | 'src' | 'dst'>): string {
  return `${edge.type}|${edge.src}|${edge.dst}`;
}

export function dependencyEdges(graph: RelationGraph): GraphEdge[] {
  return graph.edges.filter((e) => DEPENDENCY_TYPES.includes(e.type));
}

/** 影響の起点にできるノード（項と脚注）。 */
export function selectableNodes(graph: RelationGraph): GraphNode[] {
  return graph.nodes.filter((n) => n.type === 'paragraph' || n.type === 'footnote');
}

/** 依存の辺に出てくる項・脚注（図に描くもの）の id。 */
export function connectedIds(graph: RelationGraph): Set<string> {
  const ids = new Set<string>();
  for (const e of dependencyEdges(graph)) {
    ids.add(e.src);
    ids.add(e.dst);
  }
  return ids;
}

export interface Placed {
  node: GraphNode;
  x: number;
  y: number;
}

/** 並べた行の幅の上限と、つながりのまとまり（成分）どうしの間隔。 */
const ROW_WIDTH = 960;
const GAP = 40;

interface Block {
  placed: Placed[];
  width: number;
  height: number;
}

/** つながっている項・脚注のまとまり（成分）ごとに分ける。順番は、資料に出てくる順。 */
function components(nodes: GraphNode[], edges: GraphEdge[]): GraphNode[][] {
  const parent = new Map(nodes.map((n) => [n.id, n.id]));
  const find = (id: string): string => {
    let root = id;
    while (parent.get(root) !== root) root = parent.get(root) as string;
    parent.set(id, root);
    return root;
  };
  for (const e of edges) parent.set(find(e.src), find(e.dst));
  const groups = new Map<string, GraphNode[]>();
  for (const n of nodes) {
    const root = find(n.id);
    groups.set(root, [...(groups.get(root) ?? []), n]);
  }
  return [...groups.values()];
}

function layoutBlock(nodes: GraphNode[], edges: GraphEdge[]): Block {
  const ids = new Set(nodes.map((n) => n.id));
  const g = new dagre.graphlib.Graph();
  g.setGraph({ rankdir: 'LR', nodesep: 24, ranksep: 72 });
  g.setDefaultEdgeLabel(() => ({}));
  for (const n of nodes) g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT });
  for (const e of edges) if (ids.has(e.src) && ids.has(e.dst)) g.setEdge(e.src, e.dst);
  dagre.layout(g);
  const placed = nodes.map((node) => {
    const p = g.node(node.id);
    return { node, x: p.x - NODE_WIDTH / 2, y: p.y - NODE_HEIGHT / 2 };
  });
  const { width = 0, height = 0 } = g.graph();
  return { placed, width, height };
}

/**
 * 図に描くノードの位置。左が依存元、右が依存先（矢印は「依存元 → 依存先」）。
 * つながりのまとまりごとに dagre で並べ、まとまりを左から右・上から下へ、行の幅に収まるように敷き詰める
 * （そのまま dagre に任せると、まとまりが縦に積み上がって細長くなる）。
 * 位置は React Flow の左上の座標（dagre は中心を返すので、幅と高さの半分を引く）。
 */
export function layoutGraph(graph: RelationGraph): { placed: Placed[]; edges: GraphEdge[] } {
  const edges = dependencyEdges(graph);
  const ids = connectedIds(graph);
  const nodes = selectableNodes(graph).filter((n) => ids.has(n.id));

  const placed: Placed[] = [];
  let x = 0;
  let y = 0;
  let rowHeight = 0;
  for (const group of components(nodes, edges)) {
    const block = layoutBlock(group, edges);
    if (x > 0 && x + block.width > ROW_WIDTH) {
      x = 0;
      y += rowHeight + GAP;
      rowHeight = 0;
    }
    for (const p of block.placed) placed.push({ node: p.node, x: p.x + x, y: p.y + y });
    x += block.width + GAP;
    rowHeight = Math.max(rowHeight, block.height);
  }
  return { placed, edges };
}

/** 影響の分類。起点は `start`、その先は起点からの距離（1 = 直接、2 以上 = 間接）。 */
export function classifyImpact(impact: Impact | null): Map<string, ImpactKind> {
  const kinds = new Map<string, ImpactKind>();
  if (!impact) return kinds;
  for (const id of impact.start) kinds.set(id, 'start');
  for (const i of impact.impacted) kinds.set(i.id, i.hops <= 1 ? 'direct' : 'indirect');
  return kinds;
}
