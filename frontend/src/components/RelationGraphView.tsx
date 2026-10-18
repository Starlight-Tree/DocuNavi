import { Suspense, lazy, useMemo, useState } from 'react';
import type { DocumentSummary } from '../api/documents';
import type { GraphEdge, GraphNode, Impact, RelationGraph } from '../api/graph';
import { IMPACT_HOPS } from '../api/graph';
import { useImpact, useRelationGraph } from '../hooks/useDocuments';
import {
  DEPENDENCY_TYPES,
  EDGE_LABELS,
  IMPACT_LABELS,
  classifyImpact,
  connectedIds,
  edgeKey,
  layoutGraph,
  selectableNodes,
} from '../lib/relationGraph';
import { Status } from './Status';

// 図の部品（React Flow と dagre）は大きいので、関係図を開いたときに読む
const RelationCanvas = lazy(() => import('./RelationCanvas'));

export const MSG_GRAPH_NOTE =
  '入寮規程の項どうしの関係です。矢印は「依存元 → 依存先」で、依存先を変えると依存元に影響が及びます。条文の言い回し（「前項」「次のとおり」「前項の定めにかかわらず」と、決まった用語）から機械的に拾った関係で、読み落としや拾いすぎがあります。';
export const MSG_GRAPH_EMPTY = '関係を表示できる項がありません。';
export const MSG_NO_SELECTION = '項を選ぶと、本文・出典・影響を表示します。';
export const MSG_NO_IMPACT = '影響を受ける項はありません。';

/** 条の見出し（「第13条 寮費は、…」）から「第13条」だけを取り出す。 */
function articleNumber(label: string | undefined): string | null {
  return label?.match(/^第\d+条/)?.[0] ?? null;
}

function pageText(node: GraphNode): string {
  return node.page === null ? '' : `p.${node.page}`;
}

export function RelationGraphView({ document }: { document: DocumentSummary }) {
  const [state, reload] = useRelationGraph(document.id);
  if (state.status !== 'ready') return <Status state={state} onRetry={reload} />;
  return <RelationGraphReady document={document} graph={state.data} />;
}

function RelationGraphReady({ document, graph }: { document: DocumentSummary; graph: RelationGraph }) {
  const [selected, setSelected] = useState<string | null>(null);
  const layout = useMemo(() => layoutGraph(graph), [graph]);
  const nodes = useMemo(() => new Map(graph.nodes.map((n) => [n.id, n])), [graph]);
  const options = useMemo(() => selectableNodes(graph), [graph]);
  const connected = useMemo(() => connectedIds(graph), [graph]);
  const [impactState, reloadImpact] = useImpact(document.id, selected);
  const impact = selected !== null && impactState.status === 'ready' ? impactState.data : null;
  const kinds = useMemo(() => classifyImpact(impact), [impact]);
  const walked = useMemo(() => new Set((impact?.edges ?? []).map(edgeKey)), [impact]);

  if (layout.placed.length === 0) return <p>{MSG_GRAPH_EMPTY}</p>;
  const node = selected === null ? null : (nodes.get(selected) ?? null);

  return (
    <section className="relation" aria-label="関係図">
      <p className="documents-note" role="note">
        {MSG_GRAPH_NOTE}
      </p>
      <div className="relation-toolbar">
        <label className="relation-select">
          <span>項を選ぶ</span>
          <select value={selected ?? ''} onChange={(event) => setSelected(event.target.value || null)}>
            <option value="">（選んでいません）</option>
            {options.map((n) => (
              <option key={n.id} value={n.id}>
                {connected.has(n.id) ? n.label : `${n.label}（図にない）`}
              </option>
            ))}
          </select>
        </label>
        <ul className="relation-legend" aria-label="凡例">
          <li>
            <span className="legend-swatch legend-start" aria-hidden="true" />
            起点
          </li>
          <li>
            <span className="legend-swatch legend-direct" aria-hidden="true" />
            直接（1 ホップ）
          </li>
          <li>
            <span className="legend-swatch legend-indirect" aria-hidden="true" />
            間接（2 ホップ）
          </li>
        </ul>
      </div>
      <div className="relation-canvas">
        <Suspense fallback={<p role="status">読み込み中…</p>}>
          <RelationCanvas
            placed={layout.placed}
            edges={layout.edges}
            selected={selected}
            kinds={kinds}
            walked={walked}
            onSelect={setSelected}
          />
        </Suspense>
      </div>
      <aside className="relation-panel" aria-label="選んだ項の詳細">
        {node ? (
          <NodeDetail
            document={document}
            graph={graph}
            node={node}
            nodes={nodes}
            impact={impact}
            impactState={impactState}
            onRetry={reloadImpact}
          />
        ) : (
          <p className="relation-hint">{MSG_NO_SELECTION}</p>
        )}
      </aside>
    </section>
  );
}

function dependsOn(graph: RelationGraph, id: string): GraphEdge[] {
  return graph.edges.filter((e) => e.src === id && DEPENDENCY_TYPES.includes(e.type));
}

function NodeDetail({
  document,
  graph,
  node,
  nodes,
  impact,
  impactState,
  onRetry,
}: {
  document: DocumentSummary;
  graph: RelationGraph;
  node: GraphNode;
  nodes: Map<string, GraphNode>;
  impact: Impact | null;
  impactState: ReturnType<typeof useImpact>[0];
  onRetry: () => void;
}) {
  const outgoing = dependsOn(graph, node.id);
  const name = graph.title ?? document.title ?? document.source;

  return (
    <>
      <h3 className="relation-heading">{node.label}</h3>
      <dl className="relation-facts">
        <dt>本文</dt>
        <dd className="relation-text">{node.text}</dd>
        <dt>出典</dt>
        <dd>
          {name}
          {node.page !== null && `　${pageText(node)}`}
          <span className="document-file">{document.source}</span>
        </dd>
      </dl>

      <h4 className="relation-sub">この項が依存している先</h4>
      {outgoing.length === 0 ? (
        <p className="relation-hint">ありません。</p>
      ) : (
        <ul className="relation-list">
          {outgoing.map((e) => (
            <li key={edgeKey(e)}>
              <span className="relation-edge-type">{EDGE_LABELS[e.type]}</span>
              {nodes.get(e.dst)?.label ?? e.dst}
              <span className="relation-evidence">{`根拠「${e.evidence}」 p.${e.page}`}</span>
            </li>
          ))}
        </ul>
      )}

      <h4 className="relation-sub">{`この項を変えたときの影響（${IMPACT_HOPS} ホップまで）`}</h4>
      {impactState.status !== 'ready' ? (
        <Status state={impactState} onRetry={onRetry} />
      ) : impact && impact.impacted.length === 0 ? (
        <p className="relation-hint">{MSG_NO_IMPACT}</p>
      ) : (
        impact && (
          <>
            <ul className="relation-list">
              {impact.impacted.map((i) => {
                const target = nodes.get(i.id);
                const kind = i.hops <= 1 ? 'direct' : 'indirect';
                return (
                  <li key={i.id}>
                    <span className={`impact-badge impact-${kind}`}>{IMPACT_LABELS[kind]}</span>
                    {target?.label ?? i.id}
                    {target && target.page !== null && <span className="relation-evidence">{pageText(target)}</span>}
                  </li>
                );
              })}
            </ul>
            {impact.articles.length > 0 && (
              <p className="relation-articles">
                {`影響が及ぶ条: ${impact.articles.map((a) => articleNumber(nodes.get(a)?.label) ?? a).join('、')}`}
              </p>
            )}
          </>
        )
      )}
    </>
  );
}
