/** 错题记录列表、详情、修改、纠正与采用新版。 */

import { readErrorDetail } from "./papersApi.ts";
import type {
  AttachmentView,
  AttachmentRole,
  ExerciseView,
  KnowledgePoint,
  RecordSummary,
  Source,
  Step,
} from "./conversationsApi.ts";
import { createSseParser } from "./sseParser.ts";

const PATH = "/api/mistake-records";

export type FollowupView = {
  question: string;
  answer: string;
  created_at: string;
};

export type FeedbackView = {
  text: string | null;
  self_reported_understood: boolean;
  self_reported_at: string | null;
  updated_at: string | null;
};

export type RevisionView = {
  id: number;
  request_id: string;
  reason: "correction" | "retutor" | "adopt_problem";
  statement_text: string;
  source: Source;
  explanation: {
    knowledge_points: KnowledgePoint[];
    steps: Step[];
    final_answer: string;
    error_analysis: string | null;
    uncertainties: string[];
  } | null;
  created_at: string;
};

export type ProblemRef = { id: string; version: number };

export type MistakeRecordDetail = {
  id: string;
  title: string;
  conversation_id: string;
  problem_id: string;
  root_problem_id: string;
  problem_version: number;
  statement_text: string;
  student_answer_text: string | null;
  solution_text: string | null;
  source: Source;
  attachments: AttachmentView[];
  explanation: {
    knowledge_points: KnowledgePoint[];
    steps: Step[];
    final_answer: string;
    error_analysis: string | null;
    uncertainties: string[];
  } | null;
  followups: FollowupView[];
  feedback: FeedbackView;
  self_reported_understood: boolean;
  revisions: RevisionView[];
  conversation_has_newer_version: boolean;
  newer_problem: ProblemRef | null;
  exercises?: ExerciseView[];
  created_at: string;
  updated_at: string;
};

export type MistakeRecordListResponse = {
  items: RecordSummary[];
  total: number;
};

export type RecordUpdatedResult = {
  kind: "record_updated";
  record: MistakeRecordDetail;
  revision_created: boolean;
};

export type RecordNeedsClarificationResult = {
  kind: "record_needs_clarification";
  statement_text: string;
  questions: string[];
  unclear_points: string[];
};

export type RecordCorrectionProgressEvent = {
  type: "progress";
  record_id: string;
  request_id: string;
  stage: "reading_problem" | "checking_clarity" | "explaining" | "saving";
  message: string;
};

export type RecordCorrectionResultEvent = {
  type: "result";
  record_id: string;
  request_id: string;
  result: RecordUpdatedResult | RecordNeedsClarificationResult;
};

export type RecordCorrectionErrorEvent = {
  type: "error";
  record_id: string;
  request_id: string;
  code: string;
  message: string;
};

export type RecordCorrectionEvent =
  | RecordCorrectionProgressEvent
  | RecordCorrectionResultEvent
  | RecordCorrectionErrorEvent;

export type CorrectionOptions = {
  recordId: string;
  requestId: string;
  statementText: string;
  attachments?: { id: string; role: AttachmentRole }[] | null;
  source?: Source;
  studentAnswer?: string;
  solutionText?: string;
  signal?: AbortSignal;
  onProgress?: (event: RecordCorrectionProgressEvent) => void;
};

export class MistakesApiError extends Error {
  readonly status: number | undefined;
  readonly code: string | undefined;

  constructor(message: string, options: { status?: number; code?: string } = {}) {
    super(message);
    this.name = "MistakesApiError";
    this.status = options.status;
    this.code = options.code;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && !Number.isNaN(Date.parse(value));
}

function isSource(value: unknown): value is Source {
  if (!isRecord(value)) {
    return false;
  }
  if (value.type === "none") {
    return true;
  }
  if (value.type === "student_text") {
    return typeof value.text === "string";
  }
  if (value.type === "registered_paper") {
    return typeof value.paper_id === "string";
  }
  return false;
}

function isAttachment(value: unknown): value is AttachmentView {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    (value.role === "statement" || value.role === "solution" || value.role === "supplement") &&
    typeof value.url === "string"
  );
}

function isExplanation(value: unknown): value is NonNullable<MistakeRecordDetail["explanation"]> {
  if (!isRecord(value)) {
    return false;
  }
  return (
    Array.isArray(value.knowledge_points) &&
    Array.isArray(value.steps) &&
    typeof value.final_answer === "string" &&
    (value.error_analysis === null || typeof value.error_analysis === "string") &&
    Array.isArray(value.uncertainties)
  );
}

function isRecordSummary(value: unknown): value is RecordSummary {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    typeof value.title === "string" &&
    typeof value.statement_excerpt === "string" &&
    isSource(value.source) &&
    typeof value.problem_version === "number" &&
    typeof value.self_reported_understood === "boolean" &&
    isTimestamp(value.created_at) &&
    isTimestamp(value.updated_at)
  );
}

