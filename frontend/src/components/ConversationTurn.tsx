import type { ReactNode } from "react";
import type { PaperItem } from "../lib/papersApi";
import { looksLikeExam, materialPreviewUrl, type ArtifactInputMode, type ArtifactSummary, type OutputKind } from "../lib/materialsApi";
import type {
  AttachmentInput,
  ConversationTurn as StoredTurn,
  ExerciseAttemptView,
  ExerciseView,
  ProblemView,
  QuestionCard,
  RecordRef,
  SuggestionItem,
} from "../lib/conversationsApi";
import AssistantDraft from "./AssistantDraft";
import AssistantMarkdown, { extractSafeLead } from "./AssistantMarkdown";
import ExerciseCard, { type ExerciseDraftImage } from "./ExerciseCard";
import ExerciseAttemptReceipt from "./ExerciseAttemptReceipt";
import MathText from "./MathText";
import ProblemCard from "./ProblemCard";
import QuestionCardView, { QuestionResultList, type QuestionAction } from "./QuestionCard";
import QueryResult from "./QueryResult";
import { turnBusinessStatus } from "../lib/conversationsApi";
import {
  actionStatusLabel,
  classifySuggestion,
  formatDateTime,
  explanationBody,
  isCoveredText,
  stripDuplicatePaperList,
  type DownloadStates,
} from "../lib/display";

export type ExerciseRole = "full" | "attempt" | "reference";

type ConversationTurnProps = {
  turn: StoredTurn;
  liveProgress?: string;
  unsavedDraft?: boolean;
  downloads?: DownloadStates;
  onDownload: (paper: PaperItem) => void;
  onRetry: (turn: StoredTurn) => void;
  /** 只重做正文：业务操作已经成功，不能重跑。 */
  onRegenerateReply?: (turn: StoredTurn) => void;
  regeneratingReply?: boolean;
  replyQuotaBlocked?: boolean;
  exerciseDrafts?: Record<string, { answer: string; solution: string }>;
  exerciseDraftImages?: Record<string, ExerciseDraftImage[]>;
  exerciseImageNotices?: Record<string, string | null>;
  imagesBlocked?: boolean;
  solutionImageNames?: string[];
  recordsByRoot?: Record<string, RecordRef>;
  savingMistakeRootId?: string | null;
  onExerciseDraftChange?: (exerciseId: string, draft: { answer: string; solution: string }) => void;
  onAddExerciseImages?: (exerciseId: string, files: File[]) => void;
  onRemoveExerciseImage?: (exerciseId: string, localId: string) => void;
  onRetryExerciseImage?: (exerciseId: string, localId: string) => void;
  onSaveExerciseMistake?: (exercise: ExerciseView) => void;
  onFeedback: (problem: ProblemView) => void;
  onRecord: (problem: ProblemView) => void;
  onCorrect: (problem: ProblemView) => void;
  onSupplement: (problem: ProblemView) => void;
  onNew: (text: string) => void;
  onIntentChoice: (turn: StoredTurn, option: string) => void;
  onOpenRecord: (recordId: string) => void;
  onSubmitExerciseAnswer?: (
    exerciseId: string,
    answerText: string,
    solutionText?: string,
    attemptKind?: "supplement" | "correction",
    attachments?: AttachmentInput[],
  ) => void;
  onRevealExerciseSolution?: (exerciseId: string) => void;
  onSuggestionClick?: (turn: StoredTurn, suggestion: SuggestionItem) => void;
  onQuestionAction?: (
    action: QuestionAction,
    question: import("../lib/conversationsApi").QuestionCard,
    answer?: string,
  ) => void;
  questionOwners?: ReadonlyMap<string, string>;
  questionSnapshots?: ReadonlyMap<string, QuestionCard>;
  onJumpToQuestion?: (key: string) => void;
  onJumpToExercise?: (exerciseId: string) => void;
  onCorrectExercise?: (exerciseId: string) => void;
  onGenerateMaterialOutput?: (materialId: string, output: OutputKind) => void;
  onConfirmMaterialCoverage?: (
    materialId: string, outputs: OutputKind[], start: number, end: number,
    mode: ArtifactInputMode, courseHint?: string,
  ) => void;
  onRetryMaterialOutput?: (artifactId: string, output: OutputKind) => void;
  onOpenMaterialArtifact?: (artifactId: string, materialTitle: string, pageCount: number, tab?: OutputKind) => void;
  materialArtifactsById?: Map<string, ArtifactSummary>;
  onOpenWorkbenchSource?: (source: NonNullable<StoredTurn["workbench_source"]>) => void;
  materialResultIds?: string[];
  materialResultCards?: ReactNode;
  /** 这一轮是否为当前最新一轮；旧轮次的建议已经失效，不再展示。 */
  isLatestTurn?: boolean;
  hasActiveProblem?: boolean;
  exerciseRole?: ExerciseRole;
  exerciseAttempt?: ExerciseAttemptView | null;
  hiddenAttemptIds?: string[];
  receiptSeqs?: number[];
  focusToken?: number;
  correctionToken?: number;
  busy: boolean;
};

