/**
 * 本地会话、轮次与辅导结果的客户端。
 *
 * 会话管理与历史使用普通 HTTP；提交一轮使用 SSE。真实历史以服务端 SQLite
 * 为准，这里负责发请求、增量解析事件、校验信封和整理前端需要的类型。
 */

import { readErrorDetail } from "./papersApi.ts";
import {
  isResultEvent,
  type ProgressStage,
  type QueryErrorCode,
  type ResultEvent,
} from "./queryApi.ts";
import { createSseParser } from "./sseParser.ts";

const CONVERSATIONS_PATH = "/api/conversations";
const MAX_QUERY_LENGTH = 2000;

export type TurnStatus = "running" | "completed" | "failed" | "interrupted";
export type BusinessStatus = "succeeded" | "partial" | "failed" | "needs_input";
type BusinessOutcome = { business_status?: BusinessStatus | null; retryable?: boolean; retry_hint?: string | null };

export function turnBusinessStatus(turn: ConversationTurn): BusinessStatus | null {
  if (turn.status === "running") return null;
  if (turn.business_status) return turn.business_status;
  if (turn.composite_results?.some((item) => item.status === "failed")) {
    return turn.composite_results.some((item) => item.status === "success") ? "partial" : "failed";
  }
  if (turn.status !== "completed") return "failed";
  if (turn.evaluation?.needs_supplement || turn.composite_results?.some((item) => item.status === "clarification") ||
      turn.tutoring?.kind === "needs_clarification" || turn.tutoring?.kind === "intent_clarification") return "needs_input";
  if ((turn.kind === "exercise" || turn.kind === "exercise_answer") && !turn.evaluation) return "failed";
  return "succeeded";
}
export type TurnKind =
  | "query"
  | "problem"
  | "supplement"
  | "followup"
  | "correction"
  | "record"
  | "feedback"
  | "dynamic"
  | "exercise"
  // 仅用于读取 F006 修复前的历史轮次；新响应统一为 exercise。
  | "exercise_answer"
  | "reveal_solution";
export type TurnErrorCode = QueryErrorCode | "STORAGE_UNAVAILABLE" | "TUTORING_FAILED" | "BUSINESS_REJECTED";
export type TurnStage =
  | "dispatching"
  | "scheduling"
  | "generating_exercise"
  | "evaluating_exercise"
  | ProgressStage
  | "reading_problem"
  | "checking_clarity"
  | "explaining"
  | "answering"
  | "recording"
  | "updating_record"
  | "saving";

export type SourceType = "none" | "student_text" | "registered_paper";
export type Source = {
  type: SourceType;
  text?: string | null;
  paper_id?: string | null;
  paper_title?: string | null;
};

export type AttachmentRole =
  | "statement"
  | "solution"
  | "supplement"
  | "student_answer"
  | "student_solution";

export type SuggestionItem = {
  label: string;
  action: string;
  target_id?: string | null;
};

export type AttachmentView = {
  id: string;
  role: AttachmentRole;
  mime: string;
  width: number;
  height: number;
  url: string;
};

export type AttachmentMeta = {
  id: string;
  mime: string;
  width: number;
  height: number;
  size_bytes: number;
  created_at: string;
};

export type ProblemView = {
  id: string;
  root_problem_id: string;
  version: number;
  statement_text: string;
  status: "awaiting_supplement" | "explained";
  clarity: "clear" | "unclear";
  source: Source;
  attachments: AttachmentView[];
  student_answer_text: string | null;
  solution_text: string | null;
  self_reported_understood: boolean;
  created_at: string;
  updated_at: string;
};

export type KnowledgePoint = { name: string; explanation: string };
export type Step = { index: number; title: string; content: string };

export type RecordSummary = {
  id: string;
  title: string;
  statement_excerpt: string;
  source: Source;
  problem_version: number;
  self_reported_understood: boolean;
  created_at: string;
  updated_at: string;
};

export type IntentClarification = {
  kind: "intent_clarification";
  message: string;
  options: string[];
};

export type TutoringResult =
  | {
      kind: "explanation";
      problem: ProblemView;
      knowledge_points: KnowledgePoint[];
      steps: Step[];
      final_answer: string;
      error_analysis: string | null;
      uncertainties: string[];
      self_reported_understood: boolean;
      record_id: string | null;
    }
  | {
      kind: "needs_clarification";
      problem: ProblemView;
      questions: string[];
      unclear_points: string[];
    }
  | {
      kind: "followup_answer";
      problem_id: string;
      answer: string;
      related_steps: number[];
    }
  | {
      kind: "feedback_recorded";
      problem_id: string;
      message: string;
    }
  | {
      kind: "record_created";
      record: RecordSummary;
    }
  | {
      kind: "record_exists";
      record: RecordSummary;
      needs_update: boolean;
      message: string;
    }
  | IntentClarification;

