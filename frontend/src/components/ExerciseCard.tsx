import {
  useEffect,
  useRef,
  useState,
  type ChangeEvent,
  type ClipboardEvent,
} from "react";
import type {
  AttachmentInput,
  AttachmentView,
  ExerciseAttemptView,
  ExerciseEvaluation,
  ExerciseView,
  RecordRef,
} from "../lib/conversationsApi";
import { attachmentUrl, validateLocalImage } from "../lib/attachmentsApi";
import { mathSafeExcerpt } from "../lib/display";
import MathText from "./MathText";

type AttemptKind = "supplement" | "correction";

export type ExerciseDraftImage = {
  localId: string;
  file: File;
  previewUrl: string;
  status: "uploading" | "ready" | "error";
  uploadedId?: string;
  error?: string;
};

export function resolveExerciseAttemptStatus(exercise: ExerciseView): {
  code: "submitted" | "needs_supplement" | "revealed" | "pending_answer";
  label: string;
} {
  const hasAttempts = (exercise.attempts?.length ?? 0) > 0;
  const hasEvaluation = Boolean(exercise.evaluation);
  const hasContent = Boolean(
    exercise.student_answer_text?.trim() ||
      exercise.student_solution_text?.trim() ||
      (exercise.attachments?.length ?? 0) > 0,
  );
  if (exercise.evaluation?.needs_supplement) {
    return { code: "needs_supplement", label: "已作答 · 待补充过程" };
  }
  if (exercise.status === "submitted" || hasAttempts || hasEvaluation || hasContent) {
    return { code: "submitted", label: "已提交作答" };
  }
  if (exercise.status === "revealed" || Boolean(exercise.revealed_solution_at)) {
    return { code: "revealed", label: "已查看参考解答" };
  }
  return { code: "pending_answer", label: "未提交" };
}

function formatPreviewButton(
  src: string,
  alt: string,
  tag: string,
  onClick: () => void,
) {
  return (
    <button
      type="button"
      className="attachment-thumb-card"
      onClick={onClick}
      aria-label={`预览${alt}`}
    >
      <img src={src} alt={alt} className="attachment-thumb-img" />
      <span className="attachment-thumb-tag">{tag}</span>
    </button>
  );
}

function ImagePreviewModal({
  src,
  alt,
  onClose,
}: {
  src: string;
  alt: string;
  onClose: () => void;
}) {
  return (
    <div
      role="dialog"
      aria-label="图片预览"
      className="image-preview-modal"
      onClick={onClose}
    >
      <div
        className="image-preview-dialog"
        onClick={(event) => event.stopPropagation()}
      >
        <img src={src} alt={alt} className="image-preview-full" />
        <button
          type="button"
          className="quiet-button image-preview-close"
          onClick={onClose}
        >
          关闭预览
        </button>
      </div>
    </div>
  );
}

export function AttachmentThumbnails({
  attachments,
  conversationId,
}: {
  attachments: AttachmentView[];
  conversationId?: string;
}) {
  const [previewItem, setPreviewItem] = useState<AttachmentView | null>(null);
  if (!attachments.length) {
    return null;
  }
  const srcOf = (item: AttachmentView) =>
    item.url || (conversationId ? attachmentUrl(conversationId, item.id) : "");
  return (
    <>
      <div className="attachment-thumb-list">
        {attachments.map((item, idx) => {
          const roleText =
            item.role === "student_answer" ? "作答图" : "过程图";
          return (
            <div key={item.id} className="attachment-thumb-item">
              {formatPreviewButton(
                srcOf(item),
                `${roleText} ${idx + 1}`,
                roleText,
                () => setPreviewItem(item),
              )}
            </div>
          );
        })}
      </div>
      {previewItem && (
        <ImagePreviewModal
          src={srcOf(previewItem)}
          alt="作答过程图预览"
          onClose={() => setPreviewItem(null)}
        />
      )}
    </>
  );
}

