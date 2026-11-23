import type { ReviewKnowledgePoint } from "./reviewWorkbench";
import { apiFetch } from "./apiFetch.ts";
import { readErrorDetail } from "./papersApi.ts";

export type GuideCategory = "concept" | "formula" | "method" | "pitfall";
export type GuideStatus = "generating" | "ready" | "failed";
export type OutputKind = "notes" | "quiz" | "graph";
export type ArtifactInputMode = "full" | "recognize" | "partial";
export type OutputStatus = "none" | "generating" | "ready" | "failed";

export function looksLikeExam(displayName: string): boolean {
  return /试卷|考卷|真题|考试题|期中.{0,4}卷|期末.{0,4}卷/.test(displayName);
}

export type MaterialSummary = {
  is_exam?: boolean;
  id: string;
  display_name: string;
  file_sha256: string;
  size_bytes: number;
  page_count: number;
  dedicated_conversation_id: string | null;
  created_at: string;
  updated_at: string;
};

export type PageReadability = {
  page_no: number;
  char_count: number;
  scanned_suspect: boolean;
  garbled_ratio: number;
  formula_warning: boolean;
  warnings: string[];
};

export type ReadabilityReport = {
  pages?: MaterialPageStatus[];
  material_id: string;
  start_page: number;
  end_page: number;
  sufficient: boolean;
  reason: string;
  recommendation: string | null;
  total_chars: number;
  affected_pages: PageReadability[];
};

export type VerifiedCitation = {
  page_no: number;
  quote: string;
  verified: true;
};

export type GuideItemView = {
  id: string;
  sort_order: number;
  category: GuideCategory;
  title: string;
  source_excerpt: string;
  ai_explanation: string;
  practice_prompt: string;
  verified_citations: VerifiedCitation[];
};

