/**
 * F009 学习记忆接口。
 *
 * 只做三件事：读摘要、读书目、纠正/移除单条记忆。删除只写排除标记，
 * 原会话记录留在会话页，因此这里没有“删除会话”之类的能力。
 */

const PATH = "/api/learning-memory";

export type MemoryStatus = "ok" | "empty" | "unavailable";
export type MemoryVerdict =
  | "wrong_independent"
  | "correct_independent"
  | "after_reveal"
  | "insufficient"
  | "unclear"
  | "self_report"
  | "no_attempt";

export type MemorySource = {
  conversation_id: string;
  exercise_id: string | null;
  problem_id: string | null;
  question_id: string | null;
  paper_id: string | null;
  paper_title: string | null;
  number_label: string | null;
};

export type MemoryAttempt = {
  attempt_id: string;
  attempt_no: number;
  kind: string;
  verdict: MemoryVerdict;
  label: string;
  is_correct: boolean | null;
  answered_after_revealed: boolean;
  recorded_at: string;
  excluded: boolean;
};

export type MemoryEntry = {
  entry_id: string;
  kind: "exercise" | "attempt" | "selfreport";
  course: string | null;
  course_source: "user" | "paper" | "problem" | "unknown";
  knowledge_points: string[];
  knowledge_points_normalized: boolean[];
  statement_excerpt: string | null;
  verdict: MemoryVerdict;
  first_verdict: MemoryVerdict | null;
  recorded_at: string;
  first_at: string | null;
  wrong_at: string | null;
  validity: "ok" | "superseded" | "unverified" | "unavailable";
  validity_note: string | null;
  note: string | null;
  source: MemorySource;
  attempts: MemoryAttempt[];
};

export type MemoryGroup = {
  label: string;
  slug: string | null;
  normalized: boolean;
  wrong_count: number;
  correct_count: number;
  after_reveal_count: number;
  insufficient_count: number;
  unclear_count: number;
  no_attempt_count: number;
  self_report_count: number;
  excluded_evaluation_count: number;
  earlier_wrong_count: number;
  entry_ids: string[];
  latest_at: string | null;
  latest_wrong_at: string | null;
  suggestion: string | null;
};

export type MemoryCounts = {
  evidence: number;
  practices: number;
  questions: number;
  attempts: number;
  excluded: number;
  pending_course: number;
  wrong_independent: number;
  unclear: number;
  excluded_evaluation: number;
  earlier_wrong: number;
  superseded: number;
  unverified: number;
};

export type LearningMemoryView = {
  status: MemoryStatus;
  course: string | null;
  range_text: string;
  range: { days: number; fallback: boolean; limit: number };
  counts: MemoryCounts;
  groups: MemoryGroup[];
  entries: MemoryEntry[];
  suggestions: string[];
  pending_course_entries: MemoryEntry[];
  message: string;
};

export type MemoryCourseCount = {
  course: string | null;
  label: string;
  evidence: number;
};

export type MemoryCoursesView = {
  items: MemoryCourseCount[];
  pending: number;
};

export class MemoryApiError extends Error {
  readonly status: number | undefined;

  constructor(message: string, options: { status?: number } = {}) {
    super(message);
    this.name = "MemoryApiError";
    this.status = options.status;
  }
}

function asRecord(value: unknown, what: string): Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new MemoryApiError(`${what}格式不正确。`);
  }
  return value as Record<string, unknown>;
}

function asString(value: unknown, fallback = ""): string {
  return typeof value === "string" ? value : fallback;
}

function asNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}

function asArray(value: unknown): unknown[] {
  return Array.isArray(value) ? value : [];
}

async function readDetail(response: Response): Promise<string> {
  try {
    const body = await response.json();
    if (body && typeof body === "object" && typeof body.detail === "string") {
      return body.detail;
    }
  } catch {
    // 响应体不是 JSON：使用通用说明。
  }
  return `请求失败（HTTP ${response.status}）。`;
}

