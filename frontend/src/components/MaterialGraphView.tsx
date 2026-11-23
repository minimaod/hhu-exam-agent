/**
 * 知识图谱交互视图组件。
 *
 * 来源与许可说明：
 * - 左根右叶树形布局、前置依赖虚线与枢纽节点（★）展示逻辑复用自
 *   WUBING2023/ExamPass-Assistant (commit 3812643baf5de8b92bac17fbb4e136adcc19c41b,
 *   `templates/graph.js`)，原项目采用 CC BY-NC 4.0 许可证。
 * - 本站适配修改：以 React 受控组件重构，将节点笔记持久化从 `localStorage` 迁移至
 *   绑定 `(owner_id, artifact_id, node_id)` 的后端接口，防止跨账号或跨资料串用。
 */

import { useEffect, useMemo, useRef, useState } from "react";
import type { GraphTreeData, GraphTreeNode } from "../lib/materialsApi.ts";

type PositionedNode = {
  node: GraphTreeNode;
  depth: number;
  x: number;
  y: number;
  w: number;
  h: number;
  parentId: string | null;
  branchIndex: number;
};

const BRANCH_COLORS = [
  "#5a7d9a",
  "#688b68",
  "#a87b54",
  "#846275",
  "#52838d",
  "#8e7d62",
];

const NODE_W = 168;
const NODE_H = 44;
const COL_STEP = 224;
const ROW_STEP = 60;
const PAD_X = 24;
const PAD_Y = 24;

function layoutGraphTree(tree: GraphTreeData | null): {
  items: PositionedNode[];
  byId: Map<string, PositionedNode>;
  byKcId: Map<string, PositionedNode>;
  width: number;
  height: number;
} {
  if (!tree || !tree.nodes?.length) {
    return { items: [], byId: new Map(), byKcId: new Map(), width: 600, height: 320 };
  }

  let rowCursor = 0;
  let maxDepth = 0;
  const items: PositionedNode[] = [];
  const byId = new Map<string, PositionedNode>();
  const byKcId = new Map<string, PositionedNode>();

  function visit(
    node: GraphTreeNode,
    depth: number,
    parentId: string | null,
    branchIndex: number,
  ): number {
    maxDepth = Math.max(maxDepth, depth);
    const children = node.children ?? [];
    let yCenter: number;
    if (children.length === 0) {
      yCenter = PAD_Y + rowCursor * ROW_STEP;
      rowCursor += 1;
    } else {
      const childYs = children.map((ch) => visit(ch, depth + 1, node.id, branchIndex));
      yCenter = (childYs[0] + childYs[childYs.length - 1]) / 2;
    }
    const pos: PositionedNode = {
      node,
      depth,
      x: PAD_X + depth * COL_STEP,
      y: yCenter,
      w: NODE_W,
      h: NODE_H,
      parentId,
      branchIndex,
    };
    items.push(pos);
    byId.set(node.id, pos);
    if (node.kc_id) byKcId.set(node.kc_id, pos);
    return yCenter;
  }

  tree.nodes.forEach((root, idx) => {
    visit(root, 0, null, idx % BRANCH_COLORS.length);
  });

  const width = PAD_X * 2 + (maxDepth + 1) * COL_STEP;
  const height = Math.max(320, PAD_Y * 2 + rowCursor * ROW_STEP);
  return { items, byId, byKcId, width, height };
}