export type GuideSummary = {
  id: string;
  material_id: string;
  start_page: number;
  end_page: number;
  rule_version: string;
  conversation_id: string | null;
  status: GuideStatus;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type ReviewGuideView = {
  id: string;
  material_id: string;
  file_sha256: string;
  start_page: number;
  end_page: number;
  rule_version: string;
  conversation_id: string | null;
  status: GuideStatus;
  reused: boolean;
  readability: ReadabilityReport | null;
  items: GuideItemView[];
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type MaterialDetailResponse = {
  material: MaterialSummary;
  guides: GuideSummary[];
  artifacts?: ArtifactSummary[];
};

export type MaterialPageView = {
  source?: "pdf_text" | "ocr";
  material_id: string;
  page_no: number;
  char_count: number;
  scanned_suspect: boolean;
  garbled_ratio: number;
  formula_warning: boolean;
  warnings: string[];
  extracted_text: string;
};

export type DeleteImpactView = {
  material_id: string;
  display_name: string;
  guide_count: number;
  artifact_count?: number;
  quiz_attempt_count?: number;
  graph_note_count?: number;
  conversation_id: string | null;
  linked_conversation_count?: number;
  preserves_regular_conversations?: boolean;
  exercise_count: number;
  mistake_count: number;
  attachment_count: number;
};

export type QuizQuestionType =
  | "choice"
  | "multi"
  | "tf"
  | "fill"
  | "calc"
  | "code"
  | "short";

export type QuizGradingStatus =
  | "auto_graded"
  | "pending_review"
  | "evaluated"
  | "unanswered";

export type QuizAttemptView = {
  question_id: string;
  user_answer: unknown;
  is_correct: boolean | null;
  score: number | null;
  max_points: number;
  grading_status: QuizGradingStatus;
  feedback: string | null;
  revealed_solution_at: string | null;
  answered_after_revealed: boolean;
  first_is_correct?: boolean | null;
  first_score?: number | null;
  first_grading_status?: QuizGradingStatus | null;
  updated_at: string;
};

export type QuizQuestionPublicView = {
  id: string;
  sort_order: number;
  kc_id: string;
  q_type: QuizQuestionType;
  stem: string;
  options: string[] | null;
  pitfall: string;
  cognitive: string;
  difficulty: string;
  points: number;
  source_pages: number[];
  attempt: QuizAttemptView | null;
  revealed_answer: unknown;
  revealed_explanation: string | null;
};

export type GraphTreeNode = {
  id: string;
  label: string;
  summary: string;
  kc_id?: string;
  deps?: string[];
  source_refs?: string[];
  is_hub?: boolean;
  children?: GraphTreeNode[];
};

export type GraphTreeData = {
  title: string;
  nodes: GraphTreeNode[];
};

export type MaterialPageStatus = {
  page_no: number;
  status: "unchecked" | "usable" | "pending_ocr" | "ocr_failed" | "excluded";
  source?: "pdf_text" | "ocr" | null;
  version?: string | null;
  needs_review: boolean;
  reason?: string | null;
};

export type ArtifactOutputCoverage = {
  cited_pages: number[];
  missing_knowledge_points: string[];
  uncited_pages: number[];
  partial: boolean;
};

export type ArtifactCoverage = {
  input_pages: MaterialPageStatus[];
  included_pages: number[];
  missing_pages: number[];
  review_pages: number[];
  excluded_pages: number[];
  partial: boolean;
};

export type ArtifactSummary = {
  coverage?: ArtifactCoverage | null;
  id: string;
  material_id: string;
  file_sha256: string;
  start_page: number;
  end_page: number;
  rule_version: string;
  course_hint: string;
  title: string;
  is_sample: boolean;
  status: GuideStatus;
  current_step: string;
  requested_outputs?: OutputKind[];
  notes_status?: OutputStatus;
  notes_error_code?: string | null;
  notes_error_message?: string | null;
  quiz_status?: OutputStatus;
  quiz_error_code?: string | null;
  quiz_error_message?: string | null;
  graph_status?: OutputStatus;
  graph_error_code?: string | null;
  graph_error_message?: string | null;
  conversation_id: string | null;
  question_count: number;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type ArtifactDetailView = {
  coverage?: ArtifactCoverage | null;
  output_coverage?: Partial<Record<OutputKind, ArtifactOutputCoverage>>;
  id: string;
  material_id: string;
  file_sha256: string;
  start_page: number;
  end_page: number;
  rule_version: string;
  course_hint: string;
  title: string;
  is_sample: boolean;
  status: GuideStatus;
  current_step: string;
  requested_outputs?: OutputKind[];
  notes_status?: OutputStatus;
  notes_error_code?: string | null;
  notes_error_message?: string | null;
  quiz_status?: OutputStatus;
  quiz_error_code?: string | null;
  quiz_error_message?: string | null;
  graph_status?: OutputStatus;
  graph_error_code?: string | null;
  graph_error_message?: string | null;
  reused: boolean;
  conversation_id: string | null;
  skeleton: Record<string, unknown> | null;
  notes_html: string;
  graph_tree: GraphTreeData | null;
  coverage_warnings: string[];
  questions: QuizQuestionPublicView[];
  graph_notes: Record<string, string>;
  kc_index?: Record<string, ReviewKnowledgePoint>;
  kc_notes?: Record<string, string>;
  error_code: string | null;
  error_message: string | null;
  created_at: string;
  updated_at: string;
};

export type ReadabilityOverview = {
  pages?: MaterialPageStatus[];
  material_id: string;
  page_count: number;
  readable_pages: number;
  total_chars: number;
  scanned_pages: number[];
  ocr_pages?: number[];
  formula_warning_pages: number[];
  checked_pages?: number[];
  sufficient: boolean;
  summary: string;
  available_outputs: OutputKind[];
};

export type ConversationMaterialItem = {
  material: MaterialSummary;
  readability: ReadabilityOverview | null;
  artifacts: ArtifactSummary[];
  attached_at: string;
};

export type ConversationMaterialUploadResponse = {
  conversation_id: string;
  item: ConversationMaterialItem;
  assistant_notice: string;
  turn_request_id: string | null;
};

export type ConversationMaterialGenerateResponse = {
  artifact: ArtifactDetailView;
  turn_request_id: string | null;
};

export type MaterialLibraryItem = {
  material: MaterialSummary;
  readability: ReadabilityOverview | null;
  guides: GuideSummary[];
  artifacts: ArtifactSummary[];
  conversation_ids: string[];
};

export type ConversationDeleteImpactView = {
  conversation_id: string;
  title: string;
  linked_material_count: number;
  artifact_count: number;
  materials_still_accessible: boolean;
  summary: string;
};

export type GraphNoteView = {
  artifact_id: string;
  node_id: string;
  note_text: string;
  updated_at: string | null;
};

export type StudyFromItemResponse = {
  conversation_id: string;
  mode: "explain" | "exercise";
  guide_id: string;
  item_id: string;
  prompt: string;
};

export class MaterialsApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.name = "MaterialsApiError";
    this.status = status;
  }
}

async function expectJson<T>(response: Response, fallback: string): Promise<T> {
  if (!response.ok) {
    if (response.status === 409) {
      const body = await response.clone().json().catch(() => null);
      if (body?.detail?.code === "MATERIAL_COVERAGE_REQUIRED" && typeof body.detail.message === "string") {
        throw new MaterialsApiError(body.detail.message, response.status);
      }
    }
    const detail = (await readErrorDetail(response)) || fallback;
    throw new MaterialsApiError(detail, response.status);
  }
  return (await response.json()) as T;
}

export function materialPreviewUrl(materialId: string, pageNo?: number): string {
  const base = `/api/materials/pdfs/${encodeURIComponent(materialId)}/file`;
  return pageNo && pageNo >= 1 ? `${base}#page=${pageNo}` : base;
}

export async function listMaterials(): Promise<MaterialSummary[]> {
  const res = await apiFetch("/api/materials/pdfs");
  const data = await expectJson<{ items: MaterialSummary[] }>(res, "无法读取个人资料列表。");
  return data.items;
}

export async function uploadMaterialPdf(file: File): Promise<MaterialSummary> {
  const form = new FormData();
  form.append("file", file);
  const res = await apiFetch("/api/materials/pdfs", { method: "POST", body: form });
  return expectJson<MaterialSummary>(res, "PDF 上传失败。");
}

export async function getMaterialDetail(materialId: string): Promise<MaterialDetailResponse> {
  const res = await apiFetch(`/api/materials/pdfs/${encodeURIComponent(materialId)}`);
  return expectJson<MaterialDetailResponse>(res, "无法读取资料详情。");
}

export const MAX_OCR_PAGES_PER_REQUEST = 8;

export type MaterialOcrResult = {
  recognized_pages: number[];
  excluded_pages?: number[];
  reused_pages: number[];
  skipped_pages: number[];
  failures: Array<{ page_no: number; message: string }>;
  readability: ReadabilityOverview | null;
};

export async function recognizeMaterialPages(
  materialId: string,
  pages: number[],
  conversationId?: string,
): Promise<MaterialOcrResult> {
  const res = await apiFetch(
    `/api/materials/pdfs/${encodeURIComponent(materialId)}/ocr`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        ...(conversationId ? { "X-Conversation-Id": conversationId } : {}),
      },
      body: JSON.stringify({ pages }),
    },
  );
  return expectJson<MaterialOcrResult>(res, "扫描页识别失败。");
}