export type ConversationSummary = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
};

export type ConversationListResponse = {
  items: ConversationSummary[];
  total: number;
};

export type ExerciseStatus = "pending_answer" | "submitted" | "revealed";

export type ExerciseEvaluation = {
  is_correct: boolean | null;
  final_answer_assessment: string;
  step_by_step_analysis: string;
  guidance: string;
  needs_supplement: boolean;
  independence_assessment?: "independent" | "after_reveal" | "unclear" | null;
  independence_note?: string | null;
};

export type ExerciseAttemptKind = "initial" | "supplement" | "correction";

export type ExerciseAttemptView = {
  id: string;
  attempt_no: number;
  kind: ExerciseAttemptKind;
  student_answer_text?: string | null;
  student_solution_text?: string | null;
  answered_after_revealed: boolean;
  evaluation?: ExerciseEvaluation | null;
  request_id?: string | null;
  attachments: AttachmentView[];
  created_at: string;
  updated_at: string;
};

/** @deprecated 练习附件已统一使用 AttachmentView；保留别名仅为兼容旧导入。 */
export type ExerciseAttachmentView = AttachmentView;

export type ExerciseView = {
  id: string;
  conversation_id: string;
  problem_id: string;
  root_problem_id: string;
  statement_text: string;
  knowledge_point?: string | null;
  reference_solution_text?: string | null;
  student_answer_text?: string | null;
  student_solution_text?: string | null;
  status: ExerciseStatus;
  evaluation?: ExerciseEvaluation | null;
  revealed_solution_at?: string | null;
  answered_after_revealed: boolean;
  attachments: AttachmentView[];
  attempts?: ExerciseAttemptView[];
  created_at: string;
  updated_at: string;
};

export type ActionResultItem = {
  action: string;
  status: "success" | "failed" | "clarification";
  message: string;
  details?: Record<string, unknown>;
};

export type ConversationContextView = {
  confirmed_course?: string | null;
  active_problem_id?: string | null;
  active_exercise_id?: string | null;
  last_suggestions?: SuggestionItem[] | null;
  last_action_summary?: string | null;
  updated_at?: string | null;
};

export type TurnError = {
  code: string;
  message: string;
};

export type RecordRef = {
  id: string;
  problem_version: number;
};

export type ConversationTurn = BusinessOutcome & {
  request_id: string;
  seq: number;
  kind: TurnKind;
  query: string;
  status: TurnStatus;
  result: ResultEvent | null;
  tutoring: TutoringResult | null;
  exercise?: ExerciseView | null;
  evaluation?: ExerciseEvaluation | null;
  composite_results?: ActionResultItem[] | null;
  assistant_message?: string | null;
  suggestions?: SuggestionItem[] | null;
  exercise_id?: string | null;
  error: TurnError | null;
  started_at: string;
  finished_at: string | null;
};

export type ConversationDetail = {
  conversation: ConversationSummary;
  turns: ConversationTurn[];
  active_problem: ProblemView | null;
  active_exercise?: ExerciseView | null;
  context?: ConversationContextView | null;
  record: RecordRef | null;
};

export type TurnProgressEvent = {
  type: "progress";
  conversation_id: string;
  request_id: string;
  stage: TurnStage;
  message: string;
};

export type TurnResultEvent = BusinessOutcome & {
  type: "result";
  conversation_id: string;
  request_id: string;
  kind: TurnKind;
  result: ResultEvent | null;
  tutoring: TutoringResult | null;
  exercise?: ExerciseView | null;
  evaluation?: ExerciseEvaluation | null;
  composite_results?: ActionResultItem[] | null;
  assistant_message?: string | null;
  suggestions?: SuggestionItem[] | null;
  exercise_id?: string | null;
};

export type TurnErrorEvent = {
  type: "error";
  conversation_id: string;
  request_id: string;
  code: TurnErrorCode;
  message: string;
};

export type TurnEvent = TurnProgressEvent | TurnResultEvent | TurnErrorEvent;

export type StreamTurnOptions = {
  conversationId: string;
  requestId: string;
  query: string;
  kind?: TurnKind | "auto";
  attachments?: { id: string; role: AttachmentRole }[];
  studentAnswer?: string;
  solutionText?: string;
  source?: Source;
  problemId?: string;
  exerciseId?: string;
  startNew?: boolean;
  retryOf?: string;
  attemptKind?: "supplement" | "correction";
  signal?: AbortSignal;
  onProgress?: (event: TurnProgressEvent) => void;
};