async function request(path: string, init?: RequestInit): Promise<unknown> {
  let response: Response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new MemoryApiError("无法连接后端服务，请确认服务已启动。");
  }
  if (!response.ok) {
    throw new MemoryApiError(await readDetail(response), {
      status: response.status,
    });
  }
  try {
    return await response.json();
  } catch {
    throw new MemoryApiError("后端返回的内容不是合法 JSON。");
  }
}

function parseCounts(value: unknown): MemoryCounts {
  const record = asRecord(value ?? {}, "统计");
  return {
    evidence: asNumber(record.evidence),
    practices: asNumber(record.practices),
    questions: asNumber(record.questions),
    attempts: asNumber(record.attempts),
    excluded: asNumber(record.excluded),
    pending_course: asNumber(record.pending_course),
    wrong_independent: asNumber(record.wrong_independent),
    unclear: asNumber(record.unclear),
    excluded_evaluation: asNumber(record.excluded_evaluation),
    earlier_wrong: asNumber(record.earlier_wrong),
    superseded: asNumber(record.superseded),
    unverified: asNumber(record.unverified),
  };
}

function parseAttempt(value: unknown): MemoryAttempt {
  const record = asRecord(value, "作答");
  return {
    attempt_id: asString(record.attempt_id),
    attempt_no: asNumber(record.attempt_no),
    kind: asString(record.kind),
    verdict: asString(record.verdict, "insufficient") as MemoryVerdict,
    label: asString(record.label),
    is_correct:
      typeof record.is_correct === "boolean" ? record.is_correct : null,
    answered_after_revealed: record.answered_after_revealed === true,
    recorded_at: asString(record.recorded_at),
    excluded: record.excluded === true,
  };
}

function parseEntry(value: unknown): MemoryEntry {
  const record = asRecord(value, "学习记忆条目");
  const source = asRecord(record.source ?? {}, "来源");
  return {
    entry_id: asString(record.entry_id),
    kind: asString(record.kind, "exercise") as MemoryEntry["kind"],
    course: typeof record.course === "string" ? record.course : null,
    course_source: asString(
      record.course_source,
      "unknown",
    ) as MemoryEntry["course_source"],
    knowledge_points: asArray(record.knowledge_points).map((item) =>
      asString(item),
    ),
    knowledge_points_normalized: asArray(
      record.knowledge_points_normalized,
    ).map((item) => item === true),
    statement_excerpt:
      typeof record.statement_excerpt === "string"
        ? record.statement_excerpt
        : null,
    verdict: asString(record.verdict, "no_attempt") as MemoryVerdict,
    first_verdict:
      typeof record.first_verdict === "string"
        ? (record.first_verdict as MemoryVerdict)
        : null,
    recorded_at: asString(record.recorded_at),
    first_at: typeof record.first_at === "string" ? record.first_at : null,
    wrong_at: typeof record.wrong_at === "string" ? record.wrong_at : null,
    validity: asString(
      record.validity,
      "ok",
    ) as MemoryEntry["validity"],
    validity_note:
      typeof record.validity_note === "string" ? record.validity_note : null,
    note: typeof record.note === "string" ? record.note : null,
    source: {
      conversation_id: asString(source.conversation_id),
      exercise_id: typeof source.exercise_id === "string" ? source.exercise_id : null,
      problem_id: typeof source.problem_id === "string" ? source.problem_id : null,
      question_id:
        typeof source.question_id === "string" ? source.question_id : null,
      paper_id: typeof source.paper_id === "string" ? source.paper_id : null,
      paper_title:
        typeof source.paper_title === "string" ? source.paper_title : null,
      number_label:
        typeof source.number_label === "string" ? source.number_label : null,
    },
    attempts: asArray(record.attempts).map(parseAttempt),
  };
}