export async function checkMaterialReadability(
  materialId: string,
  startPage: number,
  endPage: number,
): Promise<ReadabilityReport> {
  const res = await apiFetch(
    `/api/materials/pdfs/${encodeURIComponent(materialId)}/readability`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_page: startPage, end_page: endPage }),
    },
  );
  return expectJson<ReadabilityReport>(res, "可读性检查失败。");
}

export async function generateOrGetGuide(
  materialId: string,
  startPage: number,
  endPage: number,
): Promise<ReviewGuideView> {
  const res = await apiFetch(
    `/api/materials/pdfs/${encodeURIComponent(materialId)}/guides`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ start_page: startPage, end_page: endPage }),
    },
  );
  return expectJson<ReviewGuideView>(res, "复习清单生成失败。");
}

export async function getGuide(guideId: string): Promise<ReviewGuideView> {
  const res = await apiFetch(`/api/materials/guides/${encodeURIComponent(guideId)}`);
  return expectJson<ReviewGuideView>(res, "无法读取复习清单。");
}

export async function getMaterialPage(
  materialId: string,
  pageNo: number,
): Promise<MaterialPageView> {
  const res = await apiFetch(
    `/api/materials/pdfs/${encodeURIComponent(materialId)}/pages/${pageNo}`,
  );
  return expectJson<MaterialPageView>(res, "无法读取该页原文。");
}