const STATUS_LABELS: Record<StoredTurn["status"], string> = {
  running: "进行中",
  completed: "已完成",
  failed: "失败",
  interrupted: "已中断",
};

function statusClass(status: StoredTurn["status"]): string {
  if (status === "completed") {
    return "border-emerald-200 bg-emerald-50 text-emerald-700";
  }
  if (status === "failed") {
    return "border-rose-200 bg-rose-50 text-rose-700";
  }
  if (status === "interrupted") {
    return "border-amber-200 bg-amber-50 text-amber-700";
  }
  return "border-slate-200 bg-slate-50 text-slate-600";
}

const OPTION_LABELS: Record<string, string> = {
  query: "查试卷",
  tutoring: "辅导这道题",
  new_problem: "开始新题",
  supplement: "补充当前题",
  correction: "纠正题干",
};

function bubbleFallback(turn: StoredTurn): string {
  if (turn.kind === "exercise") {
    return "（提交练习作答）";
  }
  if (turn.kind === "reveal_solution") {
    return "（查看参考解答）";
  }
  if (turn.kind === "select_question") {
    return "（选择真题）";
  }
  if (turn.kind === "explain_question") {
    return "（讲解真题）";
  }
  if (turn.kind === "view_question_answer") {
    return "（查看真题答案）";
  }
  return "（图片题目）";
}

const QUESTION_CARD_ACTIONS = new Set([
  "讲解这道题",
  "查看参考答案",
  "生成一道变式",
  "保存到错题本",
]);

/**
 * 建议只在“最新一轮 + 当前状态仍有效”时出现：
 * 已评价练习不再提示提交答案，已展开解答不再提示看答案，没有当前题不再提示练习，
 * 且剔除与当前轮已直出的讲解/真题卡按钮重复的建议。
 */
function usableSuggestions(
  turn: StoredTurn,
  isLatestTurn: boolean,
  hasActiveProblem: boolean,
): SuggestionItem[] {
  if (!isLatestTurn) {
    return [];
  }
  const exercise = turn.exercise ?? null;
  const hasExplanation = turn.tutoring?.kind === "explanation";
  const hasSelectedQuestionCard = Boolean(turn.question?.selected) && !turn.tutoring;
  return (turn.suggestions ?? []).filter((suggestion) => {
    const label = suggestion.label.trim();
    const intent = classifySuggestion(label);
    if (hasExplanation) {
      if (/(讲解|解析).{0,6}(这|本|该)?(道)?题/.test(label) || intent === "save") {
        return false;
      }
    }
    if (hasSelectedQuestionCard && QUESTION_CARD_ACTIONS.has(label)) {
      return false;
    }
    if (intent === "answer") {
      return Boolean(exercise) && !exercise!.evaluation && exercise!.status !== "submitted";
    }
    if (intent === "reveal") {
      return (
        Boolean(exercise) &&
        !exercise!.evaluation &&
        !exercise!.revealed_solution_at &&
        !exercise!.reference_solution_text
      );
    }
    if (intent === "save") {
      return hasExplanation;
    }
    if (intent === "practice") {
      return hasActiveProblem;
    }
    return true;
  });
}

