import { Background, Controls, Handle, MarkerType, Position, ReactFlow, useReactFlow } from '@xyflow/react';
import type { Edge, Node, NodeProps } from '@xyflow/react';
import '@xyflow/react/dist/style.css';
import { useEffect, useMemo } from 'react';
import type { GraphEdge, GraphNode } from '../api/graph';
import { EDGE_LABELS, IMPACT_LABELS, NODE_HEIGHT, NODE_WIDTH, edgeKey } from '../lib/relationGraph';
import type { ImpactKind, Placed } from '../lib/relationGraph';

export interface RelationCanvasProps {
  placed: Placed[];
  edges: GraphEdge[];
  selected: string | null;
  kinds: Map<string, ImpactKind>;
  /** 影響をたどった辺（強調する）。 */
  walked: Set<string>;
  onSelect: (id: string) => void;
}

interface ClauseData extends Record<string, unknown> {
  node: GraphNode;
  kind: ImpactKind | null;
  selected: boolean;
  dimmed: boolean;
  onSelect: (id: string) => void;
}

type ClauseNode = Node<ClauseData, 'clause'>;

/** 項・脚注 1 つ。色だけに頼らないよう、影響の種類は文字（起点・直接・間接）でも示す。 */
function Clause({ data }: NodeProps<ClauseNode>) {
  const { node, kind, selected, dimmed, onSelect } = data;
  const classes = ['clause', kind ? `clause-${kind}` : '', dimmed ? 'clause-dimmed' : ''].filter(Boolean);
  return (
    <>
      <Handle type="target" position={Position.Left} isConnectable={false} />
      <button
        type="button"
        className={`${classes.join(' ')} nodrag nopan`}
        aria-pressed={selected}
        aria-label={`${node.label}${kind ? `（${IMPACT_LABELS[kind]}）` : ''}`}
        style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}
        onClick={() => onSelect(node.id)}
      >
        <span className="clause-head">
          <span className="clause-label">{node.label}</span>
          {kind && <span className="clause-badge">{IMPACT_LABELS[kind]}</span>}
        </span>
        <span className="clause-text">{node.text}</span>
      </button>
      <Handle type="source" position={Position.Right} isConnectable={false} />
    </>
  );
}

const nodeTypes = { clause: Clause };

/** 影響が分かったら、起点とその先が見えるところへ、図を動かす（図の中の別の場所にあっても見失わない）。 */
function FocusOnImpact({ ids }: { ids: string[] }) {
  const { fitView } = useReactFlow();
  const key = ids.join(',');
  useEffect(() => {
    if (!key) return;
    const reduced = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
    void fitView({ nodes: key.split(',').map((id) => ({ id })), padding: 0.2, maxZoom: 1, duration: reduced ? 0 : 300 });
  }, [key, fitView]);
  return null;
}

export default function RelationCanvas({ placed, edges, selected, kinds, walked, onSelect }: RelationCanvasProps) {
  const hasImpact = kinds.size > 0;

  const nodes = useMemo<ClauseNode[]>(
    () =>
      placed.map(({ node, x, y }) => ({
        id: node.id,
        type: 'clause',
        position: { x, y },
        draggable: false,
        selectable: false,
        data: {
          node,
          kind: kinds.get(node.id) ?? null,
          selected: node.id === selected,
          dimmed: hasImpact && !kinds.has(node.id),
          onSelect,
        },
      })),
    [placed, kinds, selected, hasImpact, onSelect],
  );

  const flowEdges = useMemo<Edge[]>(
    () =>
      edges.map((e) => {
        const on = walked.has(edgeKey(e));
        return {
          id: edgeKey(e),
          source: e.src,
          target: e.dst,
          label: EDGE_LABELS[e.type],
          className: [`edge-${e.type}`, on ? 'edge-walked' : hasImpact ? 'edge-dimmed' : ''].filter(Boolean).join(' '),
          markerEnd: { type: MarkerType.ArrowClosed, width: 16, height: 16 },
          selectable: false,
          focusable: false,
        };
      }),
    [edges, walked, hasImpact],
  );

  return (
    <ReactFlow
      className="relation-flow"
      nodes={nodes}
      edges={flowEdges}
      nodeTypes={nodeTypes}
      colorMode="system"
      defaultViewport={{ x: 8, y: 8, zoom: 0.8 }}
      minZoom={0.2}
      maxZoom={1.5}
      zoomOnScroll={false}
      preventScrolling={false}
      nodesDraggable={false}
      nodesConnectable={false}
      elementsSelectable={false}
    >
      <FocusOnImpact ids={[...kinds.keys()]} />
      <Background />
      <Controls showInteractive={false} />
    </ReactFlow>
  );
}