export async function studyFromGuideItem(
  guideId: string,
  itemId: string,
  mode: "explain" | "exercise",
): Promise<StudyFromItemResponse> {
  const res = await apiFetch(
    `/api/materials/guides/${encodeURIComponent(guideId)}/items/${encodeURIComponent(itemId)}/study`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ mode }),
    },
  );
  return expectJson<StudyFromItemResponse>(res, "无法跳转到学习会话。");
}

export async function getDeleteImpact(materialId: string): Promise<DeleteImpactView> {
  const res = await apiFetch(
    `/api/materials/pdfs/${encodeURIComponent(materialId)}/delete-impact`,
  );
  return expectJson<DeleteImpactView>(res, "无法获取删除影响范围。");
}

export async function deleteMaterial(materialId: string): Promise<void> {
  const res = await apiFetch(`/api/materials/pdfs/${encodeURIComponent(materialId)}`, {
    method: "DELETE",
  });
  await expectJson<{ deleted: boolean }>(res, "删除资料失败。");
}

export function materialPageImageUrl(materialId: string, pageNo: number): string {
  return `/api/materials/pdfs/${encodeURIComponent(materialId)}/pages/${pageNo}/image`;
}

export async function importSampleArtifact(
  materialId: string,
  sampleName: "exampass-physics" = "exampass-physics",
): Promise<ArtifactDetailView> {
  const res = await apiFetch(
    `/api/materials/pdfs/${encodeURIComponent(materialId)}/artifacts/import-sample`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ sample_name: sampleName }),
    },
  );
  return expectJson<ArtifactDetailView>(res, "导入样例复习产物失败。");
}

export async function generateOrGetArtifact(
  materialId: string,
  startPage: number,
  endPage: number,
  courseHint?: string,
  outputs?: OutputKind[],
  conversationId?: string,
): Promise<ArtifactDetailView> {
  const res = await apiFetch(
    `/api/materials/pdfs/${encodeURIComponent(materialId)}/artifacts`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start_page: startPage,
        end_page: endPage,
        ...(courseHint?.trim() ? { course_hint: courseHint.trim() } : {}),
        ...(outputs?.length ? { outputs } : {}),
        ...(conversationId ? { conversation_id: conversationId } : {}),
      }),
    },
  );
  return expectJson<ArtifactDetailView>(res, "生成完整复习资料失败。");
}

export async function getArtifact(artifactId: string): Promise<ArtifactDetailView> {
  const res = await apiFetch(`/api/materials/artifacts/${encodeURIComponent(artifactId)}`);
  return expectJson<ArtifactDetailView>(res, "无法读取复习产物。");
}

export async function submitQuizAttempt(
  artifactId: string,
  questionId: string,
  userAnswer: unknown,
): Promise<ArtifactDetailView> {
  const res = await apiFetch(
    `/api/materials/artifacts/${encodeURIComponent(artifactId)}/questions/${encodeURIComponent(questionId)}/attempt`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_answer: userAnswer }),
    },
  );
  return expectJson<ArtifactDetailView>(res, "提交作答失败。");
}

export async function revealQuizSolution(
  artifactId: string,
  questionId: string,
): Promise<ArtifactDetailView> {
  const res = await apiFetch(
    `/api/materials/artifacts/${encodeURIComponent(artifactId)}/questions/${encodeURIComponent(questionId)}/reveal`,
    { method: "POST" },
  );
  return expectJson<ArtifactDetailView>(res, "无法查看参考解答。");
}