/** 一轮历史：学生输入、状态和查询或辅导结果。 */
export default function ConversationTurn({
  turn,
  liveProgress,
  unsavedDraft = false,
  downloads,
  onDownload,
  onRetry,
  onRegenerateReply,
  regeneratingReply = false,
  replyQuotaBlocked = false,
  onFeedback,
  onRecord,
  onCorrect,
  onSupplement,
  onNew,
  onIntentChoice,
  onOpenRecord,
  onSubmitExerciseAnswer,
  onRevealExerciseSolution,
  onSuggestionClick,
  onQuestionAction,
  questionOwners,
  questionSnapshots,
  onJumpToQuestion,
  onJumpToExercise,
  onCorrectExercise,
  onGenerateMaterialOutput,
  onConfirmMaterialCoverage,
  onRetryMaterialOutput,
  onOpenMaterialArtifact,
  onOpenWorkbenchSource,
  materialResultIds,
  materialResultCards,
  materialArtifactsById,
  isLatestTurn = true,
  hasActiveProblem = true,
  exerciseRole = "full",
  exerciseAttempt = null,
  hiddenAttemptIds,
  receiptSeqs,
  focusToken = 0,
  correctionToken = 0,
  busy,
  exerciseDrafts,
  exerciseDraftImages,
  exerciseImageNotices,
  imagesBlocked = false,
  solutionImageNames,
  recordsByRoot,
  savingMistakeRootId = null,
  onExerciseDraftChange,
  onAddExerciseImages,
  onRemoveExerciseImage,
  onRetryExerciseImage,
  onSaveExerciseMistake,
}: ConversationTurnProps) {
  const tutoring = turn.tutoring;
  const business = turnBusinessStatus(turn);
  const hasIncompleteReply =
    turn.reply_status === "truncated" || turn.reply_status === "failed";
  const isNormalSuccess =
    !hasIncompleteReply &&
    (business === "succeeded" || (!business && turn.status === "completed"));
  const hasSavedResults = turn.status === "completed" || business === "partial" || business === "succeeded";
  const labels = { succeeded: "成功", partial: "部分完成", failed: "未完成", needs_input: "待补充" };
  const badgeClass = business === "succeeded" ? statusClass("completed") :
    business === "failed" ? statusClass("failed") : business ? statusClass("interrupted") : statusClass(turn.status);

  const resultMessage =
    turn.result && "message" in turn.result ? turn.result.message : null;
  const compositeItems = turn.composite_results ?? [];
  const questionId = turn.question?.question_id;
  const isBankLesson = turn.question?.review.approved && (
    turn.kind === "explain_question" || compositeItems.some((item) =>
      item.status === "success" && (
        item.action === "explain_question" ||
        (item.action === "explain_problem" && item.details?.question_id === questionId)
      ),
    )
  );
  const lesson = hasSavedResults && tutoring?.kind === "explanation" ? explanationBody(tutoring) : "";
  const standaloneLesson = !!lesson && compositeItems.every((item) =>
    item.status === "success" &&
    ["tutoring", "explain_problem", "explain_question", "search_questions", "select_question", "introduce"].includes(item.action),
  );
  const replyNotice = standaloneLesson ? null :
    turn.reply_status === "truncated"
      ? "这段回复生成时被截断，不是完整内容；上面的操作已经完成并保存。"
      : turn.reply_status === "failed"
        ? "回复没有生成成功，上面显示的是系统说明；业务操作已经完成并保存。"
        : null;
  const hasMaterialReceipt = compositeItems.some((item) => item.action === "generate_material_artifact");
  const trimOldArtifactHint = (message: string) => hasMaterialReceipt
    ? message.replace(/(?:章节笔记(?:、知识图谱)?|知识图谱)已展示在下方。|点成果卡即可打开。|点「在会话中答题」开始。/gu, "")
    : message;
  const rawAssistantText = trimOldArtifactHint(turn.assistant_message ?? "");
  const narration =
    turn.result?.kind === "found"
      ? stripDuplicatePaperList(rawAssistantText, turn.result.items)
      : rawAssistantText;
  const assistantText = standaloneLesson ? lesson : [narration, lesson].filter(Boolean).join("\n\n");
  // 优先级：自然语言叙事 → 查询结果消息 → 动作结果消息。
  // 后面的文字只要已被前面的叙事整段包含，就不再单独重复。
  const shownTexts: string[] = [assistantText];
  const showResultMessage =
    Boolean(resultMessage) && !isCoveredText(resultMessage, shownTexts);
  if (showResultMessage && resultMessage) {
    shownTexts.push(resultMessage);
  }
  const hasRecordCard =
    hasSavedResults &&
    (tutoring?.kind === "record_created" || tutoring?.kind === "record_exists");
  const hasQuestionCards =
    hasSavedResults &&
    (Boolean(turn.question_results?.length) || Boolean(turn.question));
  const hasExplanationCard =
    hasSavedResults && tutoring?.kind === "explanation";
  const hasExerciseCard = hasSavedResults && Boolean(turn.exercise);
  const hasVisibleContent =
    Boolean(assistantText) ||
    Boolean(hasSavedResults && turn.result) ||
    hasRecordCard ||
    hasQuestionCards ||
    hasExplanationCard ||
    hasExerciseCard;
  const compositeRows = compositeItems
    .filter((item) => {
      if (assistantText && item.details?.rejected &&
          (item.details.tool === "generate_material_artifact" || item.details.tool === "solve_uploaded_exam")) {
        return false;
      }
      return (
        item.action === "solve_uploaded_exam" ||
        item.action === "generate_material_artifact" ||
        item.status !== "success" ||
        !hasVisibleContent
      );
    })
    .map((item) => {
      const message = item.action === "generate_material_artifact" ? trimOldArtifactHint(item.message) : item.message;
      const showMessage = Boolean(message) && !isCoveredText(message, shownTexts);
      if (showMessage) {
        shownTexts.push(message);
      }
      return { item, message, showMessage };
    });

  const isQuestionRelatedTurn =
    turn.kind === "select_question" ||
    turn.kind === "explain_question" ||
    turn.kind === "view_question_answer" ||
    compositeItems.some((item) =>
      [
        "select_question",
        "explain_question",
        "view_question_answer",
        "generate_exercise",
        "record_mistake",
      ].includes(item.action),
    );
  const exercise = turn.exercise ?? null;
  const hasEvaluationReceiptCard = Boolean(
    exercise && exerciseRole === "attempt" && exerciseAttempt?.evaluation,
  );
  const collapseEvaluationProse =
    hasEvaluationReceiptCard && assistantText.length > 140;
  const evaluationProseLead = collapseEvaluationProse
    ? extractSafeLead(assistantText, 110) ??
      "评价结果已生成，请查看下方评价卡片。"
    : "";
  const mistakeRecordForExercise = exercise
    ? recordsByRoot?.[exercise.root_problem_id] ?? null
    : null;
  // 只有真正提交/重试作答的轮次才需要“去题目卡片查看”的指引；
  // 追问、保存等轮次只是带着练习上下文，不用重复同一块提示。
  const showsExerciseReference =
    Boolean(exercise) &&
    exerciseRole === "reference" &&
    (turn.kind === "exercise" || turn.kind === "exercise_answer");
  const userText = turn.query.trim() || bubbleFallback(turn);
  // 一次查题可同时返回候选和选中对象；同一版本只保留一张，选中快照优先。
  const questionKey = (card: NonNullable<StoredTurn["question"]>) =>
    `${card.question_id}:${card.build_id}:${card.content_sha256}`;
  const selectedThisTurn = Boolean(turn.question?.selected && compositeItems.some((item) =>
    item.status === "success" && item.action === "select_question"));
  const candidates = selectedThisTurn && turn.question ? [turn.question] : (turn.question_results ?? []);
  const questionRows = new Map(candidates.map((card) => [questionKey(card), card]));
  const selectedInResults = Boolean(turn.question && questionRows.has(questionKey(turn.question)));
  if (turn.question && selectedInResults) questionRows.set(questionKey(turn.question), turn.question);
  const references = new Map<string, QuestionCard>();
  const presentCard = (card: QuestionCard): QuestionCard | null => {
    const key = questionKey(card);
    const owner = questionOwners?.get(key);
    const snapshot = questionSnapshots?.get(key);
    if (owner && owner !== turn.request_id) {
      references.set(key, snapshot ?? card);
      return null;
    }
    return snapshot ? { ...card, ...snapshot, source_request_id: card.source_request_id ?? snapshot.source_request_id } : card;
  };
  const shownQuestions = Array.from(questionRows.values()).map(presentCard).filter((card): card is QuestionCard => card !== null);
  const standaloneQuestion = turn.question && !selectedInResults ? presentCard(turn.question) : null;
  const hasQuestionCardButtons = shownQuestions.length > 0 || standaloneQuestion !== null;
  const suggestions = usableSuggestions(turn, isLatestTurn, hasActiveProblem).filter(
    (sug) => {
      const intent = classifySuggestion(sug.label);
      if (hasQuestionCardButtons && (intent === "save" || intent === "practice")) {
        return false;
      }
      if (hasExplanationCard && intent === "save") {
        return false;
      }
      return true;
    },
  );

  return (
    <li className="space-y-3">
      <div className="turn-user">
        {turn.workbench_source && onOpenWorkbenchSource && (
          <button type="button" className="quiet-button text-xs" onClick={() => onOpenWorkbenchSource(turn.workbench_source!)}>
            来自复习工作台 · {turn.workbench_source.label}
          </button>
        )}
        <p className="turn-user-note whitespace-pre-wrap break-words">
          <MathText text={userText} />
        </p>
      </div>

      <div className="turn-assistant">
        <div className="flex flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            {!isNormalSuccess && (
              <span
                className={`rounded-full border px-2.5 py-0.5 text-xs font-medium ${badgeClass}`}
              >
                {business ? labels[business] : STATUS_LABELS[turn.status]}
              </span>
            )}
            <span className="turn-meta">
              第 {turn.seq} 轮 · {formatDateTime(turn.started_at)}
            </span>
          </div>
          {turn.retryable === true && (
            <button
              type="button"
              disabled={busy}
              onClick={() => onRetry(turn)}
              className="rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-xs font-medium text-slate-700 hover:bg-slate-100"
            >
              {turn.kind === "exercise" ? "重试评价" : "重试未完成步骤"}
            </button>
          )}
        </div>

        <div className="min-w-0 space-y-3">
          {business && business !== "succeeded" && turn.retry_hint && (
            <p className="text-xs leading-6 text-amber-900">{turn.retry_hint}</p>
          )}
          {/* 自然语言助手回复；与结果/动作消息完全相同的段落不重复渲染 */}
          {assistantText &&
            (collapseEvaluationProse ? (
              <div className="space-y-1">
                <div className="assistant-prose">
                  <AssistantMarkdown text={evaluationProseLead} />
                </div>
                <details className="lesson-disclosure">
                  <summary className="cursor-pointer text-xs font-medium text-slate-600">
                    查看完整文字回复
                  </summary>
                  <div className="assistant-prose mt-1">
                    <AssistantMarkdown text={assistantText} />
                  </div>
                </details>
              </div>
            ) : (
              <AssistantDraft
                text={assistantText}
                running={unsavedDraft && turn.status === "running"}
                interrupted={unsavedDraft && turn.status === "interrupted"}
              />
            ))}
          {!assistantText && unsavedDraft && (
            <p role="status" className="text-sm text-slate-500">
              {turn.status === "interrupted" ? "回复已中断。" : liveProgress}
            </p>
          )}

          {/* 正文完整性单独提示：业务操作可能已经成功，只是这段话说得不完整 */}
          {replyNotice && (
            <div className="quota-warning">
              <span>{replyNotice}</span>
              {turn.reply_retryable === true && onRegenerateReply && (
                <button
                  type="button"
                  disabled={busy || regeneratingReply || replyQuotaBlocked}
                  onClick={() => onRegenerateReply(turn)}
                  className="ml-2 rounded-lg border border-amber-300 bg-white px-2 py-0.5 text-xs font-medium text-amber-900 hover:bg-amber-100 disabled:opacity-50"
                >
                  {regeneratingReply ? "正在重写…" : "重新生成回复"}
                </button>
              )}
            </div>
          )}

          {/* 复合操作执行结果反馈：内部动作名转成中文，重复消息折叠为状态 */}
          {compositeRows.length > 0 && (
            <div className="space-y-1.5">
              {compositeRows.map(({ item, message, showMessage }, idx) => {
                const det = item.details ?? {};
                const isAttachMat = item.action === "attach_material" && typeof det.material_id === "string";
                const isExamUpload = isAttachMat && (det.material_is_exam === true || looksLikeExam(String(det.material_title ?? "")));
                const isExamSolution = item.action === "solve_uploaded_exam" && typeof det.material_id === "string";
                const sourcePages = Array.isArray(det.source_pages)
                  ? det.source_pages.filter((page): page is number => Number.isInteger(page) && Number(page) > 0)
                  : [];
                if (isExamUpload || (isExamSolution && sourcePages.length === 0)) return null;
                if (isExamSolution) {
                  return (
                    <div key={idx} className="flex flex-wrap gap-2">
                      {sourcePages.map((page) => (
                        <a
                          key={page}
                          href={materialPreviewUrl(String(det.material_id), page)}
                          target="_blank"
                          rel="noreferrer"
                          className="quiet-button text-xs"
                        >
                          原卷第 {page} 页
                        </a>
                      ))}
                    </div>
                  );
                }
                const isGenMat = item.action === "generate_material_artifact" && typeof det.artifact_id === "string";
                const liveArtifact = isGenMat ? materialArtifactsById?.get(String(det.artifact_id)) : undefined;
                const needsInput = det.code === "MATERIAL_COVERAGE_REQUIRED";
                const previousPartial = Boolean(liveArtifact?.coverage?.missing_pages.length);
                const pendingOutputs: OutputKind[] = Array.isArray(det.outputs)
                  ? det.outputs.filter((value): value is OutputKind => value === "notes" || value === "quiz" || value === "graph")
                  : [];
                const materialId = det.material_id;
                const startPage = det.start_page;
                const endPage = det.end_page;
                const confirmCoverage = item.action === "generate_material_artifact" &&
                  (needsInput || previousPartial) && onConfirmMaterialCoverage &&
                  typeof materialId === "string" && typeof startPage === "number" && typeof endPage === "number" &&
                  pendingOutputs.length > 0
                  ? (mode: ArtifactInputMode) => onConfirmMaterialCoverage(
                    materialId, pendingOutputs, startPage, endPage, mode,
                    typeof det.course_hint === "string" ? det.course_hint : liveArtifact?.course_hint,
                  )
                  : undefined;
                const hasResultCard = materialResultIds?.includes(String(det.artifact_id));
                const visibleOutputs = Array.isArray(det.outputs) ? pendingOutputs : (["notes", "quiz", "graph"] as const);
                const getMaterialStatus = (output: OutputKind) =>
                  liveArtifact?.[`${output}_status`] ?? det[`${output}_status`];
                const failedOutput = isGenMat && visibleOutputs.some((output) => getMaterialStatus(output) === "failed");
                const effectiveStatus = failedOutput ? "failed" : item.status;
                if (isGenMat && effectiveStatus === "success" && !previousPartial &&
                    hasResultCard) {
                  return null;
                }
                return (
                  <div
                    key={idx}
                    className={`space-y-2 rounded-xl px-3 py-2 text-xs ${
                      effectiveStatus === "success"
                        ? "border border-slate-200 bg-slate-50/80 text-slate-700"
                        : effectiveStatus === "clarification"
                          ? "border border-amber-200 bg-amber-50 text-amber-800"
                          : "border border-rose-200 bg-rose-50 text-rose-700"
                    }`}
                  >
                    {(showMessage || !isGenMat || effectiveStatus !== "success") && (
                      <div className="flex items-center justify-between gap-2">
                        <span className="break-words">
                          {showMessage ? message : actionStatusLabel(effectiveStatus)}
                        </span>
                      </div>
                    )}

                    {isAttachMat && (
                      <div className="flex flex-wrap items-center gap-1.5">
                        <a
                          href={materialPreviewUrl(String(det.material_id))}
                          target="_blank"
                          rel="noreferrer"
                          className="quiet-button text-xs"
                        >
                          预览原件（{String(det.page_count ?? "")} 页）
                        </a>
                        {det.sufficient !== false && onGenerateMaterialOutput && (
                          <>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => onGenerateMaterialOutput(String(det.material_id), "notes")}
                              className="quiet-button text-xs"
                            >
                              章节笔记
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => onGenerateMaterialOutput(String(det.material_id), "quiz")}
                              className="quiet-button text-xs"
                            >
                              章节测试
                            </button>
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => onGenerateMaterialOutput(String(det.material_id), "graph")}
                              className="quiet-button text-xs"
                            >
                              知识图谱
                            </button>
                          </>
                        )}
                      </div>
                    )}

                    {confirmCoverage && (
                      <div className="space-y-2">
                        <p>每个待识别页会将原页图片发送给 DeepSeek，预计每页使用 1 次模型请求。识别完成后继续生成。</p>
                        <div className="flex flex-wrap gap-2">
                          <button type="button" className="quiet-button" disabled={busy}
                            onClick={() => confirmCoverage("recognize")}>
                            {previousPartial ? "补齐并生成新版" : "识别并生成"}
                          </button>
                          {needsInput && det.can_generate_partial === true && (
                            <button type="button" className="quiet-button" disabled={busy}
                              onClick={() => confirmCoverage("partial")}>
                              仅生成部分成果
                            </button>
                          )}
                        </div>
                      </div>
                    )}
                    {isGenMat && (
                      <div className="flex flex-wrap items-center gap-1.5">
                        {previousPartial && <span>资料有缺页</span>}
                        {!hasResultCard && onOpenMaterialArtifact && visibleOutputs.some((output) => getMaterialStatus(output) === "ready") && (
                          <button type="button" className="quiet-button" onClick={() => onOpenMaterialArtifact(
                            String(det.artifact_id), String(det.material_title ?? liveArtifact?.title ?? "个人资料"),
                            Number(liveArtifact?.end_page ?? det.end_page ?? 0),
                          )}>定位复习成果</button>
                        )}
                        {visibleOutputs.map((outKind) => {
                          const st = getMaterialStatus(outKind);
                          const label =
                            outKind === "notes"
                              ? "章节笔记"
                              : outKind === "quiz"
                                ? "章节测试"
                                : "知识图谱";
                          if (st === "failed" && onRetryMaterialOutput) {
                            return (
                              <button
                                key={outKind}
                                type="button"
                                disabled={busy}
                                onClick={() => onRetryMaterialOutput(String(det.artifact_id), outKind)}
                                className="rounded-md border border-rose-200 bg-rose-50 px-2 py-0.5 font-medium text-rose-800 hover:bg-rose-100 disabled:opacity-50"
                              >
                                重试{label}
                              </button>
                            );
                          }
                          return null;
                        })}
                      </div>
                    )}
                  </div>
                );
              })}
            </div>
          )}

          {materialResultCards}

          {/* 试卷检索结果（普通查询轮次或动态查卷动作产出） */}
          {hasSavedResults && turn.result && (
            <QueryResult
              result={turn.result}
              downloads={downloads}
              showMessage={showResultMessage}
              onDownload={onDownload}
            />
          )}

          {/* 真题候选：只显示已审核题目的公开字段，答案需另行开放 */}
          {hasSavedResults && shownQuestions.length > 0 && (
            <QuestionResultList
              cards={shownQuestions}
              status={turn.question_result_status}
              message={turn.question_result_message}
              busy={busy}
              onAction={onQuestionAction}
            />
          )}

          {hasSavedResults && standaloneQuestion && (
            <QuestionCardView
              card={standaloneQuestion}
              busy={busy}
              onAction={onQuestionAction}
            />
          )}
          {hasSavedResults && isQuestionRelatedTurn && Array.from(references, ([key, card]) => (
            <div key={key} className="flex flex-wrap items-center gap-3 rounded-xl border border-slate-200 bg-slate-50 px-3 py-2 text-xs text-slate-600"
              data-question-reference={key}>
              <span>当前关联第 {card.number_label} 题</span>
              <button type="button" className="text-river underline" onClick={() => onJumpToQuestion?.(key)}>回到题目</button>
            </div>
          ))}

          {/* 练习：一张题目主卡 + 每次作答的紧凑回执，避免两张相同大卡片 */}
          {exercise && exerciseRole === "attempt" && exerciseAttempt && (
            <ExerciseAttemptReceipt
              exercise={exercise}
              attempt={exerciseAttempt}
              busy={busy}
              onJump={() => onJumpToExercise?.(exercise.id)}
              onCorrect={
                onCorrectExercise ? () => onCorrectExercise(exercise.id) : undefined
              }
              mistakeRecord={hasRecordCard ? null : mistakeRecordForExercise}
              savingMistake={savingMistakeRootId === exercise.root_problem_id}
              onSaveMistake={hasRecordCard ? undefined : onSaveExerciseMistake}
              onOpenRecord={onOpenRecord}
            />
          )}

          {showsExerciseReference && exercise && (
            <div className="reading-note">
              <p className="text-xs leading-6 text-slate-600">
                这道练习的题目在上方主卡；本次作答与评价保留在卡片的最新记录中。
              </p>
              <button
                type="button"
                onClick={() => onJumpToExercise?.(exercise.id)}
                className="mt-2 text-xs font-medium text-river underline decoration-river/30 hover:text-river/80"
              >
                回到题目
              </button>
            </div>
          )}

          {exercise && exerciseRole === "full" && (
            <ExerciseCard
              exercise={exercise}
              busy={busy}
              anchorId={`exercise-card-${exercise.id}`}
              hiddenAttemptIds={hiddenAttemptIds}
              receiptSeqs={receiptSeqs}
              focusToken={focusToken}
              correctionToken={correctionToken}
              onSubmitAnswer={onSubmitExerciseAnswer}
              onRevealSolution={onRevealExerciseSolution}
              draft={exerciseDrafts?.[exercise.id]}
              draftImages={exerciseDraftImages?.[exercise.id]}
              imageNotice={exerciseImageNotices?.[exercise.id] ?? null}
              imagesBlocked={imagesBlocked}
              solutionImageNames={solutionImageNames}
              onDraftChange={(draft) => onExerciseDraftChange?.(exercise.id, draft)}
              onAddImages={onAddExerciseImages}
              onRemoveImage={onRemoveExerciseImage}
              onRetryImage={onRetryExerciseImage}
              mistakeRecord={hasRecordCard ? null : mistakeRecordForExercise}
              savingMistake={savingMistakeRootId === exercise.root_problem_id}
              onSaveMistake={hasRecordCard ? undefined : onSaveExerciseMistake}
              onOpenRecord={onOpenRecord}
            />
          )}

          {hasSavedResults &&
            tutoring?.kind === "explanation" && (
              <ProblemCard
                result={tutoring}
                showStatement={!turn.question}
                canCorrect={!isBankLesson || tutoring.problem.clarity === "unclear"}
                busy={busy}
                onFeedback={onFeedback}
                onRecord={onRecord}
                onCorrect={onCorrect}
                onSupplement={onSupplement}
              />
            )}

          {hasSavedResults &&
            tutoring?.kind === "needs_clarification" && (
              <div className="lesson-sheet">
                <p className="text-sm font-semibold text-amber-900">
                  题目还不清楚，先补充再讲解
                </p>
                <p className="lesson-statement">
                  <MathText text={tutoring.problem.statement_text} />
                </p>
                <ul className="mt-2 list-disc space-y-1 pl-5 text-xs leading-6 text-amber-900">
                  {tutoring.questions.map((question) => (
                    <li key={question}>{question}</li>
                  ))}
                </ul>
                <div className="mt-3 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => onSupplement(tutoring.problem)}
                    className="rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
                  >
                    补充当前题
                  </button>
                  <button
                    type="button"
                    onClick={() => onCorrect(tutoring.problem)}
                    className="rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 hover:bg-amber-100"
                  >
                    直接纠正题干
                  </button>
                </div>
              </div>
            )}

          {hasSavedResults &&
            tutoring?.kind === "intent_clarification" && (
              <div className="reading-note">
                <p className="text-sm leading-6 text-slate-700">
                  {tutoring.message}
                </p>
                <div className="mt-3 flex flex-wrap gap-2">
                  {tutoring.options.map((option) => (
                    <button
                      key={option}
                      type="button"
                      onClick={() => {
                        if (option === "new_problem") onNew(turn.query);
                        else onIntentChoice(turn, option);
                      }}
                      className="rounded-lg border border-river/30 bg-white px-3 py-1.5 text-xs font-medium text-river hover:bg-river/5"
                    >
                      {OPTION_LABELS[option] ?? option}
                    </button>
                  ))}
                </div>
              </div>
            )}

          {/* 正文已承载追问回答时不再在卡片区全文重复；仅旧记录缺正文时兜底展示。 */}
          {hasSavedResults &&
            tutoring?.kind === "followup_answer" &&
            !assistantText.trim() && (
              <div className="reading-note">
                <p className="text-xs font-medium text-slate-500">追问回答</p>
                <div className="assistant-prose mt-1">
                  <AssistantMarkdown text={tutoring.answer} />
                </div>
                {tutoring.related_steps.length > 0 && (
                  <p className="mt-2 text-xs text-slate-500">
                    相关步骤：{tutoring.related_steps.join("、")}
                  </p>
                )}
              </div>
            )}

          {hasSavedResults &&
            (tutoring?.kind === "record_created" ||
              tutoring?.kind === "record_exists") && (
              <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-emerald-800">
                <button
                  type="button"
                  onClick={() => onOpenRecord(tutoring.record.id)}
                  className="inline-flex items-center gap-1 rounded-full border border-emerald-300 bg-emerald-50 px-3 py-1 font-medium text-emerald-900 hover:bg-emerald-100"
                >
                  已加入错题本 · 查看
                </button>
                {tutoring.kind === "record_exists" && tutoring.needs_update && (
                  <span className="text-slate-500">
                    （原记录对应旧题干，可在错题本中更新）
                  </span>
                )}
              </div>
            )}

          {hasSavedResults &&
            tutoring?.kind === "feedback_recorded" && (
              <div className="reading-note">
                <p className="text-sm leading-6 text-emerald-900">
                  {tutoring.message}
                </p>
              </div>
            )}

          {turn.status === "failed" && turn.error && (
            <div>
              <p className="break-words text-sm leading-6 text-rose-800">
                {turn.error.message}
              </p>
              <details className="mt-1 text-xs text-rose-500">
                <summary className="cursor-pointer">诊断信息</summary>
                <p className="mt-1">错误码：{turn.error.code}</p>
              </details>
            </div>
          )}

          {turn.status === "interrupted" && (
            <p className="text-sm leading-6 text-amber-800">
              {business === "succeeded"
                ? "连接未完整结束，已确认的业务结果已经保存。"
                : business === "partial"
                  ? "连接中断，已成功的操作会保留，可按提示恢复未完成步骤。"
                  : "这一轮因连接停止或中断而未完成，可按提示继续操作。"}
            </p>
          )}

          {turn.status === "running" && (
            <p className="text-sm leading-6 text-slate-600">
              这一轮仍在进行中。若长时间没有变化，请刷新会话记录确认状态。
            </p>
          )}

          {/* 快捷建议操作：只在最新一轮展示仍然有效的建议 */}
          {suggestions.length > 0 && (
            <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-slate-100 pt-3">
              <span className="text-xs text-slate-400">建议下一步：</span>
              {suggestions.map((sug) => (
                <button
                  key={`${sug.action}-${sug.label}`}
                  type="button"
                  disabled={busy}
                  onClick={() => onSuggestionClick?.(turn, sug)}
                  className="rounded-full border border-indigo-200 bg-indigo-50/70 px-3 py-1 text-xs font-medium text-indigo-700 hover:bg-indigo-100 disabled:opacity-40"
                >
                  {sug.label}
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </li>
  );
}