function isRevision(value: unknown): value is RevisionView {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "number" &&
    typeof value.request_id === "string" &&
    (value.reason === "correction" ||
      value.reason === "retutor" ||
      value.reason === "adopt_problem") &&
    typeof value.statement_text === "string" &&
    isSource(value.source) &&
    (value.explanation === null || isExplanation(value.explanation)) &&
    isTimestamp(value.created_at)
  );
}

function isMistakeRecordDetail(value: unknown): value is MistakeRecordDetail {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    typeof value.title === "string" &&
    typeof value.conversation_id === "string" &&
    typeof value.problem_id === "string" &&
    typeof value.problem_version === "number" &&
    typeof value.statement_text === "string" &&
    isSource(value.source) &&
    Array.isArray(value.attachments) &&
    value.attachments.every(isAttachment) &&
    (value.explanation === null || isExplanation(value.explanation)) &&
    Array.isArray(value.followups) &&
    isRecord(value.feedback) &&
    typeof value.self_reported_understood === "boolean" &&
    Array.isArray(value.revisions) &&
    value.revisions.every(isRevision) &&
    typeof value.conversation_has_newer_version === "boolean" &&
    isTimestamp(value.created_at) &&
    isTimestamp(value.updated_at)
  );
}

async function readJson(response: Response): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new MistakesApiError("后端返回的错题记录不是有效 JSON。");
  }
}

async function ensureOk(response: Response): Promise<void> {
  if (!response.ok) {
    throw new MistakesApiError(await readErrorDetail(response), {
      status: response.status,
    });
  }
}

export async function listMistakeRecords(
  options: { limit?: number; offset?: number } = {},
  signal?: AbortSignal,
): Promise<MistakeRecordListResponse> {
  const params = new URLSearchParams();
  params.set("limit", String(options.limit ?? 20));
  params.set("offset", String(options.offset ?? 0));
  let response: Response;
  try {
    response = await fetch(`${PATH}?${params}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch {
    throw new MistakesApiError("无法读取错题本，请确认后端正在运行。");
  }
  await ensureOk(response);
  const payload = await readJson(response);
  if (!isRecord(payload) || !Array.isArray(payload.items) || typeof payload.total !== "number") {
    throw new MistakesApiError("后端返回的错题列表格式不符合约定。");
  }
  if (!payload.items.every(isRecordSummary)) {
    throw new MistakesApiError("后端返回的错题列表格式不符合约定。");
  }
  return { items: payload.items, total: payload.total };
}

export async function getMistakeRecord(
  recordId: string,
  signal?: AbortSignal,
): Promise<MistakeRecordDetail> {
  let response: Response;
  try {
    response = await fetch(`${PATH}/${encodeURIComponent(recordId)}`, {
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch {
    throw new MistakesApiError("无法读取错题记录，请确认后端正在运行。");
  }
  await ensureOk(response);
  const payload = await readJson(response);
  if (!isMistakeRecordDetail(payload)) {
    throw new MistakesApiError("后端返回的错题记录格式不符合约定。");
  }
  return payload;
}

export async function updateMistakeRecord(
  recordId: string,
  patch: {
    title?: string;
    source?: Source;
    feedback_text?: string;
    self_reported_understood?: boolean;
  },
): Promise<MistakeRecordDetail> {
  let response: Response;
  try {
    response = await fetch(`${PATH}/${encodeURIComponent(recordId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify(patch),
      cache: "no-store",
    });
  } catch {
    throw new MistakesApiError("无法修改错题记录，请确认后端正在运行。");
  }
  await ensureOk(response);
  const payload = await readJson(response);
  if (!isMistakeRecordDetail(payload)) {
    throw new MistakesApiError("后端返回的错题记录格式不符合约定。");
  }
  return payload;
}

export async function deleteMistakeRecord(recordId: string): Promise<void> {
  let response: Response;
  try {
    response = await fetch(`${PATH}/${encodeURIComponent(recordId)}`, {
      method: "DELETE",
      headers: { Accept: "application/json" },
      cache: "no-store",
    });
  } catch {
    throw new MistakesApiError("无法删除错题记录，请确认后端正在运行。");
  }
  if (response.status === 404) {
    throw new MistakesApiError("这条错题记录已经不存在。", { status: 404 });
  }
  if (!response.ok) {
    throw new MistakesApiError(await readErrorDetail(response), {
      status: response.status,
    });
  }
}

export async function adoptProblem(
  recordId: string,
  problemId: string,
  requestId: string,
): Promise<MistakeRecordDetail> {
  let response: Response;
  try {
    response = await fetch(
      `${PATH}/${encodeURIComponent(recordId)}/adopt-problem`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ request_id: requestId, problem_id: problemId }),
        cache: "no-store",
      },
    );
  } catch {
    throw new MistakesApiError("无法更新错题记录，请确认后端正在运行。");
  }
  await ensureOk(response);
  const payload = await readJson(response);
  if (!isMistakeRecordDetail(payload)) {
    throw new MistakesApiError("后端返回的错题记录格式不符合约定。");
  }
  return payload;
}