export default function MaterialGraphView({
  graphTree,
  graphNotes,
  inline = false,
  startPage = 1,
  endPage = 0,
  onOpenPage,
  onSaveNote,
  onStudyKc,
}: {
  graphTree: GraphTreeData | null;
  graphNotes: Record<string, string>;
  inline?: boolean;
  startPage?: number;
  endPage?: number;
  onOpenPage?: (pageNo: number) => void;
  onSaveNote?: (nodeId: string, noteText: string) => Promise<void>;
  onStudyKc?: (kcId: string, mode: "explain" | "exercise") => void;
}) {
  const [search, setSearch] = useState("");
  const [zoom, setZoom] = useState<number | null>(null);
  const [view, setView] = useState<"list" | "graph" | null>(null);
  const [workspaceWidth, setWorkspaceWidth] = useState(0);
  const shellRef = useRef<HTMLDivElement | null>(null);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [noteDraft, setNoteDraft] = useState("");
  const [saving, setSaving] = useState(false);
  const [savedNotice, setSavedNotice] = useState<string | null>(null);

  const layout = useMemo(() => layoutGraphTree(graphTree), [graphTree]);
  const fit = Math.min(1, Math.max(0.2, (workspaceWidth - 20) / layout.width));
  const scale = zoom ?? (inline ? Math.max(0.72, fit) : fit);
  const showList = view === "list" || (view === null && !inline && fit < 0.72);
  const selectedItem = selectedId ? layout.byId.get(selectedId) ?? null : null;
  const sourcePages = new Set<number>();
  if (onOpenPage) {
    for (const ref of selectedItem?.node.source_refs ?? []) {
      const pageRef = ref.includes("/") ? ref.slice(ref.indexOf("/") + 1) : ref;
      for (const match of pageRef.matchAll(/(\d+)(?:\s*[-~–]\s*(\d+))?/g)) {
        const first = Number(match[1]);
        const last = Number(match[2] ?? first);
        if (first < 1 || last < first || last - first >= 200) continue;
        for (let page = Math.max(first, startPage); page <= Math.min(last, endPage); page += 1) {
          sourcePages.add(page);
        }
      }
    }
  }

  useEffect(() => {
    const shell = shellRef.current;
    if (!shell) return;
    const measure = () => setWorkspaceWidth(shell.clientWidth);
    measure();
    if (typeof ResizeObserver === "undefined") return;
    const observer = new ResizeObserver(measure);
    observer.observe(shell);
    return () => observer.disconnect();
  }, [layout.items.length]);

  useEffect(() => {
    if (!selectedId) {
      setNoteDraft("");
      return;
    }
    setNoteDraft(graphNotes[selectedId] ?? "");
    setSavedNotice(null);
  }, [selectedId, graphNotes]);

  if (!graphTree || layout.items.length === 0) {
    return <div className="empty-inline">当前产物暂未包含知识图谱数据。</div>;
  }

  const q = search.trim().toLowerCase();

  const handleSave = async () => {
    if (!selectedId || !onSaveNote) return;
    setSaving(true);
    setSavedNotice(null);
    try {
      await onSaveNote(selectedId, noteDraft);
      setSavedNotice("笔记已保存。");
    } catch (err) {
      setSavedNotice(err instanceof Error ? err.message : "笔记保存失败，请重试。");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="epa-graph-shell" ref={shellRef}>
      <div className="epa-graph-toolbar">
        <input
          type="search"
          aria-label="搜索知识点"
          className="epa-graph-search"
          placeholder="搜索知识点…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
        />
        <div className="epa-graph-zoom">
          <button type="button" className="quiet-button" aria-pressed={showList} onClick={() => setView("list")}>知识点目录</button>
          <button type="button" className="quiet-button" aria-pressed={!showList} onClick={() => setView("graph")}>关系图</button>
        </div>
        {!showList && <div className="epa-graph-zoom">
          <button
            type="button"
            className="ghost-btn compact-btn"
            aria-label="缩小图谱"
            onClick={() => setZoom(Math.max(0.2, scale - 0.1))}
          >
            -
          </button>
          <span className="muted-note">{Math.round(scale * 100)}%</span>
          <button
            type="button"
            className="ghost-btn compact-btn"
            aria-label="放大图谱"
            onClick={() => setZoom(Math.min(1.5, scale + 0.1))}
          >
            +
          </button>
          <button type="button" className="quiet-button" onClick={() => setZoom(inline ? fit : null)}>适应宽度</button>
        </div>}
      </div>

      <div className="epa-graph-workspace">
        {showList ? (
          <div className="epa-graph-directory">
            {layout.items.some(({ node }) => node.kc_id && `${node.label} ${node.summary}`.toLowerCase().includes(q)) ? (
              <ul aria-label="知识点目录">
                {layout.items.filter(({ node }) => node.kc_id && `${node.label} ${node.summary}`.toLowerCase().includes(q))
                  .map(({ node }) => (
                    <li key={node.id}>
                      <button type="button" aria-pressed={selectedId === node.id} onClick={() => setSelectedId(node.id)}>
                        <strong>{node.label}</strong>
                        {node.summary && <span>{node.summary}</span>}
                      </button>
                    </li>
                  ))}
              </ul>
            ) : <p className="page-message">没有找到对应的知识点。</p>}
          </div>
        ) : <div className="epa-graph-canvas-wrap" tabIndex={0} role="region" aria-label="关系图画布">
          <div className="epa-graph-scaled-stage" style={{ width: layout.width * scale, height: layout.height * scale }}>
          <div
            className="epa-graph-canvas"
            style={{
              width: layout.width,
              height: layout.height,
              transform: `scale(${scale})`,
              transformOrigin: "top left",
            }}
          >
            <svg
              className="epa-graph-svg"
              width={layout.width}
              height={layout.height}
            >
              {layout.items.map((item) => {
                if (!item.parentId) return null;
                const parent = layout.byId.get(item.parentId);
                if (!parent) return null;
                const x1 = parent.x + parent.w;
                const y1 = parent.y + parent.h / 2;
                const x2 = item.x;
                const y2 = item.y + item.h / 2;
                const mx = (x1 + x2) / 2;
                return (
                  <path
                    key={`edge-${parent.node.id}-${item.node.id}`}
                    d={`M ${x1} ${y1} C ${mx} ${y1}, ${mx} ${y2}, ${x2} ${y2}`}
                    fill="none"
                    stroke={BRANCH_COLORS[item.branchIndex]}
                    strokeWidth={1.8}
                    strokeOpacity={0.7}
                  />
                );
              })}

              {layout.items.flatMap((item) =>
                (item.node.deps ?? []).map((depKcId) => {
                  const depNode = layout.byKcId.get(depKcId);
                  if (!depNode) return null;
                  const x1 = depNode.x + depNode.w / 2;
                  const y1 = depNode.y + depNode.h;
                  const x2 = item.x + item.w / 2;
                  const y2 = item.y;
                  return (
                    <path
                      key={`dep-${depNode.node.id}-${item.node.id}`}
                      d={`M ${x1} ${y1} Q ${(x1 + x2) / 2 + 24} ${(y1 + y2) / 2}, ${x2} ${y2}`}
                      fill="none"
                      stroke="#b85d38"
                      strokeWidth={1.3}
                      strokeDasharray="4 4"
                      strokeOpacity={0.65}
                    />
                  );
                }),
              )}
            </svg>

            {layout.items.map((item) => {
              const matched =
                q.length > 0 &&
                (item.node.label.toLowerCase().includes(q) ||
                  item.node.summary.toLowerCase().includes(q));
              const hasNote = Boolean(graphNotes[item.node.id]?.trim());
              return (
                <button
                  key={item.node.id}
                  type="button"
                  className={`epa-graph-node ${
                    selectedId === item.node.id ? "is-selected" : ""
                  } ${matched ? "is-matched" : ""}`}
                  style={{
                    left: item.x,
                    top: item.y,
                    width: item.w,
                    minHeight: item.h,
                    borderLeftColor: BRANCH_COLORS[item.branchIndex],
                  }}
                  title={item.node.summary || item.node.label}
                  onClick={() => setSelectedId(item.node.id)}
                >
                  <span className="epa-graph-node-label">
                    {item.node.is_hub ? "★ " : ""}
                    {item.node.label}
                  </span>
                  {hasNote && <span className="epa-graph-note-dot" title="已有节点笔记" />}
                </button>
              );
            })}
          </div>
          </div>
        </div>}

        {selectedItem && <aside className="epa-graph-notes-pane">
            <>
              <h4 className="epa-graph-notes-title">
                {selectedItem.node.is_hub ? "★ " : ""}
                {selectedItem.node.label}
              </h4>
              {selectedItem.node.summary && (
                <p className="muted-note">{selectedItem.node.summary}</p>
              )}
              {sourcePages.size > 0 && <div className="flex flex-wrap gap-1">
                {Array.from(sourcePages).sort((a, b) => a - b).map((page) => (
                  <button key={page} type="button" className="quiet-button" onClick={() => onOpenPage?.(page)}>
                    原件第 {page} 页
                  </button>
                ))}
              </div>}
              {onSaveNote && <textarea
                rows={4}
                className="epa-textarea"
                aria-label="知识点笔记"
                placeholder="记录这个知识点…"
                value={noteDraft}
                onChange={(e) => setNoteDraft(e.target.value)}
              />}
              <div className="epa-graph-notes-actions">
                {onSaveNote && <button
                  type="button"
                  className="primary-btn compact-btn"
                  disabled={saving}
                  onClick={() => void handleSave()}
                >
                  {saving ? "保存中…" : "保存节点笔记"}
                </button>}
                {onStudyKc && selectedItem.node.kc_id && (
                  <button
                    type="button"
                    className="ghost-btn compact-btn"
                    onClick={() => onStudyKc(selectedItem.node.kc_id!, "explain")}
                  >
                    去聊天追问
                  </button>
                )}
                {savedNotice && <span className="muted-note">{savedNotice}</span>}
              </div>
            </>
        </aside>}
      </div>
    </div>
  );
}
