import { describe, expect, it } from 'vitest';
import type { GraphEdge, GraphNode, Impact, RelationGraph } from '../api/graph';
import {
  NODE_HEIGHT,
  NODE_WIDTH,
  classifyImpact,
  connectedIds,
  dependencyEdges,
  edgeKey,
  layoutGraph,
  selectableNodes,
} from './relationGraph';

function paragraph(article: number, number: number): GraphNode {
  return {
    id: `a${article}p${number}`,
    type: 'paragraph',
    label: `第${article}条 第${number}項`,
    text: `第${article}条の${number}`,
    parent: `a${article}`,
    page: 1,
  };
}

function edge(type: GraphEdge['type'], src: string, dst: string): GraphEdge {
  return { type, src, dst, evidence: '前項', page: 1 };
}

const GRAPH: RelationGraph = {
  title: '規程',
  nodes: [
    { id: 'ch1', type: 'chapter', label: '第1章', text: null, parent: null, page: null },
    { id: 'a1', type: 'article', label: '第1条 …', text: null, parent: 'ch1', page: 1 },
    paragraph(1, 1),
    paragraph(1, 2),
    paragraph(2, 1), // 依存なし
    paragraph(3, 1),
    paragraph(3, 2),
    paragraph(4, 1),
    { id: 'fn1', type: 'footnote', label: '脚注 1', text: '注', parent: null, page: 1 },
  ],
  edges: [
    edge('same_chapter', 'a1', 'ch1'),
    edge('exception_of', 'a1p2', 'a1p1'),
    edge('references', 'a3p2', 'a3p1'),
    edge('uses_term', 'fn1', 'a3p1'),
    edge('uses_term', 'a4p1', 'a1p1'),
  ],
};

describe('関係図: 描くもの', () => {
  it('依存の辺（参照・例外・用語）だけを使い、所属（same_chapter）は使わない', () => {
    expect(dependencyEdges(GRAPH).map((e) => e.type)).toEqual(['exception_of', 'references', 'uses_term', 'uses_term']);
  });

  it('図に描くのは、依存の辺に出てくる項と脚注だけ。章・条・依存のない項は描かない', () => {
    expect([...connectedIds(GRAPH)].sort()).toEqual(['a1p1', 'a1p2', 'a3p1', 'a3p2', 'a4p1', 'fn1']);
    expect(layoutGraph(GRAPH).placed.map((p) => p.node.id).sort()).toEqual(['a1p1', 'a1p2', 'a3p1', 'a3p2', 'a4p1', 'fn1']);
  });

  it('選べるのは項と脚注だけ（依存のない項も選べる）', () => {
    expect(selectableNodes(GRAPH).map((n) => n.id)).toEqual(['a1p1', 'a1p2', 'a2p1', 'a3p1', 'a3p2', 'a4p1', 'fn1']);
  });

  it('辺が無いときは、何も描かない', () => {
    expect(layoutGraph({ title: null, nodes: GRAPH.nodes, edges: [] }).placed).toEqual([]);
  });
});

describe('関係図: 並べ方', () => {
  const { placed } = layoutGraph(GRAPH);
  const at = (id: string) => placed.find((p) => p.node.id === id)!;

  it('依存元が左、依存先が右（矢印は左から右へ向かう）', () => {
    expect(at('a1p2').x).toBeLessThan(at('a1p1').x);
    expect(at('a3p2').x).toBeLessThan(at('a3p1').x);
    expect(at('fn1').x).toBeLessThan(at('a3p1').x);
  });

  it('ノードどうしは重ならない', () => {
    for (const a of placed) {
      for (const b of placed) {
        if (a === b) continue;
        const overlapX = a.x < b.x + NODE_WIDTH && b.x < a.x + NODE_WIDTH;
        const overlapY = a.y < b.y + NODE_HEIGHT && b.y < a.y + NODE_HEIGHT;
        expect(overlapX && overlapY, `${a.node.id} と ${b.node.id}`).toBe(false);
      }
    }
  });

  it('座標は有限で、負にならない', () => {
    for (const p of placed) {
      expect(Number.isFinite(p.x) && Number.isFinite(p.y)).toBe(true);
      expect(p.x).toBeGreaterThanOrEqual(0);
      expect(p.y).toBeGreaterThanOrEqual(0);
    }
  });

  it('つながりの無いまとまりは、縦に積み上げず、行の幅に収まる限り横に並べる', () => {
    // a1p1・a1p2・a4p1 のまとまりと、a3p1・a3p2・fn1 のまとまり
    expect(at('a3p1').y).toBeLessThan(at('a1p1').y + 4 * NODE_HEIGHT);
    expect(at('a3p1').x).toBeGreaterThan(at('a1p1').x);
  });
});

describe('関係図: 影響の分類', () => {
  const impact: Impact = {
    node: 'a1p1',
    hops: 2,
    start: ['a1p1'],
    impacted: [
      { id: 'a1p2', hops: 1 },
      { id: 'a4p1', hops: 1 },
      { id: 'a9p1', hops: 2 },
    ],
    articles: ['a4', 'a9'],
    edges: [],
  };

  it('起点・直接（1 ホップ）・間接（2 ホップ）に分ける', () => {
    const kinds = classifyImpact(impact);
    expect(kinds.get('a1p1')).toBe('start');
    expect(kinds.get('a1p2')).toBe('direct');
    expect(kinds.get('a4p1')).toBe('direct');
    expect(kinds.get('a9p1')).toBe('indirect');
    expect(kinds.has('a3p1')).toBe(false);
  });

  it('影響がまだ無い（null）ときは空', () => {
    expect(classifyImpact(null).size).toBe(0);
  });

  it('辺の識別は、種類・依存元・依存先で決める', () => {
    expect(edgeKey(edge('references', 'a1p2', 'a1p1'))).toBe('references|a1p2|a1p1');
  });
});