function parseCorrectionEvent(
  raw: string,
  recordId: string,
  requestId: string,
): RecordCorrectionEvent {
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new MistakesApiError("后端返回的纠正事件不是有效 JSON。");
  }
  if (!isRecord(payload)) {
    throw new MistakesApiError("后端返回的纠正事件格式不符合约定。");
  }
  if (payload.record_id !== recordId || payload.request_id !== requestId) {
    throw new MistakesApiError("后端返回的纠正事件属于其他记录或请求。");
  }

  if (payload.type === "progress") {
    if (
      !(
        payload.stage === "reading_problem" ||
        payload.stage === "checking_clarity" ||
        payload.stage === "explaining" ||
        payload.stage === "saving"
      ) ||
      typeof payload.message !== "string"
    ) {
      throw new MistakesApiError("后端返回的纠正进度格式不符合约定。");
    }
    return {
      type: "progress",
      record_id: recordId,
      request_id: requestId,
      stage: payload.stage,
      message: payload.message,
    };
  }

  if (payload.type === "error") {
    if (typeof payload.code !== "string" || typeof payload.message !== "string") {
      throw new MistakesApiError("后端返回的纠正错误格式不符合约定。");
    }
    return {
      type: "error",
      record_id: recordId,
      request_id: requestId,
      code: payload.code,
      message: payload.message,
    };
  }

  if (!isRecord(payload.result)) {
    throw new MistakesApiError("后端返回的纠正结果格式不符合约定。");
  }
  if (payload.result.kind === "record_updated") {
    if (!isMistakeRecordDetail(payload.result.record)) {
      throw new MistakesApiError("后端返回的纠正结果格式不符合约定。");
    }
    return {
      type: "result",
      record_id: recordId,
      request_id: requestId,
      result: {
        kind: "record_updated",
        record: payload.result.record,
        revision_created: Boolean(payload.result.revision_created),
      },
    };
  }
  if (payload.result.kind === "record_needs_clarification") {
    if (
      typeof payload.result.statement_text !== "string" ||
      !Array.isArray(payload.result.questions) ||
      !Array.isArray(payload.result.unclear_points)
    ) {
      throw new MistakesApiError("后端返回的纠正结果格式不符合约定。");
    }
    return {
      type: "result",
      record_id: recordId,
      request_id: requestId,
      result: {
        kind: "record_needs_clarification",
        statement_text: payload.result.statement_text,
        questions: payload.result.questions.map(String),
        unclear_points: payload.result.unclear_points.map(String),
      },
    };
  }
  throw new MistakesApiError("后端返回的纠正结果格式不符合约定。");
}

export async function streamRecordCorrection(
  options: CorrectionOptions,
): Promise<RecordUpdatedResult | RecordNeedsClarificationResult> {
  const body: Record<string, unknown> = {
    request_id: options.requestId,
    statement_text: options.statementText.trim(),
  };
  if (options.attachments !== undefined) {
    body.attachments = options.attachments ?? [];
  }
  if (options.source) {
    body.source = options.source;
  }
  if (options.studentAnswer !== undefined) {
    body.student_answer_text = options.studentAnswer;
  }
  if (options.solutionText !== undefined) {
    body.solution_text = options.solutionText;
  }

  let response: Response;
  try {
    response = await fetch(
      `${PATH}/${encodeURIComponent(options.recordId)}/corrections`,
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Accept: "text/event-stream",
        },
        body: JSON.stringify(body),
        cache: "no-store",
        signal: options.signal,
      },
    );
  } catch (error) {
    if (error instanceof DOMException && error.name === "AbortError") {
      throw error;
    }
    throw new MistakesApiError("无法纠正记录，请确认后端正在运行。");
  }

  if (!response.ok) {
    throw new MistakesApiError(await readErrorDetail(response), {
      status: response.status,
    });
  }
  if (!response.body) {
    throw new MistakesApiError("后端没有返回可读取的纠正流。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = createSseParser();

  const consume = (
    payloads: string[],
  ): RecordUpdatedResult | RecordNeedsClarificationResult | null => {
    for (const payload of payloads) {
      const event = parseCorrectionEvent(
        payload,
        options.recordId,
        options.requestId,
      );
      if (event.type === "progress") {
        options.onProgress?.(event);
        continue;
      }
      if (event.type === "error") {
        throw new MistakesApiError(event.message, { code: event.code });
      }
      return event.result;
    }
    return null;
  };

  try {
    while (true) {
      const { done, value } = await reader.read();
      if (done) {
        const tail = decoder.decode();
        if (tail) {
          const tailResult = consume(parser.push(tail));
          if (tailResult) {
            return tailResult;
          }
        }
        let flushed: string[];
        try {
          flushed = parser.flush();
        } catch {
          throw new MistakesApiError("连接提前结束，最后一条事件不完整。", {
            code: "STREAM_INCOMPLETE",
          });
        }
        const flushedResult = consume(flushed);
        if (flushedResult) {
          return flushedResult;
        }
        throw new MistakesApiError("连接在返回纠正结果前结束。", {
          code: "STREAM_INCOMPLETE",
        });
      }
      const result = consume(parser.push(decoder.decode(value, { stream: true })));
      if (result) {
        return result;
      }
    }
  } finally {
    try {
      await reader.cancel();
    } catch {
      // 连接可能已关闭。
    }
  }
}