type ExerciseCardProps = {
  exercise: ExerciseView;
  busy?: boolean;
  draft?: { answer: string; solution: string };
  draftImages?: ExerciseDraftImage[];
  imageNotice?: string | null;
  imagesBlocked?: boolean;
  solutionImageNames?: string[];
  /** 已在别处（对应作答轮次）展示的尝试，不在本题主卡里重复。 */
  hiddenAttemptIds?: string[];
  /** 这些作答回执所在的轮次，用于告诉学生评价去哪里看。 */
  receiptSeqs?: number[];
  /** 主卡锚点，供“查看完整题目”定位。 */
  anchorId?: string;
  /** 递增令牌：请求聚焦作答框（来自建议按钮）。 */
  focusToken?: number;
  /** 递增令牌：请求展开订正表单（来自作答回执）。 */
  correctionToken?: number;
  mistakeRecord?: RecordRef | null;
  savingMistake?: boolean;
  onDraftChange?: (draft: { answer: string; solution: string }) => void;
  onAddImages?: (exerciseId: string, files: File[]) => void;
  onRemoveImage?: (exerciseId: string, localId: string) => void;
  onRetryImage?: (exerciseId: string, localId: string) => void;
  onSubmitAnswer?: (
    exerciseId: string,
    answerText: string,
    solutionText?: string,
    attemptKind?: AttemptKind,
    attachments?: AttachmentInput[],
  ) => void;
  onRevealSolution?: (exerciseId: string) => void;
  onSaveMistake?: (exercise: ExerciseView) => void;
  onOpenRecord?: (recordId: string) => void;
};

const KIND_LABELS: Record<string, string> = {
  initial: "首次作答",
  supplement: "补充过程",
  correction: "订正",
};

export { KIND_LABELS };

function firstSentence(text: string): string {
  const trimmed = text.trim();
  if (trimmed.length <= 96) {
    return trimmed;
  }
  const match = trimmed.match(/^.{12,96}[。！？!?]/);
  // 句号截断与长度截断都不能切断公式，否则 KaTeX 解析失败会暴露源码。
  if (match && mathSafeExcerpt(trimmed, match[0].length + 1) === `${match[0].trimEnd()}…`) {
    return match[0];
  }
  return mathSafeExcerpt(trimmed, 96);
}

function truncateReference(text: string, maxLen = 280): string {
  return mathSafeExcerpt(text, maxLen);
}

