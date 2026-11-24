import { useId } from "react";
import type { GraphTreeData, GraphTreeNode } from "../lib/materialsApi";
import type { ReviewKnowledgePoint, reviewStatusLabels } from "../lib/reviewWorkbench";

const importanceLabels: Record<string, string> = { must: "核心", freq: "常用", key: "重点", info: "补充", basic: "基础", high: "重点", medium: "常用", low: "拓展" };
const rowHeight = 110;

function layout(graph: Array<[string, ReviewKnowledgePoint]>) {
  const pending = new Map(graph);
  const positions = new Map<string, { x: number; y: number; width: number }>();
  let row = 0;
  while (pending.size) {
    const next = [...pending].filter(([, point]) => point.deps.every((dep) => !pending.has(dep)));
    // Older results can contain a dependency cycle. Show those nodes on the remaining rows.
    const layer = next.length ? next : [...pending];
    for (let start = 0; start < layer.length; start += 3) {
      const group = layer.slice(start, start + 3);
      group.forEach(([id], index) => {
        positions.set(id, { x: (index + 0.5) * 100 / group.length, y: row * rowHeight + 48, width: Math.min(86 / group.length, 55) });
        pending.delete(id);
      });
      row++;
    }
  }
  return { positions, height: row * rowHeight };
}

function trace(id: string, index: Record<string, ReviewKnowledgePoint>, direction: "deps" | "dependents") {
  const seen = new Set([id]);
  const pending = [id];
  while (pending.length) {
    for (const next of index[pending.pop()!]?.[direction] ?? []) {
      if (seen.has(next)) continue;
      seen.add(next);
      pending.push(next);
    }
  }
  return seen;
}

function StructureTree({ nodes }: { nodes: GraphTreeNode[] }) {
  return <ul>{nodes.map((node) => (
    <li key={node.id}>
      {node.label} {node.summary && <small className="review-muted">{node.summary}</small>}
      {node.children?.length ? <StructureTree nodes={node.children} /> : null}
    </li>
  ))}</ul>;
}

export function ReviewGraph({
  index, selected, statuses, onSelect, go, annotation, summary, legacyTree,
}: {
  index: Record<string, ReviewKnowledgePoint>;
  selected?: string;
  statuses: Record<string, keyof typeof reviewStatusLabels>;
  onSelect: (kcId?: string) => void;
  go: (kcId: string, view: "notes" | "quiz") => void;
  annotation: (kcId: string) => React.ReactNode;
  summary?: string;
  legacyTree: GraphTreeData | null;
}) {
  const id = useId();
  const graph = Object.entries(index).filter(([, point]) => point.graph_node_id);
  const { positions, height } = layout(graph);
  const active = selected && positions.has(selected) ? selected : undefined;
  const upstream = active ? trace(active, index, "deps") : new Set<string>();
  const downstream = active ? trace(active, index, "dependents") : new Set<string>();
  const point = active ? index[active] : null;
  if (graph.length === 0) {
    return <div className="review-graph">
      <h2>知识图谱</h2>
      {legacyTree?.nodes?.length ? <StructureTree nodes={legacyTree.nodes} /> : <p>暂无可用的图谱节点。</p>}
    </div>;
  }
  return (
    <div className="review-graph">
      <h2>知识点依赖图</h2>
      <p className="review-muted">从上到下按前置关系排列。单击查看详情，双击回看笔记。</p>
      <div className="review-graph-canvas" style={{ height }}>
        <svg viewBox={`0 0 100 ${height}`} preserveAspectRatio="none" aria-hidden="true">
          <defs>
            <marker id={id} viewBox="0 0 6 6" refX="5" refY="3" markerWidth="5" markerHeight="5" orient="auto">
              <path d="M0 0L6 3L0 6Z" fill="context-stroke" />
            </marker>
          </defs>
          {graph.flatMap(([to, item]) => item.deps.map((from) => {
            const a = positions.get(from);
            const b = positions.get(to);
            if (!a || !b) return null;
            const y1 = a.y + 32, y2 = b.y - 33, mid = (y1 + y2) / 2;
            const related = upstream.has(from) && upstream.has(to) || downstream.has(from) && downstream.has(to);
            return <path key={from + ":" + to} d={`M${a.x},${y1} C${a.x},${mid} ${b.x},${mid} ${b.x},${y2}`}
              className={related ? "is-related" : active ? "is-dim" : undefined}
              vectorEffect="non-scaling-stroke" markerEnd={`url(#${id})`} />;
          }))}
        </svg>
        {graph.map(([kcId, item]) => {
          const pos = positions.get(kcId)!;
          return (
            <button key={kcId} type="button" data-review-graph-node={kcId} data-review-status={statuses[kcId]}
              aria-pressed={active === kcId} title={item.label}
              className={active && !upstream.has(kcId) && !downstream.has(kcId) ? "is-dim" : undefined}
              style={{ left: pos.x + "%", top: pos.y, width: pos.width + "%" }}
              onClick={() => onSelect(kcId)}
              onDoubleClick={() => item.note_anchor && go(kcId, "notes")}>
              <span>{item.label}</span>
              {item.importance && <small>{importanceLabels[item.importance] ?? item.importance}</small>}
            </button>
          );
        })}
      </div>
      {point && active && (
        <aside className="review-graph-detail" aria-label="知识点详情">
          <div className="review-result-heading">
            <strong>{point.label}</strong>
            <button type="button" className="review-button" onClick={() => onSelect(undefined)}>关闭详情</button>
          </div>
          {summary && <div className="epa-notes-article review-graph-summary" dangerouslySetInnerHTML={{ __html: summary }} />}
          {point.deps.length > 0 && <p className="review-crosslinks">前置：
            {point.deps.map((dep) => index[dep]?.graph_node_id && (
              <button type="button" key={dep} className="review-link" onClick={() => onSelect(dep)}>{index[dep].label}</button>
            ))}
          </p>}
          <div className="review-result-links">
            {point.note_anchor && <button type="button" className="review-primary" onClick={() => go(active, "notes")}>读这一节笔记</button>}
            {point.question_ids.length > 0 && <button type="button" className="review-button" onClick={() => go(active, "quiz")}>做题</button>}
          </div>
          {annotation(active)}
        </aside>
      )}
    </div>
  );
}