export async function updateGraphNote(
  artifactId: string,
  nodeId: string,
  noteText: string,
): Promise<GraphNoteView> {
  const res = await apiFetch(
    `/api/materials/artifacts/${encodeURIComponent(artifactId)}/graph-notes/${encodeURIComponent(nodeId)}`,
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note_text: noteText }),
    },
  );
  return expectJson<GraphNoteView>(res, "保存图谱笔记失败。");
}

export async function updateKnowledgePointNote(
  artifactId: string,
  kcId: string,
  noteText: string,
): Promise<GraphNoteView> {
  const res = await apiFetch(
    "/api/materials/artifacts/" + encodeURIComponent(artifactId) + "/kc-notes/" + encodeURIComponent(kcId),
    {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note_text: noteText }),
    },
  );
  return expectJson<GraphNoteView>(res, "保存知识点批注失败。");
}

export type StudyFromArtifactQuestionResponse = {
  conversation_id: string;
  artifact_id: string;
  question_id: string;
  mode: "explain" | "evaluate";
  prompt: string;
};

export async function studyFromArtifactQuestion(
  artifactId: string,
  questionId: string,
  mode: "explain" | "evaluate",
  conversationId?: string,
): Promise<StudyFromArtifactQuestionResponse> {
  const res = await apiFetch(
    `/api/materials/artifacts/${encodeURIComponent(artifactId)}/questions/${encodeURIComponent(questionId)}/study`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        ...(conversationId ? { conversation_id: conversationId } : {}),
      }),
    },
  );
  return expectJson<StudyFromArtifactQuestionResponse>(res, "无法跳转到学习会话。");
}

export async function evaluateQuizQuestion(
  artifactId: string,
  questionId: string,
): Promise<ArtifactDetailView> {
  const res = await apiFetch(
    `/api/materials/artifacts/${encodeURIComponent(artifactId)}/questions/${encodeURIComponent(questionId)}/evaluate`,
    { method: "POST" },
  );
  return expectJson<ArtifactDetailView>(res, "AI 评价作答失败。");
}

export type StudyFromArtifactKcResponse = {
  conversation_id: string;
  artifact_id: string;
  kc_id: string;
  mode: "explain" | "exercise";
  prompt: string;
};

export async function studyFromArtifactKc(
  artifactId: string,
  kcId: string,
  mode: "explain" | "exercise" = "explain",
  conversationId?: string,
): Promise<StudyFromArtifactKcResponse> {
  const res = await apiFetch(
    `/api/materials/artifacts/${encodeURIComponent(artifactId)}/kcs/${encodeURIComponent(kcId)}/study`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        mode,
        ...(conversationId ? { conversation_id: conversationId } : {}),
      }),
    },
  );
  return expectJson<StudyFromArtifactKcResponse>(res, "无法跳转到学习会话。");
}

export async function fetchMaterialLibrary(): Promise<MaterialLibraryItem[]> {
  const res = await apiFetch("/api/materials/library");
  const data = await expectJson<{ items: MaterialLibraryItem[] }>(
    res,
    "无法读取资料与成果列表。",
  );
  return data.items;
}

export async function listConversationMaterials(
  conversationId: string,
): Promise<ConversationMaterialItem[]> {
  const res = await apiFetch(
    `/api/conversations/${encodeURIComponent(conversationId)}/materials`,
  );
  const data = await expectJson<{ items: ConversationMaterialItem[] }>(
    res,
    "无法读取当前会话的资料列表。",
  );
  return data.items;
}

export async function uploadConversationMaterial(
  conversationId: string,
  file: File,
): Promise<ConversationMaterialUploadResponse> {
  const form = new FormData();
  form.append("file", file);
  const res = await apiFetch(
    `/api/conversations/${encodeURIComponent(conversationId)}/materials/upload`,
    { method: "POST", body: form },
  );
  return expectJson<ConversationMaterialUploadResponse>(res, "上传学习资料失败。");
}

