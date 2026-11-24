import type { ArtifactDetailView, GraphTreeNode, OutputKind } from "./materialsApi";

export type ReviewKnowledgePoint = {
  label: string;
  importance: string;
  module: string;
  note_anchor: string | null;
  graph_node_id: string | null;
  question_ids: string[];
  pages: number[];
  deps: string[];
  dependents: string[];
};

export type ReviewPosition = { view: OutputKind; kcId?: string };
type ReviewProgress = { read: string[]; last?: string };
const progressEvent = "hhu-review-progress";

function storage(key: string) {
  try { return localStorage.getItem(key); } catch { return null; }
}

export function saveReviewPreference(key: string, value: string) {
  try { localStorage.setItem(key, value); } catch { /* Storage is optional. */ }
}

export function readReviewProgress(key: string): ReviewProgress {
  try {
    const value = JSON.parse(storage(key) ?? "null");
    if (Array.isArray(value?.read)) return value;
  } catch { /* An older browser value can be ignored. */ }
  return { read: [] };
}

export function saveReviewProgress(key: string, kcId: string) {
  const previous = readReviewProgress(key);
  if (previous.last === kcId) return;
  const read = previous.read.includes(kcId) ? previous.read : [...previous.read, kcId];
  saveReviewPreference(key, JSON.stringify({ read, last: kcId }));
  window.dispatchEvent(new Event(progressEvent));
}

export function subscribeReviewProgress(refresh: () => void) {
  window.addEventListener(progressEvent, refresh);
  window.addEventListener("storage", refresh);
  return () => {
    window.removeEventListener(progressEvent, refresh);
    window.removeEventListener("storage", refresh);
  };
}

export function reviewProgressKey(accountKey: string, artifactId: string) {
  return accountKey + ".review." + artifactId;
}

export function readCollapsedDirectory(accountKey: string) {
  return storage(accountKey + ".review-directory") === "collapsed";
}

export function outputAvailable(artifact: ArtifactDetailView, view: OutputKind) {
  const status = artifact[`${view}_status`];
  if (status) return status === "ready";
  return view === "notes" ? Boolean(artifact.notes_html)
    : view === "graph" ? Boolean(artifact.graph_tree)
    : artifact.questions.length > 0;
}

export function reviewHash(): (ReviewPosition & { artifactId: string; conversationId: string }) | null {
  const params = new URLSearchParams(location.hash.slice(1));
  const artifactId = params.get("review");
  const conversationId = params.get("conversation");
  const view = params.get("view");
  if (!artifactId || !conversationId || (view !== "notes" && view !== "graph" && view !== "quiz")) return null;
  return { artifactId, conversationId, view, kcId: params.get("kc") ?? undefined };
}

export function writeReviewHash(conversationId: string, artifactId: string, position: ReviewPosition) {
  const hash = new URLSearchParams({ review: artifactId, conversation: conversationId, view: position.view });
  if (position.kcId) hash.set("kc", position.kcId);
  history.replaceState(history.state, "", "#" + hash);
}

export function clearReviewHash() {
  if (reviewHash()) history.replaceState(history.state, "", location.pathname + location.search);
}

export function reviewKcIndex(artifact: ArtifactDetailView): Record<string, ReviewKnowledgePoint> {
  if (artifact.kc_index) return Object.assign(Object.create(null), artifact.kc_index);
  const anchors = new Set(Array.from(
    new DOMParser().parseFromString(artifact.notes_html ?? "", "text/html").querySelectorAll("h3[data-kc]"),
    (heading) => heading.getAttribute("data-kc"),
  ));
  const graph = new Map<string, GraphTreeNode>();
  const visit = (nodes: GraphTreeNode[]) => {
    for (const node of nodes) {
      if (node.kc_id && !graph.has(node.kc_id)) graph.set(node.kc_id, node);
      visit(node.children ?? []);
    }
  };
  visit(artifact.graph_tree?.nodes ?? []);
  const entries = new Map<string, ReviewKnowledgePoint>();
  type LegacyPoint = { id: string; label: string; importance?: string; source_refs?: string[]; deps?: string[] };
  const chapters = artifact.skeleton?.chapters as Array<{ label?: string; kcs?: LegacyPoint[] }> | undefined;
  for (const chapter of Array.isArray(chapters) ? chapters : []) {
    for (const point of chapter.kcs ?? []) {
      if (!point.id || entries.has(point.id)) continue;
      entries.set(point.id, {
        label: point.label, importance: point.importance ?? "", module: chapter.label ?? "",
        note_anchor: anchors.has(point.id) ? point.id : null,
        graph_node_id: graph.get(point.id)?.id ?? null, question_ids: [], pages: [],
        deps: point.deps ?? [], dependents: [],
      });
    }
  }
  for (const [id, node] of graph) {
    if (!entries.has(id)) entries.set(id, {
      label: node.label, importance: "", module: "", note_anchor: anchors.has(id) ? id : null,
      graph_node_id: node.id, question_ids: [], pages: [], deps: node.deps ?? [], dependents: [],
    });
  }
  for (const [id, point] of entries) {
    point.deps = [...new Set(point.deps.filter((dep) => dep !== id && entries.has(dep)))];
    for (const dep of point.deps) entries.get(dep)?.dependents.push(id);
  }
  for (const question of artifact.questions) entries.get(question.kc_id)?.question_ids.push(question.id);
  return Object.assign(Object.create(null), Object.fromEntries(entries));
}

export const reviewStatusLabels = {
  unread: "未读", read: "已读", correct: "全对", wrong: "有错题",
};

export function reviewStatus(questions: ArtifactDetailView["questions"], read: boolean): keyof typeof reviewStatusLabels {
  if (questions.some((q) => q.attempt?.is_correct === false)) return "wrong";
  if (questions.length && questions.every((q) => q.attempt?.is_correct === true)) return "correct";
  return read ? "read" : "unread";
}