export class ConversationsApiError extends Error {
  readonly status: number | undefined;
  readonly code: string | undefined;

  constructor(
    message: string,
    options: { status?: number; code?: string } = {},
  ) {
    super(message);
    this.name = "ConversationsApiError";
    this.status = options.status;
    this.code = options.code;
  }
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null;
}

function isBusinessOutcome(value: Record<string, unknown>): boolean {
  return (value.business_status === undefined || value.business_status === null ||
    ["succeeded", "partial", "failed", "needs_input"].includes(value.business_status as string)) &&
    (value.retryable === undefined || typeof value.retryable === "boolean") &&
    (value.retry_hint === undefined || value.retry_hint === null || typeof value.retry_hint === "string");
}

function isTimestamp(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && !Number.isNaN(Date.parse(value));
}

function isConversationSummary(value: unknown): value is ConversationSummary {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    typeof value.title === "string" &&
    isTimestamp(value.created_at) &&
    isTimestamp(value.updated_at)
  );
}

function isTurnStatus(value: unknown): value is TurnStatus {
  return (
    value === "running" ||
    value === "completed" ||
    value === "failed" ||
    value === "interrupted"
  );
}

function isTurnKind(value: unknown): value is TurnKind {
  return (
    value === "query" ||
    value === "problem" ||
    value === "supplement" ||
    value === "followup" ||
    value === "correction" ||
    value === "record" ||
    value === "feedback" ||
    value === "dynamic" ||
    value === "exercise" ||
    value === "exercise_answer" ||
    value === "reveal_solution"
  );
}

function isSuggestionItem(value: unknown): value is SuggestionItem {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.label === "string" &&
    typeof value.action === "string" &&
    (value.target_id === undefined ||
      value.target_id === null ||
      typeof value.target_id === "string")
  );
}

function isExerciseEvaluation(value: unknown): value is ExerciseEvaluation {
  if (!isRecord(value)) {
    return false;
  }
  const independenceOk =
    value.independence_assessment === undefined ||
    value.independence_assessment === null ||
    value.independence_assessment === "independent" ||
    value.independence_assessment === "after_reveal" ||
    value.independence_assessment === "unclear";
  return (
    (value.is_correct === null || typeof value.is_correct === "boolean") &&
    typeof value.final_answer_assessment === "string" &&
    typeof value.step_by_step_analysis === "string" &&
    typeof value.guidance === "string" &&
    typeof value.needs_supplement === "boolean" &&
    independenceOk &&
    (value.independence_note === undefined ||
      value.independence_note === null ||
      typeof value.independence_note === "string")
  );
}

function isExerciseAttemptView(value: unknown): value is ExerciseAttemptView {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    typeof value.attempt_no === "number" &&
    (value.kind === "initial" ||
      value.kind === "supplement" ||
      value.kind === "correction") &&
    (value.student_answer_text === undefined ||
      value.student_answer_text === null ||
      typeof value.student_answer_text === "string") &&
    (value.student_solution_text === undefined ||
      value.student_solution_text === null ||
      typeof value.student_solution_text === "string") &&
    typeof value.answered_after_revealed === "boolean" &&
    (value.evaluation === undefined ||
      value.evaluation === null ||
      isExerciseEvaluation(value.evaluation)) &&
    (value.request_id === undefined ||
      value.request_id === null ||
      typeof value.request_id === "string") &&
    Array.isArray(value.attachments) &&
    value.attachments.every(isAttachmentView) &&
    isTimestamp(value.created_at) &&
    isTimestamp(value.updated_at)
  );
}