function parseGroup(value: unknown): MemoryGroup {
  const record = asRecord(value, "知识点汇总");
  return {
    label: asString(record.label),
    slug: typeof record.slug === "string" ? record.slug : null,
    normalized: record.normalized === true,
    wrong_count: asNumber(record.wrong_count),
    correct_count: asNumber(record.correct_count),
    after_reveal_count: asNumber(record.after_reveal_count),
    insufficient_count: asNumber(record.insufficient_count),
    unclear_count: asNumber(record.unclear_count),
    no_attempt_count: asNumber(record.no_attempt_count),
    self_report_count: asNumber(record.self_report_count),
    excluded_evaluation_count: asNumber(record.excluded_evaluation_count),
    earlier_wrong_count: asNumber(record.earlier_wrong_count),
    entry_ids: asArray(record.entry_ids).map((item) => asString(item)),
    latest_at: typeof record.latest_at === "string" ? record.latest_at : null,
    latest_wrong_at:
      typeof record.latest_wrong_at === "string" ? record.latest_wrong_at : null,
    suggestion: typeof record.suggestion === "string" ? record.suggestion : null,
  };
}

function parseView(payload: unknown): LearningMemoryView {
  const record = asRecord(payload, "学习记忆");
  const range = asRecord(record.range ?? {}, "时间范围");
  return {
    status: asString(record.status, "ok") as MemoryStatus,
    course: typeof record.course === "string" ? record.course : null,
    range_text: asString(record.range_text),
    range: {
      days: asNumber(range.days) || 30,
      fallback: range.fallback === true,
      limit: asNumber(range.limit) || 20,
    },
    counts: parseCounts(record.counts),
    groups: asArray(record.groups).map(parseGroup),
    entries: asArray(record.entries).map(parseEntry),
    suggestions: asArray(record.suggestions).map((item) => asString(item)),
    pending_course_entries: asArray(record.pending_course_entries).map(parseEntry),
    message: asString(record.message),
  };
}

export async function fetchLearningMemory(
  params: { course?: string | null; days?: number; limit?: number },
  signal?: AbortSignal,
): Promise<LearningMemoryView> {
  const query = new URLSearchParams();
  if (params.course) {
    query.set("course", params.course);
  }
  if (params.days) {
    query.set("days", String(params.days));
  }
  if (params.limit) {
    query.set("limit", String(params.limit));
  }
  const suffix = query.toString() ? `?${query.toString()}` : "";
  const payload = await request(`${PATH}${suffix}`, { signal });
  return parseView(payload);
}

export async function fetchMemoryCourses(
  signal?: AbortSignal,
): Promise<MemoryCoursesView> {
  const payload = await request(`${PATH}/courses`, { signal });
  const record = asRecord(payload, "课程列表");
  return {
    items: asArray(record.items).map((item) => {
      const entry = asRecord(item, "课程");
      return {
        course: typeof entry.course === "string" ? entry.course : null,
        label: asString(entry.label),
        evidence: asNumber(entry.evidence),
      };
    }),
    pending: asNumber(record.pending),
  };
}

function splitEntryId(entryId: string): [string, string] {
  const [kind, sourceId] = entryId.split(":");
  if (!kind || !sourceId) {
    throw new MemoryApiError("记忆条目编号不合法。");
  }
  return [kind, sourceId];
}

export async function correctMemory(
  entryId: string,
  change: { course?: string; knowledge_point?: string; note?: string },
): Promise<MemoryEntry> {
  const [kind, sourceId] = splitEntryId(entryId);
  const payload = await request(`${PATH}/${kind}/${sourceId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(change),
  });
  return parseEntry(payload);
}

export async function deleteMemory(
  entryId: string,
  reason?: string,
): Promise<{ removed: boolean; message: string }> {
  const [kind, sourceId] = splitEntryId(entryId);
  const query = reason ? `?reason=${encodeURIComponent(reason)}` : "";
  const payload = await request(`${PATH}/${kind}/${sourceId}${query}`, {
    method: "DELETE",
  });
  const record = asRecord(payload, "移除结果");
  return {
    removed: record.removed === true,
    message: asString(record.message, "已处理。"),
  };
}