export function EvaluationBlock({
  evaluation,
  answeredAfterRevealed,
  studentAnswerText,
  referenceSolutionText,
  hasProcess = true,
}: {
  evaluation: ExerciseEvaluation;
  answeredAfterRevealed: boolean;
  studentAnswerText?: string | null;
  referenceSolutionText?: string | null;
  hasProcess?: boolean;
}) {
  const afterReveal =
    answeredAfterRevealed ||
    evaluation.independence_assessment === "after_reveal";
  const independenceLabel = afterReveal
    ? "已看解答后作答"
    : evaluation.independence_assessment === "independent"
      ? "独立作答"
      : evaluation.independence_assessment === "unclear"
        ? "独立性待确认"
        : null;
  const showProcessAnalysis = hasProcess || evaluation.needs_supplement;
  const fullAnalysis = evaluation.step_by_step_analysis.trim();
  const isLongAnalysis = fullAnalysis.length > 100;
  const diagnosticSummary = evaluation.needs_supplement
    ? "未提供完整过程，暂无法定位具体错误步骤。"
    : isLongAnalysis
      ? firstSentence(fullAnalysis)
      : fullAnalysis;
  const longIndependenceNote =
    evaluation.independence_note ||
    (afterReveal
      ? "这次作答是在参考解答展示之后完成的；答案正确不代表独立掌握，建议遮住解答后完整重做一遍。"
      : "");
  const trimmedRef = referenceSolutionText?.trim() ?? "";
  const isLongReference = trimmedRef.length > 280;
  const hasDisclosure =
    (showProcessAnalysis && (isLongAnalysis || (evaluation.needs_supplement && Boolean(fullAnalysis)))) ||
    Boolean(evaluation.final_answer_assessment?.trim()) ||
    Boolean(longIndependenceNote) ||
    isLongReference;

  return (
    <div
      className={
        evaluation.is_correct === false
          ? "red-ink space-y-2 text-sm"
          : "mt-2 space-y-2 text-sm leading-6 text-slate-800"
      }
    >
      <div className="flex flex-wrap items-center gap-2">
        <span
          className={`rounded-full px-2 py-0.5 text-xs font-medium ${
            evaluation.is_correct === true
              ? "border border-emerald-300 bg-emerald-100 text-emerald-800"
              : evaluation.is_correct === false
                ? "border border-rose-300 bg-rose-100 text-rose-800"
                : "border border-amber-300 bg-amber-100 text-amber-800"
          }`}
        >
          {evaluation.is_correct === true
            ? "答案正确"
            : evaluation.is_correct === false
              ? "答案有误"
              : "待补充依据"}
        </span>
        {evaluation.needs_supplement && (
          <span className="text-xs font-medium text-amber-800">
            需补充过程
          </span>
        )}
        {independenceLabel && (
          <span
            className={`rounded-full px-2 py-0.5 text-xs ${
              afterReveal
                ? "border border-amber-300 bg-amber-50 text-amber-900"
                : "border border-slate-200 bg-slate-50 text-slate-700"
            }`}
          >
            {independenceLabel}
          </span>
        )}
      </div>

      {(studentAnswerText || trimmedRef) && (
        <div className="space-y-1 text-xs leading-6 text-slate-800">
          {studentAnswerText && (
            <p>
              <span className="font-semibold text-slate-600">你的答案：</span>
              <MathText text={studentAnswerText} />
            </p>
          )}
          {trimmedRef && (
            <p className="text-emerald-900">
              <span className="font-semibold">参考解答：</span>
              <MathText text={truncateReference(trimmedRef)} />
            </p>
          )}
        </div>
      )}

      {showProcessAnalysis && (
        <p>
          <span className="font-semibold text-slate-600">过程诊断：</span>
          <MathText text={diagnosticSummary} />
        </p>
      )}

      {evaluation.guidance && (
        <p className="text-river">
          <span className="font-semibold">建议：</span>
          <MathText text={evaluation.guidance} />
        </p>
      )}

      {hasDisclosure && (
        <details className="lesson-disclosure">
          <summary className="cursor-pointer text-xs font-medium text-slate-600">
            查看完整分析与说明
          </summary>
          <div className="mt-2 space-y-2 text-xs leading-6 text-slate-700">
            {evaluation.final_answer_assessment && (
              <p>
                <span className="font-semibold text-slate-600">结果点评：</span>
                <MathText text={evaluation.final_answer_assessment} />
              </p>
            )}
            {showProcessAnalysis && (isLongAnalysis || evaluation.needs_supplement) && fullAnalysis && (
              <p>
                <span className="font-semibold text-slate-600">完整步骤分析：</span>
                <MathText text={fullAnalysis} />
              </p>
            )}
            {longIndependenceNote && (
              <p
                className={
                  afterReveal
                    ? "rounded-lg border border-amber-300 bg-amber-50 p-2 text-amber-900"
                    : "rounded-lg border border-slate-200 bg-slate-50 p-2 text-slate-700"
                }
              >
                <span className="font-semibold">
                  {afterReveal ? "对照作答说明：" : "独立性说明："}
                </span>
                {longIndependenceNote}
              </p>
            )}
            {isLongReference && (
              <p>
                <span className="font-semibold text-emerald-800">完整参考解答：</span>
                <MathText text={trimmedRef} />
              </p>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

export default function ExerciseCard({
  exercise,
  busy = false,
  draft,
  draftImages,
  imageNotice = null,
  imagesBlocked = false,
  solutionImageNames = [],
  hiddenAttemptIds,
  receiptSeqs,
  anchorId,
  focusToken = 0,
  correctionToken = 0,
  mistakeRecord = null,
  savingMistake = false,
  onDraftChange,
  onAddImages,
  onRemoveImage,
  onRetryImage,
  onSubmitAnswer,
  onRevealSolution,
  onSaveMistake,
  onOpenRecord,
}: ExerciseCardProps) {
  const attempts = exercise.attempts ?? [];
  const latest = attempts.length ? attempts[attempts.length - 1] : null;
  const hiddenIds = new Set(hiddenAttemptIds ?? []);
  const visibleAttempts = attempts.filter((attempt) => !hiddenIds.has(attempt.id));
  const displayAttempt: ExerciseAttemptView | null = visibleAttempts.length
    ? visibleAttempts[visibleAttempts.length - 1]
    : null;
  const displayAnswer =
    displayAttempt?.student_answer_text ??
    (attempts.length === 0 ? exercise.student_answer_text : null);
  const displaySolution =
    displayAttempt?.student_solution_text ??
    (attempts.length === 0 ? exercise.student_solution_text : null);
  const displayAttachments =
    displayAttempt?.attachments ??
    (attempts.length === 0 ? exercise.attachments : []);
  const displayEvaluation =
    displayAttempt?.evaluation ??
    (attempts.length === 0 ? exercise.evaluation : null);
  const displayAfterReveal =
    displayAttempt?.answered_after_revealed ??
    (attempts.length === 0 ? exercise.answered_after_revealed : false);
  const historyAttempts = displayAttempt
    ? visibleAttempts.filter((attempt) => attempt.id !== displayAttempt.id)
    : visibleAttempts;
  const latestAnswer = latest?.student_answer_text ?? exercise.student_answer_text;
  const latestSolution =
    latest?.student_solution_text ?? exercise.student_solution_text;
  const latestAttachments = latest?.attachments ?? exercise.attachments;
  const latestEvaluation = latest?.evaluation ?? exercise.evaluation;
  const latestAfterReveal =
    latest?.answered_after_revealed ?? exercise.answered_after_revealed;
  // 被回执接管的尝试不在主卡回填，避免把隐藏的作答又显示一次。
  const baseAnswer =
    displayAttempt?.student_answer_text ??
    (attempts.length === 0 ? exercise.student_answer_text : "");
  const baseSolution =
    displayAttempt?.student_solution_text ??
    (attempts.length === 0 ? exercise.student_solution_text : "");

  const [localAnswer, setAnswerInput] = useState(baseAnswer || "");
  const [localSolution, setSolutionInput] = useState(baseSolution || "");
  const [localDraftImages, setLocalDraftImages] = useState<ExerciseDraftImage[]>([]);
  const [localImageNotice, setLocalImageNotice] = useState<string | null>(null);
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [revealing, setRevealing] = useState(false);
  const [correcting, setCorrecting] = useState(false);
  const answerRef = useRef<HTMLInputElement>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // 记录已处理的令牌，避免切换会话后重新挂载卡片时自动聚焦/展开订正。
  const handledFocusTokenRef = useRef(focusToken);
  const handledCorrectionTokenRef = useRef(correctionToken);

  useEffect(() => {
    setLocalDraftImages([]);
    setLocalImageNotice(null);
    setPreviewUrl(null);
  }, [exercise.id]);

  useEffect(() => {
    setAnswerInput(baseAnswer || "");
    setSolutionInput(baseSolution || "");
    setCorrecting(false);
    // 只在新尝试落库后同步；用户正在输入时 attempts.length 不会变化。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [attempts.length, hiddenAttemptIds?.join(",")]);

  const hasRevealed =
    exercise.status === "revealed" || Boolean(exercise.revealed_solution_at);
  const needsSupplement = latestEvaluation?.needs_supplement === true;
  const isSubmitted = Boolean(latestEvaluation) && !needsSupplement;
  const isEvaluationPending =
    !latestEvaluation &&
    Boolean(latestAnswer || latestSolution || latestAttachments.length > 0);

  const answerInput = correcting ? localAnswer : draft?.answer ?? localAnswer;
  const solutionInput = correcting
    ? localSolution
    : draft?.solution ?? localSolution;
  const activeDraftImages = draftImages ?? localDraftImages;
  const activeImageNotice = imageNotice ?? localImageNotice;
  const hasUploadingImage = activeDraftImages.some(
    (item) => item.status === "uploading",
  );
  const hasErrorImage = activeDraftImages.some(
    (item) => item.status === "error",
  );
  const readyAttachments: AttachmentInput[] = activeDraftImages
    .filter((item) => item.status === "ready" && Boolean(item.uploadedId))
    .map((item) => ({ id: item.uploadedId!, role: "solution" }));
  const showForm = !isSubmitted || correcting;
  const attemptKind: AttemptKind | undefined = needsSupplement
    ? "supplement"
    : correcting
      ? "correction"
      : undefined;

  const handleIncomingFiles = (incoming: File[]) => {
    if (!incoming.length || busy || imagesBlocked) return;
    if (onAddImages) {
      onAddImages(exercise.id, incoming);
      return;
    }
    setLocalImageNotice(null);
    const next: ExerciseDraftImage[] = [];
    for (const file of incoming) {
      if (localDraftImages.length + next.length >= 4) {
        setLocalImageNotice("单次作答最多附带 4 张过程图片。");
        break;
      }
      const err = validateLocalImage(file);
      if (err) {
        setLocalImageNotice(err);
        continue;
      }
      next.push({
        localId: `${exercise.id}-${Date.now()}-${Math.random().toString(16).slice(2)}`,
        file,
        previewUrl:
          typeof URL.createObjectURL === "function"
            ? URL.createObjectURL(file)
            : "",
        status: "ready",
        uploadedId: `local-${next.length + localDraftImages.length + 1}`,
      });
    }
    if (next.length) {
      setLocalDraftImages((prev) => [...prev, ...next]);
    }
  };

  const handleFileChange = (event: ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(event.target.files ?? []);
    event.target.value = "";
    handleIncomingFiles(selected);
  };

  const handlePaste = (
    event: ClipboardEvent<HTMLTextAreaElement | HTMLInputElement>,
  ) => {
    const fileList = Array.from(event.clipboardData?.files ?? []);
    const itemFiles = Array.from(event.clipboardData?.items ?? [])
      .filter((item) => item.kind === "file")
      .map((item) => item.getAsFile())
      .filter((file): file is File => Boolean(file));
    const images = (fileList.length ? fileList : itemFiles).filter((file) =>
      file.type.startsWith("image/"),
    );
    if (images.length) {
      event.preventDefault();
      handleIncomingFiles(images);
    }
  };

  const removeDraftImage = (localId: string) => {
    if (onRemoveImage) {
      onRemoveImage(exercise.id, localId);
      return;
    }
    setLocalDraftImages((prev) => prev.filter((item) => item.localId !== localId));
    setLocalImageNotice(null);
  };

  const handleReveal = async () => {
    if (!onRevealSolution || revealing || busy) return;
    setRevealing(true);
    try {
      await onRevealSolution(exercise.id);
    } finally {
      setRevealing(false);
    }
  };

  const startCorrection = () => {
    setCorrecting(true);
    setAnswerInput("");
    setSolutionInput("");
    onDraftChange?.({ answer: "", solution: "" });
  };

  const handleSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    if (!onSubmitAnswer || busy || hasUploadingImage || hasErrorImage) return;
    const trimmedAns = answerInput.trim();
    const trimmedSol = solutionInput.trim();
    if (
      !trimmedAns &&
      !trimmedSol &&
      !readyAttachments.length &&
      !solutionImageNames.length
    ) {
      return;
    }
    if (readyAttachments.length > 0) {
      onSubmitAnswer(
        exercise.id,
        trimmedAns,
        trimmedSol,
        attemptKind,
        readyAttachments,
      );
    } else {
      onSubmitAnswer(exercise.id, trimmedAns, trimmedSol, attemptKind);
    }
  };

  useEffect(() => {
    if (focusToken > handledFocusTokenRef.current && showForm) {
      answerRef.current?.focus();
    }
    handledFocusTokenRef.current = focusToken;
    // 只在点击“提交我的答案”时聚焦一次，不随普通输入变化。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusToken]);

  useEffect(() => {
    if (correctionToken > handledCorrectionTokenRef.current && isSubmitted) {
      startCorrection();
      window.setTimeout(() => answerRef.current?.focus(), 0);
    }
    handledCorrectionTokenRef.current = correctionToken;
    // 回执里的“回到题目并订正”每次点击都会递增令牌。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [correctionToken]);

  const statusText = needsSupplement
    ? "待补充过程"
    : isSubmitted
      ? attempts.length > 1
        ? `已评价（${attempts.length} 次作答）`
        : "已提交评价"
      : isEvaluationPending
        ? "评价未完成"
        : hasRevealed
          ? "已看解答"
          : "等待作答";

  const submitLabel = busy
    ? "评价中..."
    : needsSupplement
      ? "补充并重新评价"
      : correcting
        ? "提交订正并重新评价"
        : "提交作答并评价";

  return (
    <div
      id={anchorId}
      className="rounded-2xl practice-sheet"
    >
      <div className="sheet-meta">
        <div className="flex flex-wrap items-center gap-2">
          <span className="sheet-badge">
            AI 生成类似题
          </span>
          {!exercise.source_question && (
            <span className="rounded-full border border-slate-200 bg-slate-100 px-2.5 py-0.5 text-xs font-medium text-slate-700">
              模型生成 · 非真题
            </span>
          )}
          {exercise.source_question && (
            <span
              title={exercise.source_question.paper_title ?? exercise.source_question.paper_id}
              className="max-w-full truncate rounded-full border border-river/30 bg-river/10 px-2.5 py-0.5 text-xs font-medium text-river"
            >
              根据真题生成：第 {exercise.source_question.number_label ?? "?"} 题
            </span>
          )}
          {exercise.knowledge_point && (
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-xs font-medium text-slate-600">
              考点：{exercise.knowledge_point}
            </span>
          )}
          {latestAfterReveal && !displayEvaluation && (
            <span className="rounded-full border border-amber-300 bg-amber-50 px-2.5 py-0.5 text-xs font-medium text-amber-800">
              已看解答后作答
            </span>
          )}
        </div>
        <span className="text-xs font-medium text-slate-400">
          {statusText}
        </span>
      </div>

      <p className="lesson-statement">
        <MathText text={exercise.statement_text} />
      </p>

      {hiddenIds.size > 0 && (
        <p className="mt-2 text-xs leading-5 text-slate-500">
          本题共 {attempts.length} 次作答；每次作答与评价按提交顺序显示在各自轮次
          {receiptSeqs && receiptSeqs.length > 0
            ? `（第 ${receiptSeqs.join("、")} 轮）`
            : ""}
          ，题目主卡不重复展开。
        </p>
      )}

      {(displayAnswer || displaySolution || displayAttachments.length > 0) && (
        <div className="student-work">
          <p className="text-xs font-medium text-slate-700">
            {displayAttempt
              ? `第 ${displayAttempt.attempt_no} 次 · ${KIND_LABELS[displayAttempt.kind] ?? "本次作答"}：`
              : "你的作答"}
          </p>
          {displayAnswer && (
            <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
              <span className="font-semibold text-slate-600">最终答案：</span>
              <MathText text={displayAnswer} />
            </p>
          )}
          {displaySolution &&
            (displaySolution.trim().length > 120 ? (
              <details className="lesson-disclosure mt-1">
                <summary className="cursor-pointer text-xs font-medium text-slate-600">
                  提交的解题步骤（点击展开）
                </summary>
                <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
                  <span className="font-semibold text-slate-600">解题步骤：</span>
                  <MathText text={displaySolution} />
                </p>
              </details>
            ) : (
              <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
                <span className="font-semibold text-slate-600">解题步骤：</span>
                <MathText text={displaySolution} />
              </p>
            ))}
          <AttachmentThumbnails attachments={displayAttachments} />
        </div>
      )}

      {isEvaluationPending && (
        <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs leading-6 text-amber-900">
          作答已经保存，但评价尚未完成。可以再次提交同一份作答重试评价，也可以修改后作为订正提交；旧记录都会保留。
        </p>
      )}

      {needsSupplement && (
        <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-2 text-xs leading-6 text-amber-900">
          评价认为解题过程还需要补充。参考答案暂未展示；可在下方继续补充步骤后重新提交，原作答会保留。
        </p>
      )}

      {isSubmitted && !correcting && (
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={startCorrection}
            disabled={busy}
            className="quiet-button"
          >
            再订正一次
          </button>
        </div>
      )}

      {showForm && (
        <form onSubmit={handleSubmit} className="mt-4 space-y-3">
          {hasRevealed && (
            <p className="rounded-lg bg-amber-50 p-2 text-xs text-amber-800 border border-amber-200">
              你已查看参考解答，仍可对照思路写出你的答案并提交评价（提交后将标注为“已看解答后作答”）。
            </p>
          )}
          <div>
            <label
              htmlFor={`ans-${exercise.id}`}
              className="block text-xs font-medium text-slate-600"
            >
              最终答案
            </label>
            <input
              id={`ans-${exercise.id}`}
              ref={answerRef}
              type="text"
              value={answerInput}
              onChange={(e) => {
                setAnswerInput(e.target.value);
                onDraftChange?.({ answer: e.target.value, solution: solutionInput });
              }}
              onPaste={handlePaste}
              placeholder="请输入最终答案，例如：x = 2 或 1/2"
              disabled={busy}
              className="field-control"
            />
          </div>

          <div>
            <div className="flex flex-wrap items-center justify-between gap-2">
              <label
                htmlFor={`sol-${exercise.id}`}
                className="block text-xs font-medium text-slate-600"
              >
                解题步骤 / 推导思路（选填）
              </label>
              <button
                type="button"
                onClick={() => fileInputRef.current?.click()}
                disabled={busy || imagesBlocked || activeDraftImages.length >= 4}
                className="quiet-button text-xs"
              >
                ＋ 上传过程图
              </button>
              <input
                ref={fileInputRef}
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp"
                multiple
                className="sr-only"
                aria-label="选择过程图"
                onChange={handleFileChange}
              />
            </div>
            <textarea
              id={`sol-${exercise.id}`}
              rows={3}
              value={solutionInput}
              onChange={(e) => {
                setSolutionInput(e.target.value);
                onDraftChange?.({ answer: answerInput, solution: e.target.value });
              }}
              onPaste={handlePaste}
              placeholder="简要写出步骤，或直接粘贴/上传手写过程图"
              disabled={busy}
              className="field-control"
            />
          </div>

          {imagesBlocked && (
            <p className="text-xs text-amber-800">
              今日图片解析额度已用完，仍可提交文字作答。
            </p>
          )}
          {activeImageNotice && (
            <p role="alert" className="text-xs text-rose-700">
              {activeImageNotice}
            </p>
          )}

          {activeDraftImages.length > 0 && (
            <div className="attachment-thumb-list">
              {activeDraftImages.map((item, idx) => (
                <div key={item.localId} className="draft-thumb-card">
                  <button
                    type="button"
                    className="attachment-thumb-card"
                    onClick={() => {
                      const src = item.uploadedId
                        ? attachmentUrl(exercise.conversation_id, item.uploadedId)
                        : item.previewUrl;
                      if (src) setPreviewUrl(src);
                    }}
                    aria-label={`预览待提交过程图 ${idx + 1}`}
                  >
                    {item.previewUrl ? (
                      <img
                        src={item.previewUrl}
                        alt={item.file.name || `过程图 ${idx + 1}`}
                        className="attachment-thumb-img"
                      />
                    ) : (
                      <span className="attachment-thumb-tag">
                        {item.file.name}
                      </span>
                    )}
                    <span className="attachment-thumb-tag">
                      {item.status === "uploading"
                        ? "上传中…"
                        : item.status === "error"
                          ? "上传失败"
                          : "过程图"}
                    </span>
                  </button>
                  <button
                    type="button"
                    aria-label={`移除过程图 ${idx + 1}`}
                    className="draft-thumb-remove"
                    disabled={busy}
                    onClick={() => removeDraftImage(item.localId)}
                  >
                    ×
                  </button>
                  {item.status === "error" && (
                    <div className="mt-1 flex items-center gap-1 text-xs text-rose-700">
                      <span>{item.error || "上传失败"}</span>
                      {onRetryImage && (
                        <button
                          type="button"
                          className="underline"
                          onClick={() => onRetryImage(exercise.id, item.localId)}
                        >
                          重试
                        </button>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </div>
          )}

          {solutionImageNames.length > 0 && (
            <p className="text-xs text-slate-500">
              本次提交将附上解法图：{solutionImageNames.join("、")}
            </p>
          )}
          <div className="flex flex-wrap items-center justify-between gap-2 pt-1">
            <button
              type="submit"
              disabled={
                busy ||
                hasUploadingImage ||
                hasErrorImage ||
                (!answerInput.trim() &&
                  !solutionInput.trim() &&
                  !readyAttachments.length &&
                  !solutionImageNames.length)
              }
              className="primary-button"
            >
              {hasUploadingImage ? "图片上传中..." : submitLabel}
            </button>

            <div className="flex items-center gap-3">
              {correcting && (
                <button
                  type="button"
                  onClick={() => {
                    setCorrecting(false);
                    setAnswerInput(baseAnswer || "");
                    setSolutionInput(baseSolution || "");
                    onDraftChange?.({
                      answer: baseAnswer || "",
                      solution: baseSolution || "",
                    });
                  }}
                  disabled={busy}
                  className="text-xs text-slate-500 underline decoration-slate-300 hover:text-indigo-600 disabled:opacity-40"
                >
                  取消订正
                </button>
              )}
              {!hasRevealed && onRevealSolution && (
                <button
                  type="button"
                  onClick={handleReveal}
                  disabled={busy || revealing}
                  className="text-xs text-slate-500 underline decoration-slate-300 hover:text-indigo-600 disabled:opacity-40"
                  title="查看后作答将明确标记为已看解答"
                >
                  {revealing ? "获取中..." : "不会做？查看参考解答"}
                </button>
              )}
            </div>
          </div>
        </form>
      )}

      {exercise.reference_solution_text && (
        <div className="lesson-answer">
          <div className="flex items-center justify-between">
            <h4 className="text-xs font-semibold text-emerald-900">
              参考解答与推导
            </h4>
            {latestAfterReveal && (
              <span className="text-xs text-amber-700">
                （参考解答已在本次作答前展示）
              </span>
            )}
          </div>
          <p className="mt-2 whitespace-pre-wrap break-words text-xs leading-6 text-emerald-950">
            <MathText text={exercise.reference_solution_text} />
          </p>
        </div>
      )}

      {displayEvaluation && (
        <div className="lesson-disclosure">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <div className="flex items-center gap-2">
              <span className="text-xs font-medium text-slate-700">
                作答评价
              </span>
              {displayAttempt && (
                <span className="text-xs text-slate-500">
                  第 {displayAttempt.attempt_no} 次 ·{" "}
                  {KIND_LABELS[displayAttempt.kind] ?? displayAttempt.kind}
                </span>
              )}
            </div>
            {(onSaveMistake || (mistakeRecord && onOpenRecord)) && (
              <div>
                {mistakeRecord ? (
                  <button
                    type="button"
                    onClick={() => onOpenRecord?.(mistakeRecord.id)}
                    className="text-xs font-medium text-emerald-700 underline decoration-emerald-300 hover:text-emerald-900"
                  >
                    已加入 · 查看
                  </button>
                ) : (
                  <button
                    type="button"
                    disabled={busy || savingMistake}
                    onClick={() => onSaveMistake?.(exercise)}
                    className="quiet-button text-xs"
                  >
                    {savingMistake ? "保存中..." : "加入错题本"}
                  </button>
                )}
              </div>
            )}
          </div>
          <EvaluationBlock
            evaluation={displayEvaluation}
            answeredAfterRevealed={displayAfterReveal}
            studentAnswerText={displayAnswer}
            hasProcess={Boolean(
              displaySolution?.trim() ||
                displayAttachments.some(
                  (a) => a.role === "student_solution" || a.role === "solution",
                ),
            )}
          />
        </div>
      )}

      {historyAttempts.length > 0 && (
        <details className="lesson-disclosure">
          <summary className="cursor-pointer text-xs font-medium text-slate-600">
            更早的作答与评价（{historyAttempts.length} 次）
          </summary>
          <ol className="mt-3 space-y-3">
            {historyAttempts.map((attempt) => (
              <li
                key={attempt.id}
                className="border-t border-slate-200 pt-2"
              >
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs font-medium text-slate-700">
                    第 {attempt.attempt_no} 次 ·{" "}
                    {KIND_LABELS[attempt.kind] ?? attempt.kind}
                  </span>
                  <span className="text-xs text-slate-400">
                    {attempt.answered_after_revealed
                      ? "已看解答后作答"
                      : "独立作答"}
                  </span>
                </div>
                {attempt.student_answer_text && (
                  <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
                    <span className="font-semibold text-slate-600">最终答案：</span>
                    <MathText text={attempt.student_answer_text} />
                  </p>
                )}
                {attempt.student_solution_text && (
                  <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-900">
                    <span className="font-semibold text-slate-600">解题步骤：</span>
                    <MathText text={attempt.student_solution_text} />
                  </p>
                )}
                <AttachmentThumbnails attachments={attempt.attachments} />
                {attempt.evaluation ? (
                  <EvaluationBlock
                    evaluation={attempt.evaluation}
                    answeredAfterRevealed={attempt.answered_after_revealed}
                    studentAnswerText={attempt.student_answer_text}
                    hasProcess={Boolean(
                      attempt.student_solution_text?.trim() ||
                        attempt.attachments.some(
                          (a) => a.role === "student_solution" || a.role === "solution",
                        ),
                    )}
                  />
                ) : (
                  <p className="mt-2 text-xs text-amber-800">
                    这次评价没有完成；可重试评价，旧作答不会丢失。
                  </p>
                )}
              </li>
            ))}
          </ol>
        </details>
      )}
      {previewUrl && (
        <ImagePreviewModal
          src={previewUrl}
          alt="待提交过程图预览"
          onClose={() => setPreviewUrl(null)}
        />
      )}
    </div>
  );
}