function isExerciseView(value: unknown): value is ExerciseView {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    typeof value.conversation_id === "string" &&
    typeof value.problem_id === "string" &&
    typeof value.root_problem_id === "string" &&
    typeof value.statement_text === "string" &&
    (value.knowledge_point === undefined ||
      value.knowledge_point === null ||
      typeof value.knowledge_point === "string") &&
    (value.status === "pending_answer" ||
      value.status === "submitted" ||
      value.status === "revealed") &&
    (value.student_answer_text === undefined ||
      value.student_answer_text === null ||
      typeof value.student_answer_text === "string") &&
    (value.student_solution_text === undefined ||
      value.student_solution_text === null ||
      typeof value.student_solution_text === "string") &&
    (value.reference_solution_text === undefined ||
      value.reference_solution_text === null ||
      typeof value.reference_solution_text === "string") &&
    (value.revealed_solution_at === undefined ||
      value.revealed_solution_at === null ||
      typeof value.revealed_solution_at === "string") &&
    typeof value.answered_after_revealed === "boolean" &&
    (value.evaluation === undefined ||
      value.evaluation === null ||
      isExerciseEvaluation(value.evaluation)) &&
    Array.isArray(value.attachments) &&
    value.attachments.every(isAttachmentView) &&
    (value.attempts === undefined ||
      (Array.isArray(value.attempts) &&
        value.attempts.every(isExerciseAttemptView))) &&
    isTimestamp(value.created_at) &&
    isTimestamp(value.updated_at)
  );
}

function isActionResultItem(value: unknown): value is ActionResultItem {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.action === "string" &&
    (value.status === "success" ||
      value.status === "failed" ||
      value.status === "clarification") &&
    typeof value.message === "string" &&
    (value.details === undefined ||
      value.details === null ||
      isRecord(value.details))
  );
}

function isConversationContext(value: unknown): value is ConversationContextView {
  if (!isRecord(value)) {
    return false;
  }
  return (
    (value.confirmed_course === undefined ||
      value.confirmed_course === null ||
      typeof value.confirmed_course === "string") &&
    (value.active_problem_id === undefined ||
      value.active_problem_id === null ||
      typeof value.active_problem_id === "string") &&
    (value.active_exercise_id === undefined ||
      value.active_exercise_id === null ||
      typeof value.active_exercise_id === "string") &&
    (value.last_suggestions === undefined ||
      value.last_suggestions === null ||
      (Array.isArray(value.last_suggestions) &&
        value.last_suggestions.every(isSuggestionItem))) &&
    (value.last_action_summary === undefined ||
      value.last_action_summary === null ||
      typeof value.last_action_summary === "string")
  );
}

function isTurnError(value: unknown): value is TurnError {
  if (!isRecord(value)) {
    return false;
  }
  return typeof value.code === "string" && typeof value.message === "string";
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

function isAttachmentRole(value: unknown): value is AttachmentRole {
  return (
    value === "statement" ||
    value === "solution" ||
    value === "supplement" ||
    value === "student_answer" ||
    value === "student_solution"
  );
}

function isAttachmentView(value: unknown): value is AttachmentView {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    isAttachmentRole(value.role) &&
    typeof value.mime === "string" &&
    typeof value.width === "number" &&
    typeof value.height === "number" &&
    typeof value.url === "string"
  );
}