export async function attachConversationMaterial(
  conversationId: string,
  materialId: string,
): Promise<ConversationMaterialItem> {
  const res = await apiFetch(
    `/api/conversations/${encodeURIComponent(conversationId)}/materials/${encodeURIComponent(materialId)}/attach`,
    { method: "POST" },
  );
  return expectJson<ConversationMaterialItem>(res, "关联个人资料失败。");
}

export async function detachConversationMaterial(
  conversationId: string,
  materialId: string,
): Promise<void> {
  const res = await apiFetch(
    `/api/conversations/${encodeURIComponent(conversationId)}/materials/${encodeURIComponent(materialId)}`,
    { method: "DELETE" },
  );
  await expectJson<{ detached: boolean }>(res, "移除会话资料关联失败。");
}

export async function generateConversationMaterialArtifact(
  conversationId: string,
  materialId: string,
  options: {
    outputs: OutputKind[];
    startPage?: number;
    endPage?: number;
    courseHint?: string;
    coverageMode?: ArtifactInputMode,
  },
): Promise<ConversationMaterialGenerateResponse> {
  const res = await apiFetch(
    `/api/conversations/${encodeURIComponent(conversationId)}/materials/${encodeURIComponent(materialId)}/generate`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        outputs: options.outputs,
        ...(options.coverageMode ? { coverage_mode: options.coverageMode } : {}),
        ...(options.startPage ? { start_page: options.startPage } : {}),
        ...(options.endPage ? { end_page: options.endPage } : {}),
        ...(options.courseHint?.trim() ? { course_hint: options.courseHint.trim() } : {}),
      }),
    },
  );
  return expectJson<ConversationMaterialGenerateResponse>(
    res,
    "生成复习成果失败。",
  );
}

export async function getConversationDeleteImpact(
  conversationId: string,
): Promise<ConversationDeleteImpactView> {
  const res = await apiFetch(
    `/api/conversations/${encodeURIComponent(conversationId)}/delete-impact`,
  );
  return expectJson<ConversationDeleteImpactView>(
    res,
    "无法获取会话删除影响信息。",
  );
}

export type MaterialProgressItem = {
  task_id: string;
  conversation_id: string;
  material_id: string;
  material_name: string;
  kind: "generate" | "ocr";
  start_page: number;
  end_page: number;
  outputs: OutputKind[];
  stage: string;
  stage_label: string;
  ocr_done: number;
  ocr_total: number;
  ocr_failed_pages: number[];
  updated_at: string;
};

export async function getConversationMaterialProgress(
  conversationId: string,
): Promise<{ conversation_id: string; items: MaterialProgressItem[]; chat_turn_request_id: string | null }> {
  const res = await apiFetch(
    `/api/conversations/${encodeURIComponent(conversationId)}/materials/progress`,
  );
  const payload = await expectJson<{ conversation_id?: unknown; items?: unknown; chat_turn_request_id?: unknown }>(
    res,
    "无法读取资料处理进度。",
  );
  const rawItems = Array.isArray(payload.items) ? payload.items : [];
  const items = rawItems.filter(
    (entry): entry is MaterialProgressItem =>
      Boolean(entry) &&
      typeof entry === "object" &&
      typeof (entry as MaterialProgressItem).task_id === "string" &&
      typeof (entry as MaterialProgressItem).material_name === "string" &&
      typeof (entry as MaterialProgressItem).stage_label === "string",
  ).map((entry) => ({
    ...entry,
    outputs: Array.isArray(entry.outputs) ? entry.outputs : [],
    ocr_failed_pages: Array.isArray(entry.ocr_failed_pages) ? entry.ocr_failed_pages : [],
  }));
  return {
    conversation_id: typeof payload.conversation_id === "string" ? payload.conversation_id : conversationId,
    items,
    chat_turn_request_id: typeof payload.chat_turn_request_id === "string" ? payload.chat_turn_request_id : null,
  };
}

export async function deleteConversation(conversationId: string): Promise<void> {
  const res = await apiFetch(
    `/api/conversations/${encodeURIComponent(conversationId)}`,
    { method: "DELETE" },
  );
  await expectJson<{ deleted: boolean }>(res, "删除学习会话失败。");
}