export function isProblemView(value: unknown): value is ProblemView {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.id === "string" &&
    typeof value.root_problem_id === "string" &&
    typeof value.version === "number" &&
    typeof value.statement_text === "string" &&
    (value.status === "awaiting_supplement" || value.status === "explained") &&
    (value.clarity === "clear" || value.clarity === "unclear") &&
    isSource(value.source) &&
    Array.isArray(value.attachments) &&
    value.attachments.every(isAttachmentView) &&
    (value.student_answer_text === null || typeof value.student_answer_text === "string") &&
    (value.solution_text === null || typeof value.solution_text === "string") &&
    typeof value.self_reported_understood === "boolean"
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

export function isTutoringResult(value: unknown): value is TutoringResult {
  if (!isRecord(value)) {
    return false;
  }
  switch (value.kind) {
    case "explanation":
      return (
        isProblemView(value.problem) &&
        Array.isArray(value.knowledge_points) &&
        Array.isArray(value.steps) &&
        typeof value.final_answer === "string" &&
        (value.error_analysis === null || typeof value.error_analysis === "string") &&
        Array.isArray(value.uncertainties)
      );
    case "needs_clarification":
      return (
        isProblemView(value.problem) &&
        Array.isArray(value.questions) &&
        value.questions.every((item) => typeof item === "string")
      );
    case "followup_answer":
      return (
        typeof value.problem_id === "string" &&
        typeof value.answer === "string" &&
        Array.isArray(value.related_steps)
      );
    case "feedback_recorded":
      return typeof value.problem_id === "string" && typeof value.message === "string";
    case "record_created":
      return isRecordSummary(value.record);
    case "record_exists":
      return (
        isRecordSummary(value.record) &&
        typeof value.needs_update === "boolean" &&
        typeof value.message === "string"
      );
    case "intent_clarification":
      return (
        typeof value.message === "string" &&
        Array.isArray(value.options) &&
        value.options.every((item) => typeof item === "string")
      );
    default:
      return false;
  }
}

function isConversationTurn(value: unknown): value is ConversationTurn {
  if (!isRecord(value) || !isBusinessOutcome(value)) return false;
  if (!isRecord(value)) {
    return false;
  }
  if (
    typeof value.request_id !== "string" ||
    typeof value.seq !== "number" ||
    !isTurnKind(value.kind) ||
    typeof value.query !== "string" ||
    !isTurnStatus(value.status) ||
    !isTimestamp(value.started_at) ||
    !(value.finished_at === null || isTimestamp(value.finished_at))
  ) {
    return false;
  }

  const result = value.result;
  const tutoring = value.tutoring;
  const error = value.error;
  const exercise = value.exercise;
  const evaluation = value.evaluation;
  const composite = value.composite_results;
  const suggestions = value.suggestions;
  if (!(result === null || isResultEvent(result))) {
    return false;
  }
  if (!(tutoring === null || isTutoringResult(tutoring))) {
    return false;
  }
  if (!(error === null || isTurnError(error))) {
    return false;
  }
  if (!(exercise === undefined || exercise === null || isExerciseView(exercise))) {
    return false;
  }
  if (
    !(evaluation === undefined || evaluation === null || isExerciseEvaluation(evaluation))
  ) {
    return false;
  }
  if (
    !(
      composite === undefined ||
      composite === null ||
      (Array.isArray(composite) && composite.every(isActionResultItem))
    )
  ) {
    return false;
  }
  if (
    !(
      suggestions === undefined ||
      suggestions === null ||
      (Array.isArray(suggestions) && suggestions.every(isSuggestionItem))
    )
  ) {
    return false;
  }
  if (
    !(
      value.assistant_message === undefined ||
      value.assistant_message === null ||
      typeof value.assistant_message === "string"
    )
  ) {
    return false;
  }
  if (
    !(
      value.exercise_id === undefined ||
      value.exercise_id === null ||
      typeof value.exercise_id === "string"
    )
  ) {
    return false;
  }
  if (value.status === "completed") {
    if (value.kind === "query") {
      return (
        result !== null &&
        tutoring === null &&
        error === null &&
        value.finished_at !== null
      );
    }
    if (value.kind === "exercise" || value.kind === "exercise_answer") {
      return error === null && exercise != null && value.finished_at !== null;
    }
    if (value.kind === "reveal_solution") {
      return error === null && exercise != null && value.finished_at !== null;
    }
    if (value.kind === "dynamic") {
      return (
        error === null &&
        value.finished_at !== null &&
        (result !== null ||
          tutoring !== null ||
          exercise != null ||
          Boolean(value.assistant_message) ||
          (Array.isArray(composite) && composite.length > 0))
      );
    }
    return result === null && tutoring !== null && error === null && value.finished_at !== null;
  }
  if (value.status === "failed") {
    if (exercise != null && result === null && tutoring === null) return error !== null && value.finished_at !== null;
    if ((value.business_status === "partial" || value.business_status === "succeeded") && Array.isArray(composite) && composite.some((item) => item.status === "success")) {
      return error !== null && value.finished_at !== null;
    }
    return (
      error !== null &&
      result === null &&
      tutoring === null &&
      exercise == null &&
      evaluation == null &&
      value.finished_at !== null
    );
  }
  if (value.status === "interrupted") {
    if ((value.business_status === "partial" || value.business_status === "succeeded") && Array.isArray(composite) && composite.some((item) => item.status === "success")) {
      return error === null && value.finished_at !== null;
    }
    return result === null && tutoring === null && error === null && value.finished_at !== null;
  }
  return result === null && tutoring === null && error === null && value.finished_at === null;
}

function isRecordRef(value: unknown): value is RecordRef {
  return (
    isRecord(value) &&
    typeof value.id === "string" &&
    typeof value.problem_version === "number"
  );
}

function isConversationDetail(value: unknown): value is ConversationDetail {
  if (!isRecord(value)) {
    return false;
  }
  return (
    isConversationSummary(value.conversation) &&
    Array.isArray(value.turns) &&
    value.turns.every(isConversationTurn) &&
    (value.active_problem === null || isProblemView(value.active_problem)) &&
    (value.active_exercise === undefined ||
      value.active_exercise === null ||
      isExerciseView(value.active_exercise)) &&
    (value.context === undefined ||
      value.context === null ||
      isConversationContext(value.context)) &&
    (value.record === null || isRecordRef(value.record))
  );
}

function isConversationList(value: unknown): value is ConversationListResponse {
  if (!isRecord(value)) {
    return false;
  }
  return (
    Array.isArray(value.items) &&
    value.items.every(isConversationSummary) &&
    typeof value.total === "number" &&
    value.total === value.items.length
  );
}

function isTurnStage(value: unknown): value is TurnStage {
  return (
    value === "dispatching" ||
    value === "scheduling" ||
    value === "generating_exercise" ||
    value === "evaluating_exercise" ||
    value === "understanding" ||
    value === "retrieving" ||
    value === "reading_problem" ||
    value === "checking_clarity" ||
    value === "explaining" ||
    value === "answering" ||
    value === "recording" ||
    value === "updating_record" ||
    value === "saving"
  );
}

function isTurnErrorCode(value: unknown): value is TurnErrorCode {
  return (
    value === "MODEL_UNAVAILABLE" ||
    value === "MODEL_AUTH_FAILED" ||
    value === "MODEL_RATE_LIMITED" ||
    value === "MODEL_TIMEOUT" ||
    value === "MODEL_RESPONSE_INVALID" ||
    value === "CATALOG_UNAVAILABLE" ||
    value === "QUERY_FAILED" ||
    value === "TUTORING_FAILED" ||
    value === "BUSINESS_REJECTED" ||
    value === "STORAGE_UNAVAILABLE"
  );
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

/** 生成一轮的 request_id；同一次逻辑提交只在开始时生成一次。 */
export function createRequestId(): string {
  if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
    return crypto.randomUUID();
  }

  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0"));
  return [
    hex.slice(0, 4).join(""),
    hex.slice(4, 6).join(""),
    hex.slice(6, 8).join(""),
    hex.slice(8, 10).join(""),
    hex.slice(10, 16).join(""),
  ].join("-");
}

async function readJson(response: Response, description: string): Promise<unknown> {
  try {
    return await response.json();
  } catch {
    throw new ConversationsApiError(`后端返回的${description}不是有效 JSON。`);
  }
}

async function ensureOk(response: Response): Promise<void> {
  if (!response.ok) {
    throw new ConversationsApiError(await readErrorDetail(response), {
      status: response.status,
    });
  }
}

export async function createConversation(
  signal?: AbortSignal,
): Promise<ConversationSummary> {
  let response: Response;
  try {
    response = await fetch(CONVERSATIONS_PATH, {
      method: "POST",
      headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: "{}",
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new ConversationsApiError("无法连接到后端服务，请确认后端正在运行。");
  }

  await ensureOk(response);
  const payload = await readJson(response, "会话");
  if (!isConversationSummary(payload)) {
    throw new ConversationsApiError("后端返回的会话格式不符合约定。");
  }
  return payload;
}

export async function listConversations(
  signal?: AbortSignal,
): Promise<ConversationListResponse> {
  let response: Response;
  try {
    response = await fetch(CONVERSATIONS_PATH, {
      method: "GET",
      headers: { Accept: "application/json" },
      cache: "no-store",
      signal,
    });
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new ConversationsApiError("无法连接到后端服务，请确认后端正在运行。");
  }

  await ensureOk(response);
  const payload = await readJson(response, "会话列表");
  if (!isConversationList(payload)) {
    throw new ConversationsApiError("后端返回的会话列表格式不符合约定。");
  }
  return payload;
}

export async function getConversation(
  conversationId: string,
  signal?: AbortSignal,
): Promise<ConversationDetail> {
  let response: Response;
  try {
    response = await fetch(
      `${CONVERSATIONS_PATH}/${encodeURIComponent(conversationId)}`,
      {
        method: "GET",
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal,
      },
    );
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new ConversationsApiError("无法连接到后端服务，请确认后端正在运行。");
  }

  await ensureOk(response);
  const payload = await readJson(response, "会话记录");
  if (!isConversationDetail(payload)) {
    throw new ConversationsApiError("后端返回的会话记录格式不符合约定。");
  }
  return payload;
}

export function parseTurnEvent(
  raw: string,
  conversationId: string,
  requestId: string,
): TurnEvent {
  let payload: unknown;
  try {
    payload = JSON.parse(raw);
  } catch {
    throw new ConversationsApiError("后端返回的查询事件不是有效 JSON。");
  }

  if (!isRecord(payload)) {
    throw new ConversationsApiError("后端返回的查询事件格式不符合约定。");
  }
  if (
    payload.conversation_id !== conversationId ||
    payload.request_id !== requestId
  ) {
    throw new ConversationsApiError("后端返回的事件属于其他会话或请求。");
  }

  if (payload.type === "progress") {
    if (!isTurnStage(payload.stage) || typeof payload.message !== "string") {
      throw new ConversationsApiError("后端返回的进度事件格式不符合约定。");
    }
    return {
      type: "progress",
      conversation_id: conversationId,
      request_id: requestId,
      stage: payload.stage,
      message: payload.message,
    };
  }

  if (payload.type === "error") {
    if (!isTurnErrorCode(payload.code) || typeof payload.message !== "string") {
      throw new ConversationsApiError("后端返回的错误事件格式不符合约定。");
    }
    return {
      type: "error",
      conversation_id: conversationId,
      request_id: requestId,
      code: payload.code,
      message: payload.message,
    };
  }

  if (payload.type !== "result" || !isTurnKind(payload.kind)) {
    throw new ConversationsApiError("后端返回的查询事件格式不符合约定。");
  }
  const kind: TurnKind =
    payload.kind === "exercise_answer" ? "exercise" : payload.kind;
  if (!isBusinessOutcome(payload)) throw new ConversationsApiError("后端返回的业务状态格式不符合约定。");
  const outcome: BusinessOutcome = {
    business_status: payload.business_status as BusinessStatus | null | undefined,
    retryable: payload.retryable as boolean | undefined,
    retry_hint: payload.retry_hint as string | null | undefined,
  };
  const result = payload.result;
  const tutoring = payload.tutoring;
  if (!(result === null || result === undefined || isResultEvent(result))) {
    throw new ConversationsApiError("后端返回的查询结果格式不符合约定。");
  }
  if (!(tutoring === null || tutoring === undefined || isTutoringResult(tutoring))) {
    throw new ConversationsApiError("后端返回的辅导结果格式不符合约定。");
  }
  if (kind === "query") {
    if (!result) {
      throw new ConversationsApiError("查询轮次缺少结果。");
    }
    return {
      type: "result",
      conversation_id: conversationId,
      request_id: requestId,
      kind,
      result,
      tutoring: null,
      ...outcome,
    };
  }
  if (kind === "dynamic" || kind === "exercise" || kind === "reveal_solution") {
    const exercise = payload.exercise;
    const evaluation = payload.evaluation;
    const composite = payload.composite_results;
    const suggestions = payload.suggestions;
    const assistantMessage = payload.assistant_message;
    const exerciseId = payload.exercise_id;
    if (
      !(
        exercise === undefined ||
        exercise === null ||
        isExerciseView(exercise)
      )
    ) {
      throw new ConversationsApiError("后端返回的练习题格式不符合约定。");
    }
    if (
      !(
        evaluation === undefined ||
        evaluation === null ||
        isExerciseEvaluation(evaluation)
      )
    ) {
      throw new ConversationsApiError("后端返回的练习评价格式不符合约定。");
    }
    if (
      !(
        composite === undefined ||
        composite === null ||
        (Array.isArray(composite) && composite.every(isActionResultItem))
      )
    ) {
      throw new ConversationsApiError("后端返回的动作结果格式不符合约定。");
    }
    if (
      !(
        suggestions === undefined ||
        suggestions === null ||
        (Array.isArray(suggestions) && suggestions.every(isSuggestionItem))
      )
    ) {
      throw new ConversationsApiError("后端返回的建议格式不符合约定。");
    }
    if (
      !(
        assistantMessage === undefined ||
        assistantMessage === null ||
        typeof assistantMessage === "string"
      )
    ) {
      throw new ConversationsApiError("后端返回的助手回复格式不符合约定。");
    }
    if (
      !(
        exerciseId === undefined ||
        exerciseId === null ||
        typeof exerciseId === "string"
      )
    ) {
      throw new ConversationsApiError("后端返回的练习标识格式不符合约定。");
    }
    if ((kind === "exercise" || kind === "reveal_solution") && !exercise) {
      throw new ConversationsApiError("练习轮次缺少练习题结果。");
    }
    return {
      type: "result",
      conversation_id: conversationId,
      request_id: requestId,
      kind,
      result: result ?? null,
      tutoring: tutoring ?? null,
      exercise: exercise ?? null,
      evaluation: evaluation ?? null,
      composite_results: composite ?? null,
      assistant_message: assistantMessage ?? null,
      suggestions: suggestions ?? null,
      exercise_id: exerciseId ?? null,
      ...outcome,
    };
  }
  if (!tutoring) {
    throw new ConversationsApiError("辅导轮次缺少结果。");
  }
  return {
    type: "result",
    conversation_id: conversationId,
    request_id: requestId,
    kind,
    result: null,
    tutoring,
    ...outcome,
  };
}

/**
 * 提交一轮输入，返回最终成功事件。
 *
 * 只有收到属于本会话、本 request_id 的合法 result 才算完成；error 或提前断流
 * 都会抛出 ConversationsApiError。重复提交已完成的请求会直接收到保存的结果。
 */
export async function streamTurn(
  options: StreamTurnOptions,
): Promise<TurnResultEvent> {
  const text = options.query.trim();
  if (text.length > MAX_QUERY_LENGTH) {
    throw new ConversationsApiError(`本轮文字最长为 ${MAX_QUERY_LENGTH} 个字符。`);
  }
  if (!text && !(options.attachments ?? []).length && !options.retryOf &&
      !((options.kind === "exercise" || options.kind === "exercise_answer") &&
        (options.studentAnswer?.trim() || options.solutionText?.trim()))) {
    throw new ConversationsApiError("请输入文字或添加图片。");
  }

  const body: Record<string, unknown> = {
    request_id: options.requestId,
    query: text,
  };
  if (options.kind) {
    body.kind = options.kind;
  }
  if (options.attachments?.length) {
    body.attachments = options.attachments;
  }
  if (options.studentAnswer?.trim()) {
    body.student_answer = options.studentAnswer.trim();
  }
  if (options.solutionText?.trim()) {
    body.solution_text = options.solutionText.trim();
  }
  if (options.source && options.source.type !== "none") {
    body.source = options.source;
  }
  if (options.problemId) {
    body.problem_id = options.problemId;
  }
  if (options.exerciseId) {
    body.exercise_id = options.exerciseId;
  }
  if (options.startNew) {
    body.start_new = true;
  }
  if (options.attemptKind) {
    body.attempt_kind = options.attemptKind;
  }
  if (options.retryOf) {
    Object.keys(body).forEach((key) => { if (key !== "request_id") delete body[key]; });
    body.retry_of = options.retryOf;
  }

  let response: Response;
  try {
    response = await fetch(
      `${CONVERSATIONS_PATH}/${encodeURIComponent(options.conversationId)}/turns`,
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
    if (isAbortError(error)) {
      throw error;
    }
    throw new ConversationsApiError("无法连接到后端服务，请确认后端正在运行。");
  }

  await ensureOk(response);
  if (!response.body) {
    throw new ConversationsApiError("后端没有返回可读取的查询流。");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  const parser = createSseParser();

  const consume = (payloads: string[]): TurnResultEvent | null => {
    for (const payload of payloads) {
      const event = parseTurnEvent(
        payload,
        options.conversationId,
        options.requestId,
      );
      if (event.type === "progress") {
        options.onProgress?.(event);
        continue;
      }
      if (event.type === "error") {
        throw new ConversationsApiError(event.message, { code: event.code });
      }
      return event;
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
          throw new ConversationsApiError("连接提前结束，最后一条事件不完整。", {
            code: "STREAM_INCOMPLETE",
          });
        }

        const flushedResult = consume(flushed);
        if (flushedResult) {
          return flushedResult;
        }

        throw new ConversationsApiError("连接在返回最终结果前结束，本轮未完成。", {
          code: "STREAM_INCOMPLETE",
        });
      }

      const result = consume(parser.push(decoder.decode(value, { stream: true })));
      if (result) {
        return result;
      }
    }
  } catch (error) {
    if (error instanceof ConversationsApiError || isAbortError(error)) {
      throw error;
    }
    throw new ConversationsApiError("读取结果时连接中断，请重试。");
  } finally {
    try {
      await reader.cancel();
    } catch {
      // 连接可能已经关闭，无需再处理。
    }
  }
}

export async function revealExerciseSolution(
  conversationId: string,
  exerciseId: string,
  signal?: AbortSignal,
): Promise<ExerciseView> {
  let response: Response;
  try {
    response = await fetch(
      `${CONVERSATIONS_PATH}/${encodeURIComponent(conversationId)}/exercises/${encodeURIComponent(exerciseId)}/reveal`,
      {
        method: "POST",
        headers: { Accept: "application/json" },
        cache: "no-store",
        signal,
      },
    );
  } catch (error) {
    if (isAbortError(error)) {
      throw error;
    }
    throw new ConversationsApiError("无法连接到后端服务，请确认后端正在运行。");
  }

  await ensureOk(response);
  const payload = await readJson(response, "练习解答");
  if (!isExerciseView(payload)) {
    throw new ConversationsApiError("后端返回的练习解答格式不符合约定。");
  }
  return payload;
}
