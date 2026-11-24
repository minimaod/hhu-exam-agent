import { createPortal } from "react-dom";
import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type UIEvent,
} from "react";
import ConversationList from "./ConversationList";
import ConversationMaterialPanel from "./ConversationMaterialPanel";
import ConversationTurn from "./ConversationTurn";
import { type ExerciseDraftImage } from "./ExerciseCard";
import ReviewWorkbench from "./ReviewWorkbench";
import ReviewWorkbenchAsk from "./ReviewWorkbenchAsk";
import ReviewResultCard from "./ReviewResultCard";
import { clearReviewHash, reviewHash, reviewKcIndex, reviewProgressKey, type ReviewPosition } from "../lib/reviewWorkbench";
import MaterialLibraryModal from "./MaterialLibraryModal";
import TurnComposer, { type PdfUploadStatus, type PendingFile } from "./TurnComposer";
import UsageNotice from "./UsageNotice";
import {
  attachConversationMaterial,
  checkMaterialReadability,
  deleteConversation,
  detachConversationMaterial,
  generateConversationMaterialArtifact,
  getArtifact,
  getConversationDeleteImpact,
  getConversationMaterialProgress,
  looksLikeExam,
  MaterialsApiError,
  MAX_OCR_PAGES_PER_REQUEST,
  recognizeMaterialPages,
  uploadConversationMaterial,
  type ArtifactDetailView,
  type ArtifactInputMode,
  type ArtifactSummary,
  type ConversationDeleteImpactView,
  type MaterialProgressItem,
  type OutputKind,
} from "../lib/materialsApi";
import {
  ConversationsApiError,
  createConversation,
  createRequestId,
  getConversation,
  listConversations,
  regenerateTurnReply,
  revealExerciseSolution,
  stopConversationMaterialTurn,
  streamTurn,
  turnBusinessStatus,
  type AttachmentInput,
  type AttachmentRole,
  type WorkbenchSource,
  type ConversationDetail,
  type ConversationTurn as StoredTurn,
  type ConversationSummary,
  type ExerciseAttemptView,
  type ExerciseView,
  type ProblemView,
  type QuestionCard,
  type RecordRef,
  type Source,
  type SuggestionItem,
  type TurnKind,
  type TurnProgressEvent,
  type TurnStage,
} from "../lib/conversationsApi";
import {
  createMistakeRecord,
  MistakesApiError,
} from "../lib/mistakesApi";
import {
  uploadAttachment,
  validateLocalImage,
  AttachmentsApiError,
} from "../lib/attachmentsApi";
import type { PaperItem } from "../lib/papersApi";
import {
  classifySuggestion,
  type DownloadStates,
} from "../lib/display";
import {
  fetchUsage,
  millisecondsUntilReset,
  quotaBlocksImages,
  quotaBlocksRequests,
  type UsageSnapshot,
} from "../lib/usageApi";

type ListState =
  | { phase: "loading" }
  | { phase: "ready"; items: ConversationSummary[] }
  | { phase: "error"; message: string };

const CATALOG_FOCUS_ACTIONS = new Set(["introduce", "query_papers"]);
const TUTORING_FOCUS_ACTIONS = new Set([
  "select_question", "explain_question", "view_question_answer", "explain_problem",
  "tutoring", "followup_problem", "generate_exercise", "evaluate_exercise", "reveal_solution",
]);

type DetailState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "ready"; detail: ConversationDetail }
  | { phase: "error"; message: string };

type LiveState = {
  requestId: string;
  stage: "starting" | TurnStage;
  message: string;
};

/**
 * 输入区的显式意图：只由历史卡片上的“纠正/补充/新题”等明确对象操作设置，
 * 不再以输入模式按钮暴露给普通聊天（F008）。
 */
type ComposerMode = "auto" | "supplement" | "correction" | "new";

/** 正在流式生成的正文；interrupted 表示断流/停止后未保存的临时文本。 */
type StreamDraft = {
  conversationId: string;
  requestId: string;
  turn: StoredTurn;
  text: string;
  interrupted: boolean;
  paperQueryActive?: boolean;
};

type ConversationWorkspaceProps = {
  downloads: DownloadStates;
  onDownload: (paper: PaperItem) => void;
  onOpenRecord: (recordId: string) => void;
  /** 从学习记忆跳回某次会话；只切到该会话，不复制任何内容。 */
  openConversationId?: string | null;
  onConversationOpened?: (id: string) => void;
  /** 按用户隔离的上次会话恢复键；测试可省略。 */
  storageKey?: string | null;
  rail?: { history: HTMLElement | null; create: HTMLElement | null; usage: HTMLElement | null };
  onNavigateToConversation?: () => void;
  onCountChange?: (count: number) => void;
  onLearningChanged?: () => void;
  prefillDraft?: {
    conversationId: string;
    text: string;
    materialGuideId?: string;
    materialItemId?: string;
    materialArtifactId?: string;
    materialQuestionId?: string;
    materialKcId?: string;
    materialStudyMode?: "explain" | "exercise" | "evaluate";
  } | null;
  onPrefillConsumed?: () => void;
};

const STORAGE_KEY = "hhu-exam-review.current-conversation";
const MAX_FILES = 4;
const MAX_TOTAL_BYTES = 20 * 1024 * 1024;
const MATERIAL_PROGRESS_POLL_MS = 1_200;
const MATERIAL_PROGRESS_MAX_POLL_MS = 30_000;

function readStoredId(key: string): string | null {
  try {
    return window.localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeStoredId(key: string, id: string): void {
  try {
    window.localStorage.setItem(key, id);
  } catch {
    // 隐私模式等情况下无法持久化，只保留本次页面内的状态。
  }
}

function clearStoredId(key: string): void {
  try {
    window.localStorage.removeItem(key);
  } catch {
    // 同上。
  }
}

function toMessage(error: unknown): string {
  if (
    error instanceof ConversationsApiError ||
    error instanceof AttachmentsApiError ||
    error instanceof MaterialsApiError
  ) {
    return error.message;
  }
  return "读取本地会话时发生未知错误，请重试。";
}

function delay(ms: number): Promise<void> {
  return new Promise((resolve) => {
    window.setTimeout(resolve, ms);
  });
}

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function sourceFromPaper(paperId: string, papers: PaperItem[]): Source {
  if (!paperId) {
    return { type: "none" };
  }
  const paper = papers.find((item) => item.id === paperId);
  if (!paper) {
    return { type: "none" };
  }
  return {
    type: "registered_paper",
    paper_id: paper.id,
    paper_title: paper.title,
  };
}

/**
 * 会话工作区：会话列表、历史、统一输入、上传、停止和记录操作。
 *
 * 历史以服务端 SQLite 为准；localStorage 只记住当前会话 ID。切换会话时中止
 * 本地流，并用序号和 conversation_id 丢弃迟到响应。
 */
export default function ConversationWorkspace({
  downloads,
  onDownload,
  onOpenRecord,
  openConversationId = null,
  onConversationOpened,
  storageKey = STORAGE_KEY,
  rail,
  onNavigateToConversation,
  onCountChange,
  onLearningChanged,
  prefillDraft = null,
  onPrefillConsumed,
}: ConversationWorkspaceProps) {
  const activeStorageKey = storageKey ?? STORAGE_KEY;
  const [listState, setListState] = useState<ListState>({ phase: "loading" });
  const [listAttempt, setListAttempt] = useState(0);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>({ phase: "idle" });
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [draft, setDraft] = useState("");
  const [live, setLive] = useState<LiveState | null>(null);
  const [notice, setWorkspaceNotice] = useState<{ text: string; canRefresh: boolean } | null>(null);
  const setNotice = useCallback((text: string | null, canRefresh = false) => {
    setWorkspaceNotice(text ? { text, canRefresh } : null);
  }, []);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);
  const [usage, setUsage] = useState<UsageSnapshot | null>(null);
  const [usageError, setUsageError] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  // F008：普通自然语言输入不再有“追问/补充/纠正/新题”模式按钮；这里的
  // mode 只由历史卡片上的明确对象操作（纠正/补充/新题）设置。
  const [mode, setMode] = useState<ComposerMode>("auto");
  const [streamDraft, setStreamDraft] = useState<StreamDraft | null>(null);
  const [regeneratingReplyId, setRegeneratingReplyId] = useState<string | null>(null);
  const [files, setFiles] = useState<PendingFile[]>([]);
  const [sourcePaperId, setSourcePaperId] = useState("");
  const [targetProblem, setTargetProblem] = useState<ProblemView | null>(null);
  const [materialStudyTarget, setMaterialStudyTarget] = useState<{
    guideId?: string;
    itemId?: string;
    artifactId?: string;
    questionId?: string;
    kcId?: string;
    mode?: "explain" | "exercise" | "evaluate";
  } | null>(null);
  const [pdfUploadsByConv, setPdfUploadsByConv] = useState<
    Record<string, PdfUploadStatus & { file?: File; isExam?: boolean; pageCount?: number; displayName?: string }>
  >({});
  const currentPdfUpload = currentId ? pdfUploadsByConv[currentId] ?? null : null;
  const uploadingPdf = currentPdfUpload?.phase === "uploading";
  const [recognizingMaterialByConv, setRecognizingMaterialByConv] = useState<Record<string, string | null>>({});
  const [checkingReadabilityId, setCheckingReadabilityId] = useState<string | null>(null);
  const [generatingMaterialByConv, setGeneratingMaterialByConv] = useState<Record<string, string | null>>({});
  const [materialProgressByConv, setMaterialProgressByConv] = useState<Record<string, MaterialProgressItem[]>>({});
  const [materialTurnByConv, setMaterialTurnByConv] = useState<Record<string, string>>({});
  const [stoppingMaterialRequestId, setStoppingMaterialRequestId] = useState<string | null>(null);
  const [optimisticProgressByConv, setOptimisticProgressByConv] = useState<Record<string, MaterialProgressItem[]>>({});
  const progressPollStopRef = useRef<Map<string, () => void>>(new Map());
  const optimisticTasksRef = useRef<Map<string, Set<string>>>(new Map());
  const chatAbortByConvRef = useRef<Map<string, AbortController>>(new Map());
  const chatMaterialActiveByConvRef = useRef<Map<string, string>>(new Map());
  const activeMaterialTurnId = currentId
    ? materialTurnByConv[currentId] ?? chatMaterialActiveByConvRef.current.get(currentId)
    : undefined;
  const recognizingMaterialId = currentId ? recognizingMaterialByConv[currentId] ?? null : null;
  const generatingMaterialKey = currentId ? generatingMaterialByConv[currentId] ?? null : null;
  const [readerModal, setReaderModal] = useState<{
    artifact: ArtifactDetailView;
    displayName: string;
    pageCount: number;
    initial?: ReviewPosition;
    openSequence: number;
    requestIds: string[];
  } | null>(null);
  const readerLoadSequence = useRef(0);
  const [updatedArtifacts, setUpdatedArtifacts] = useState<Record<string, ArtifactDetailView>>({});
  const initialReview = useRef(reviewHash());
  const closeWorkbench = useCallback(() => {
    readerLoadSequence.current += 1;
    clearReviewHash();
    setReaderModal(null);
  }, []);
  const [retryingReaderOutput, setRetryingReaderOutput] = useState<OutputKind | null>(null);
  const [libraryOpen, setLibraryOpen] = useState(false);
  const [deleteConfirm, setDeleteConfirm] = useState<{
    impact: ConversationDeleteImpactView;
    deleting: boolean;
    error: string | null;
  } | null>(null);
  const [exerciseDrafts, setExerciseDrafts] = useState<Record<string, Record<string, { answer: string; solution: string }>>>({});
  const [exerciseDraftImages, setExerciseDraftImages] = useState<
    Record<string, Record<string, ExerciseDraftImage[]>>
  >({});
  const exerciseDraftImagesRef = useRef(exerciseDraftImages);
  exerciseDraftImagesRef.current = exerciseDraftImages;
  const [exerciseImageNotices, setExerciseImageNotices] = useState<
    Record<string, Record<string, string | null>>
  >({});
  const [savingMistakeRootId, setSavingMistakeRootId] = useState<string | null>(null);
  const [recordsByRootOverride, setRecordsByRootOverride] = useState<
    Record<string, Record<string, RecordRef>>
  >({});
  const [exerciseFocusTokens, setExerciseFocusTokens] = useState<Record<string, number>>({});
  const [exerciseCorrectionTokens, setExerciseCorrectionTokens] = useState<Record<string, number>>({});

  useEffect(() => {
    return () => {
      for (const perConv of Object.values(exerciseDraftImagesRef.current)) {
        for (const items of Object.values(perConv)) {
          for (const item of items) {
            URL.revokeObjectURL(item.previewUrl);
          }
        }
      }
    };
  }, []);
  const composerSnapshot = {
    draft,
    sourcePaperId,
    mode,
    files,
    targetProblem,
    materialStudyTarget,
  };
  const composerRef = useRef(composerSnapshot);
  composerRef.current = composerSnapshot;
  const composerDraftsRef = useRef(new Map<string, typeof composerSnapshot>());

  const currentIdRef = useRef<string | null>(null);
  const liveRef = useRef<LiveState | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const streamSeqRef = useRef(0);
  const detailRequestRef = useRef(0);
  const filesRef = useRef<PendingFile[]>([]);
  const submittingRef = useRef(false);
  const regeneratingReplyRef = useRef(false);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const stickToBottomRef = useRef(true);

  const updateLive = useCallback((value: LiveState | null) => {
    liveRef.current = value;
    setLive(value);
  }, []);

  const setPendingFiles = useCallback((updater: (current: PendingFile[]) => PendingFile[]) => {
    setFiles((current) => {
      const next = updater(current);
      filesRef.current = next;
      return next;
    });
  }, []);

  const isCurrentOperation = useCallback(
    (conversationId: string, sequence: number) =>
      currentIdRef.current === conversationId &&
      streamSeqRef.current === sequence,
    [],
  );

  const scrollToBottom = useCallback((behavior: "auto" | "smooth" = "auto") => {
    const container = scrollRef.current;
    if (!container) {
      return;
    }
    if (typeof container.scrollTo === "function") {
      container.scrollTo({ top: container.scrollHeight, behavior });
    } else {
      container.scrollTop = container.scrollHeight;
    }
  }, []);

  const handleMessageScroll = useCallback((event: UIEvent<HTMLDivElement>) => {
    const container = event.currentTarget;
    stickToBottomRef.current =
      container.scrollHeight - container.scrollTop - container.clientHeight < 120;
  }, []);

  // 启动时读取会话列表；只有列表确认存在时才恢复上次打开的会话。
  useEffect(() => {
    const controller = new AbortController();
    setListState({ phase: "loading" });

    listConversations(controller.signal)
      .then((payload) => {
        setListState({ phase: "ready", items: payload.items });

        if (currentIdRef.current !== null) {
          return;
        }
        const target = initialReview.current?.conversationId;
        const stored = target && payload.items.some((item) => item.id === target)
          ? target
          : readStoredId(activeStorageKey);
        if (!stored) {
          return;
        }
        if (payload.items.some((item) => item.id === stored)) {
          currentIdRef.current = stored;
          setCurrentId(stored);
        } else {
          clearStoredId(activeStorageKey);
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setListState({ phase: "error", message: toMessage(error) });
      });

    return () => controller.abort();
  }, [activeStorageKey, listAttempt]);

  const loadDetail = useCallback(
    async (
      conversationId: string,
      options?: { preserveOnError?: boolean; confirmedTurn?: StoredTurn },
    ): Promise<ConversationDetail | null> => {
      if (currentIdRef.current !== conversationId) {
        return null;
      }
      const requestToken = detailRequestRef.current + 1;
      detailRequestRef.current = requestToken;

      try {
        const detail = await getConversation(conversationId);
        if (
          currentIdRef.current === conversationId &&
          detailRequestRef.current === requestToken
        ) {
          const confirmed = options?.confirmedTurn;
          setDetailState({
            phase: "ready",
            detail: confirmed && !detail.turns.some((turn) => turn.request_id === confirmed.request_id)
              ? { ...detail, turns: [...detail.turns, confirmed] }
              : detail,
          });
        }
        return detail;
      } catch (error) {
        if (
          !options?.preserveOnError &&
          currentIdRef.current === conversationId &&
          detailRequestRef.current === requestToken
        ) {
          setDetailState({ phase: "error", message: toMessage(error) });
        }
        return null;
      }
    },
    [],
  );

  useEffect(() => {
    if (!currentId) {
      setDetailState({ phase: "idle" });
      return;
    }
    setDetailState({ phase: "loading" });
    void loadDetail(currentId);
  }, [currentId, detailAttempt, loadDetail]);

  useEffect(
    () => () => {
      abortRef.current?.abort();
      for (const ctrl of chatAbortByConvRef.current.values()) {
        ctrl.abort();
      }
      chatAbortByConvRef.current.clear();
      chatMaterialActiveByConvRef.current.clear();
      filesRef.current.forEach((item) => URL.revokeObjectURL(item.previewUrl));
      composerDraftsRef.current.forEach((saved) => saved.files.forEach((item) => URL.revokeObjectURL(item.previewUrl)));
    },
    [],
  );

  const refreshList = useCallback(async (): Promise<boolean> => {
    try {
      const payload = await listConversations();
      setListState({ phase: "ready", items: payload.items });
      return true;
    } catch {
      // 列表刷新失败不覆盖已有内容；由调用方决定是否提示刷新失败。
      return false;
    }
  }, []);

  const openConversation = useCallback(
    (conversationId: string) => {
      if (currentIdRef.current === conversationId) {
        return;
      }

      const prevConvId = currentIdRef.current;
      if (!prevConvId || !chatMaterialActiveByConvRef.current.get(prevConvId)) {
        abortRef.current?.abort();
      }
      abortRef.current = null;
      streamSeqRef.current += 1;
      submittingRef.current = false;
      setSubmitting(false);
      stickToBottomRef.current = true;
      updateLive(null);
      // 切换会话时旧请求的临时正文必须一起清掉：迟到的片段会因操作序号变化
      // 被忽略，但已经显示出来的半截文字不能留在新会话页面上。
      setStreamDraft(null);
      setNotice(null);
      setErrorNotice(null);
      if (initialReview.current?.conversationId !== conversationId) closeWorkbench();
      setLibraryOpen(false);
      setDeleteConfirm(null);
      if (currentIdRef.current) composerDraftsRef.current.set(currentIdRef.current, composerRef.current);
      const saved = composerDraftsRef.current.get(conversationId);
      setDraft(saved?.draft ?? "");
      setSourcePaperId(saved?.sourcePaperId ?? "");
      setMode(saved?.mode ?? "auto");
      setTargetProblem(saved?.targetProblem ?? null);
      setMaterialStudyTarget(saved?.materialStudyTarget ?? null);
      setPendingFiles(() => saved?.files ?? []);
      currentIdRef.current = conversationId;
      setCurrentId(conversationId);
      setDetailState({ phase: "loading" });
      writeStoredId(activeStorageKey, conversationId);
    },
    [activeStorageKey, closeWorkbench, setPendingFiles, updateLive],
  );

  const handleCreate = useCallback(async () => {
    if (creating) {
      return;
    }
    setCreating(true);
    setNotice(null);

    try {
      const created = await createConversation();
      setListState((current) =>
        current.phase === "ready"
          ? { phase: "ready", items: [created, ...current.items] }
          : { phase: "ready", items: [created] },
      );
      openConversation(created.id);
    } catch (error) {
      setNotice(toMessage(error));
    } finally {
      setCreating(false);
    }
  }, [creating, openConversation]);

  // 从学习记忆跳回某次会话：同一个 id 只切一次，之后仍可自由切换。
  const openedRequestRef = useRef<string | null>(null);
  useEffect(() => {
    if (!openConversationId) {
      openedRequestRef.current = null;
      return;
    }
    if (openedRequestRef.current === openConversationId) {
      return;
    }
    openedRequestRef.current = openConversationId;
    openConversation(openConversationId);
    onConversationOpened?.(openConversationId);
  }, [openConversationId, openConversation, onConversationOpened]);

  useEffect(() => {
    if (!prefillDraft || currentId !== prefillDraft.conversationId) {
      return;
    }
    setDraft(prefillDraft.text);
    if (prefillDraft.materialGuideId && prefillDraft.materialItemId) {
      setMaterialStudyTarget({
        guideId: prefillDraft.materialGuideId,
        itemId: prefillDraft.materialItemId,
        mode: prefillDraft.materialStudyMode,
      });
    } else if (prefillDraft.materialArtifactId && prefillDraft.materialQuestionId) {
      setMaterialStudyTarget({
        artifactId: prefillDraft.materialArtifactId,
        questionId: prefillDraft.materialQuestionId,
        mode: prefillDraft.materialStudyMode,
      });
    } else if (prefillDraft.materialArtifactId && prefillDraft.materialKcId) {
      setMaterialStudyTarget({
        artifactId: prefillDraft.materialArtifactId,
        kcId: prefillDraft.materialKcId,
        mode: prefillDraft.materialStudyMode,
      });
    }
    onPrefillConsumed?.();
  }, [prefillDraft, currentId, onPrefillConsumed]);

  const refreshUsage = useCallback(async (signal?: AbortSignal) => {
    try {
      const snapshot = await fetchUsage(signal);
      setUsage(snapshot);
      setUsageError(null);
    } catch (error) {
      if (isAbortError(error)) return;
      setUsageError(
        error instanceof Error ? error.message : "读取今日用量失败，请稍后重试。",
      );
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void refreshUsage(controller.signal);
    return () => controller.abort();
  }, [refreshUsage]);

  useEffect(() => {
    if (!usage) return;
    const timer = window.setTimeout(
      () => void refreshUsage(),
      Math.min(millisecondsUntilReset(usage.reset_at), 2_147_000_000),
    );
    return () => window.clearTimeout(timer);
  }, [usage, refreshUsage]);

  useEffect(() => {
    const refresh = () => {
      if (document.visibilityState === "visible") {
        void refreshUsage();
      }
    };
    window.addEventListener("focus", refresh);
    document.addEventListener("visibilitychange", refresh);
    return () => {
      window.removeEventListener("focus", refresh);
      document.removeEventListener("visibilitychange", refresh);
    };
  }, [refreshUsage]);

  const sourceOptions = useMemo(() => {
    const map = new Map<string, PaperItem>();
    if (detailState.phase === "ready") {
      for (const turn of detailState.detail.turns) {
        if (turn.result?.kind === "found") {
          for (const paper of turn.result.items) {
            map.set(paper.id, paper);
          }
        }
      }
    }
    return [...map.values()];
  }, [detailState]);

  const recordsByRoot = useMemo(() => {
    const fromServer =
      detailState.phase === "ready" ? (detailState.detail.records_by_root ?? {}) : {};
    const fromLocal = currentId ? (recordsByRootOverride[currentId] ?? {}) : {};
    return { ...fromServer, ...fromLocal };
  }, [detailState, currentId, recordsByRootOverride]);

  const activeProblem =
    detailState.phase === "ready" ? detailState.detail.active_problem : null;
  const turnCount =
    detailState.phase === "ready" ? detailState.detail.turns.length : 0;
  const latestTurnId =
    detailState.phase === "ready"
      ? (detailState.detail.turns[detailState.detail.turns.length - 1]
          ?.request_id ?? "")
      : "";

  useEffect(() => {
    stickToBottomRef.current = true;
  }, [currentId]);

  const addFiles = useCallback(
    (incoming: File[], role: AttachmentRole) => {
      const current = filesRef.current;
      const accepted: PendingFile[] = [];
      let total = current.reduce((sum, item) => sum + item.file.size, 0);
      let noticeText: string | null = null;

      for (const file of incoming) {
        if (current.length + accepted.length >= MAX_FILES) {
          noticeText = "一轮最多 4 张图片。";
          break;
        }
        const localError = validateLocalImage(file);
        if (localError) {
          noticeText = localError;
          continue;
        }
        if (total + file.size > MAX_TOTAL_BYTES) {
          noticeText = "本轮图片合计不能超过 20 MiB。";
          break;
        }
        total += file.size;
        accepted.push({
          file,
          previewUrl: URL.createObjectURL(file),
          role,
        });
      }

      if (accepted.length) {
        setPendingFiles((currentFiles) => [...currentFiles, ...accepted]);
      }
      if (noticeText) {
        setNotice(noticeText);
      }
    },
    [setPendingFiles],
  );

  const uploadExerciseDraftImage = useCallback(
    async (conversationId: string, exerciseId: string, localId: string, file: File) => {
      try {
        const meta = await uploadAttachment(conversationId, file);
        setExerciseDraftImages((current) => {
          const perConv = current[conversationId] ?? {};
          const list = perConv[exerciseId] ?? [];
          if (!list.some((item) => item.localId === localId)) {
            return current;
          }
          return {
            ...current,
            [conversationId]: {
              ...perConv,
              [exerciseId]: list.map((item) =>
                item.localId === localId
                  ? { ...item, status: "ready", uploadedId: meta.id, error: undefined }
                  : item,
              ),
            },
          };
        });
      } catch (error) {
        const msg =
          error instanceof AttachmentsApiError ? error.message : toMessage(error);
        if (
          error instanceof AttachmentsApiError &&
          (error.status === 429 || error.quota !== null)
        ) {
          void refreshUsage();
        }
        setExerciseDraftImages((current) => {
          const perConv = current[conversationId] ?? {};
          const list = perConv[exerciseId] ?? [];
          if (!list.some((item) => item.localId === localId)) {
            return current;
          }
          return {
            ...current,
            [conversationId]: {
              ...perConv,
              [exerciseId]: list.map((item) =>
                item.localId === localId
                  ? { ...item, status: "error", error: msg }
                  : item,
              ),
            },
          };
        });
        setExerciseImageNotices((current) => ({
          ...current,
          [conversationId]: {
            ...(current[conversationId] ?? {}),
            [exerciseId]: msg,
          },
        }));
      }
    },
    [refreshUsage],
  );

  const addExerciseImages = useCallback(
    (exerciseId: string, incoming: File[]) => {
      const conversationId = currentIdRef.current;
      if (!conversationId || incoming.length === 0) {
        return;
      }
      if (quotaBlocksImages(usage)) {
        setExerciseImageNotices((current) => ({
          ...current,
          [conversationId]: {
            ...(current[conversationId] ?? {}),
            [exerciseId]: "今日图片额度已用完，请明日再试。",
          },
        }));
        return;
      }
      const existing = exerciseDraftImagesRef.current[conversationId]?.[exerciseId] ?? [];
      let totalBytes = existing.reduce((sum, item) => sum + item.file.size, 0);
      const accepted: ExerciseDraftImage[] = [];
      let noticeText: string | null = null;

      for (const file of incoming) {
        if (existing.length + accepted.length >= MAX_FILES) {
          noticeText = "一题最多上传 4 张过程图。";
          break;
        }
        const localError = validateLocalImage(file);
        if (localError) {
          noticeText = localError;
          continue;
        }
        if (totalBytes + file.size > MAX_TOTAL_BYTES) {
          noticeText = "本轮图片合计不能超过 20 MiB。";
          break;
        }
        totalBytes += file.size;
        accepted.push({
          localId: createRequestId(),
          file,
          previewUrl: URL.createObjectURL(file),
          status: "uploading",
        });
      }

      setExerciseImageNotices((current) => ({
        ...current,
        [conversationId]: {
          ...(current[conversationId] ?? {}),
          [exerciseId]: noticeText,
        },
      }));

      if (accepted.length === 0) {
        return;
      }

      setExerciseDraftImages((current) => {
        const perConv = current[conversationId] ?? {};
        const prevList = perConv[exerciseId] ?? [];
        return {
          ...current,
          [conversationId]: {
            ...perConv,
            [exerciseId]: [...prevList, ...accepted],
          },
        };
      });

      for (const item of accepted) {
        void uploadExerciseDraftImage(conversationId, exerciseId, item.localId, item.file);
      }
    },
    [usage, uploadExerciseDraftImage],
  );

  const removeExerciseImage = useCallback((exerciseId: string, localId: string) => {
    const conversationId = currentIdRef.current;
    if (!conversationId) return;
    setExerciseDraftImages((current) => {
      const perConv = current[conversationId] ?? {};
      const list = perConv[exerciseId] ?? [];
      const target = list.find((item) => item.localId === localId);
      if (target) {
        URL.revokeObjectURL(target.previewUrl);
      }
      return {
        ...current,
        [conversationId]: {
          ...perConv,
          [exerciseId]: list.filter((item) => item.localId !== localId),
        },
      };
    });
    setExerciseImageNotices((current) => ({
      ...current,
      [conversationId]: {
        ...(current[conversationId] ?? {}),
        [exerciseId]: null,
      },
    }));
  }, []);

  const retryExerciseImage = useCallback(
    (exerciseId: string, localId: string) => {
      const conversationId = currentIdRef.current;
      if (!conversationId) return;
      const list = exerciseDraftImagesRef.current[conversationId]?.[exerciseId] ?? [];
      const target = list.find((item) => item.localId === localId);
      if (!target) return;
      setExerciseImageNotices((current) => ({
        ...current,
        [conversationId]: {
          ...(current[conversationId] ?? {}),
          [exerciseId]: null,
        },
      }));
      setExerciseDraftImages((current) => {
        const perConv = current[conversationId] ?? {};
        const prevList = perConv[exerciseId] ?? [];
        return {
          ...current,
          [conversationId]: {
            ...perConv,
            [exerciseId]: prevList.map((item) =>
              item.localId === localId
                ? { ...item, status: "uploading", error: undefined }
                : item,
            ),
          },
        };
      });
      void uploadExerciseDraftImage(conversationId, exerciseId, localId, target.file);
    },
    [uploadExerciseDraftImage],
  );

  const removeFile = useCallback(
    (index: number) => {
      setPendingFiles((current) => {
        const item = current[index];
        if (item) {
          URL.revokeObjectURL(item.previewUrl);
        }
        return current.filter((_, itemIndex) => itemIndex !== index);
      });
    },
    [setPendingFiles],
  );

  const submit = useCallback(
    async (
      kindOverride?: TurnKind | "auto",
      textOverride?: string,
      options: {
        problem?: ProblemView;
        files?: PendingFile[];
        uploadedAttachments?: AttachmentInput[];
        startNew?: boolean;
        studentAnswer?: string;
        solutionText?: string;
        sourcePaperId?: string;
        exerciseId?: string;
        retryOf?: string;
        attemptKind?: "supplement" | "correction";
        questionId?: string;
        questionSourceTurn?: string;
        questionAction?: "exercise" | "record";
        workbenchSource?: WorkbenchSource;
        onRequestStarted?: (requestId: string) => void;
      } = {},
    ) => {
      if (submittingRef.current || regeneratingReplyRef.current) {
        return;
      }
      const conversationId = currentIdRef.current;
      if (!conversationId) {
        return;
      }
      // 题卡选中、查看答案、保存错题等操作后端明确不调用模型；额度耗尽时
      // 仍应可用。普通聊天和生成/评价练习继续受额度限制。
      const modelFree =
        options.questionAction === "record" ||
        kindOverride === "select_question" ||
        kindOverride === "view_question_answer" ||
        kindOverride === "record" ||
        kindOverride === "feedback" ||
        kindOverride === "reveal_solution";
      if (!modelFree && quotaBlocksRequests(usage)) {
        return;
      }

      const text = (textOverride ?? draft).trim();
      const uploadedAttachments = options.uploadedAttachments ?? [];
      const pending = uploadedAttachments.length > 0
        ? []
        : (options.files ?? (kindOverride ? [] : filesRef.current));
      const isQuestionTurn = kindOverride === "select_question"
        || kindOverride === "explain_question"
        || kindOverride === "view_question_answer";
      const hasQuestionTarget = Boolean(options.questionId && options.questionSourceTurn);
      if (!text && pending.length === 0 && uploadedAttachments.length === 0 && !options.retryOf &&
          !(isQuestionTurn && hasQuestionTarget) &&
          !(options.questionAction && hasQuestionTarget) &&
          !(kindOverride === "exercise" && (options.studentAnswer?.trim() || options.solutionText?.trim() || uploadedAttachments.length > 0))) {
        return;
      }

      let kind: TurnKind | "auto" = kindOverride ?? "auto";
      if (!kindOverride) {
        if (mode === "new") {
          kind = "problem";
        } else if (mode === "supplement") {
          kind = "supplement";
        } else if (mode === "correction") {
          kind = "correction";
        } else {
          kind = "auto";
        }
      }
      const explicitFields = kind === "problem" || kind === "supplement" || kind === "correction";
      const submittedAnswer = options.studentAnswer ?? "";
      const submittedSolution = options.solutionText ?? "";
      const currentDetail = detailState.phase === "ready" ? detailState.detail : null;
      const hasAnswerTarget = Boolean(activeProblem || currentDetail?.active_question?.selected);
      const answerImage = kind === "auto" && hasAnswerTarget &&
        !/(?:新题|另一道题|换题|这是题干|题目截图)/.test(text) &&
        /(?:我的|我写的|我做的|我算的)(?:答案|解法|解题过程|过程)|帮我(?:核对|批改)这(?:题|道题)/.test(text);

      const requestId = createRequestId();
      options.onRequestStarted?.(requestId);
      const sequence = streamSeqRef.current + 1;
      streamSeqRef.current = sequence;
      submittingRef.current = true;
      setSubmitting(true);

      const controller = new AbortController();
      abortRef.current = controller;
      chatAbortByConvRef.current.set(conversationId, controller);
      setNotice(null);
      setErrorNotice(null);
      stickToBottomRef.current = true;
      scrollToBottom("auto");
      let paperQueryActive = false;
      let triggeredMaterialProgress = false;
      updateLive({
        requestId,
        stage: "starting",
        message: pending.length ? "正在上传图片…" : "正在提交…",
      });
      const currentTurns = currentDetail?.turns ?? [];
      const provisionalTurn: StoredTurn = {
        request_id: requestId,
        seq: (currentTurns[currentTurns.length - 1]?.seq ?? 0) + 1,
        kind: kind === "auto" ? "dynamic" : kind,
        query: text || (pending.length ? "发送图片" : ""),
        workbench_source: options.workbenchSource,
        status: "running",
        result: null,
        tutoring: null,
        error: null,
        started_at: new Date().toISOString(),
        finished_at: null,
      };
      setStreamDraft({ conversationId, requestId, turn: provisionalTurn, text: "", interrupted: false });

      try {
        const uploaded: AttachmentInput[] = [...uploadedAttachments];
        for (const item of pending) {
          const meta = await uploadAttachment(conversationId, item.file, controller.signal);
          if (!isCurrentOperation(conversationId, sequence)) {
            return;
          }
          uploaded.push({ id: meta.id, role: item.role === "statement" && answerImage ? "solution" : item.role });
        }

        const source =
          kind === "problem" || kind === "supplement" || kind === "correction"
            ? sourceFromPaper(
                options.sourcePaperId ?? (!kindOverride ? sourcePaperId : ""),
                sourceOptions,
              )
            : { type: "none" as const };

        const result = await streamTurn({
          conversationId,
          requestId,
          query: text,
          kind,
          attachments: uploaded,
          studentAnswer: submittedAnswer,
          solutionText: submittedSolution,
          source,
          problemId:
            kind === "correction"
              ? options.problem?.id ?? targetProblem?.id ?? activeProblem?.id
              : kind === "supplement" || kind === "record" || kind === "feedback"
                ? options.problem?.id ?? activeProblem?.id
                : undefined,
          exerciseId: options.exerciseId,
          retryOf: options.retryOf,
          attemptKind: options.attemptKind,
          questionId: options.questionId,
          questionSourceTurn: options.questionSourceTurn,
          questionAction: options.questionAction,
          materialGuideId: !kindOverride ? materialStudyTarget?.guideId : undefined,
          materialItemId: !kindOverride ? materialStudyTarget?.itemId : undefined,
          materialArtifactId: options.workbenchSource?.artifact_id ?? (!kindOverride ? materialStudyTarget?.artifactId : undefined),
          materialQuestionId: options.workbenchSource?.question_id ?? (!kindOverride ? materialStudyTarget?.questionId : undefined),
          materialKcId: options.workbenchSource?.kc_id ?? (!kindOverride ? materialStudyTarget?.kcId : undefined),
          materialStudyMode: !kindOverride ? materialStudyTarget?.mode : undefined,
          workbenchView: options.workbenchSource?.view,
          materialSourcePages: options.workbenchSource?.pages,
          startNew: options.startNew ?? (!kindOverride && kind === "problem" && mode === "new"),
          signal: controller.signal,
          onProgress: (event: TurnProgressEvent) => {
            if (event.stage === "generating_material") {
              triggeredMaterialProgress = true;
              chatMaterialActiveByConvRef.current.set(conversationId, requestId);
              addOptimisticProgress(conversationId, {
                task_id: `chat:${requestId}`,
                conversation_id: conversationId,
                material_id: "",
                material_name: "当前会话资料",
                kind: "generate",
                start_page: 1,
                end_page: 1,
                outputs: [],
                stage: "generating_material",
                stage_label: event.message || "生成复习成果",
                ocr_done: 0,
                ocr_total: 0,
                ocr_failed_pages: [],
                updated_at: new Date().toISOString(),
              });
              startProgressPolling(conversationId);
              void syncConversationProgress(conversationId);
            }
            if (!isCurrentOperation(conversationId, sequence)) {
              return;
            }
            if (event.stage === "retrieving") {
              paperQueryActive = true;
            }
            updateLive({
              requestId,
              stage: event.stage,
              message: event.message,
            });
          },
          onMessageDelta: (event) => {
            // 正文片段只属于当前会话与当前请求；迟到或他人的片段直接丢弃，
            // 且不触碰输入框和附件。
            if (!isCurrentOperation(conversationId, sequence)) {
              return;
            }
            setStreamDraft((current) => {
              if (current && current.requestId !== requestId) {
                return current;
              }
              return current && {
                ...current,
                text: current.text + event.delta,
                paperQueryActive,
              };
            });
          },
        });

        if (!isCurrentOperation(conversationId, sequence)) {
          if (triggeredMaterialProgress && currentIdRef.current === conversationId) {
            void loadDetail(conversationId);
            void refreshList();
          }
          return;
        }

        const confirmedTurn: StoredTurn = {
          ...provisionalTurn,
          ...result,
          status: "completed",
          finished_at: new Date().toISOString(),
        };
        setDetailState((current) => current.phase === "ready" && current.detail.conversation.id === conversationId
          ? { phase: "ready", detail: {
            ...current.detail,
            turns: [...current.detail.turns.filter((turn) => turn.request_id !== requestId), confirmedTurn],
          } }
          : current);
        setStreamDraft(null);
        updateLive(null);
        setNotice(null);
        const detail = await loadDetail(conversationId, { preserveOnError: true, confirmedTurn });
        if (!isCurrentOperation(conversationId, sequence)) {
          if (triggeredMaterialProgress && currentIdRef.current === conversationId) {
            void loadDetail(conversationId);
            void refreshList();
          }
          return;
        }
        void refreshList();
        onLearningChanged?.();
        if (
          result.tutoring?.kind === "record_exists" &&
          result.tutoring.needs_update
        ) {
          setNotice("这道题已有旧版本记录，可在错题本中更新或重新讲解。");
        }
        if (!detail) {
          setNotice("这一步已完成并保存，但读取会话记录失败，请稍后刷新。", true);
        }
        const stored = detail?.turns.find((turn) => turn.request_id === requestId);
        const outcome = stored ? turnBusinessStatus(stored) : result.business_status;
        if (outcome === "failed" || outcome === "partial") {
          const actions = stored?.composite_results ?? result.composite_results ?? [];
          const isExamReply = actions.some((item) => item.action === "solve_uploaded_exam");
          if (!isExamReply || !result.assistant_message) {
            setErrorNotice(result.assistant_message ?? "本轮业务尚未完成，草稿已保留，请查看结果并按提示恢复。");
          }
        } else {
          if (options.exerciseId) {
            const targetExerciseId = options.exerciseId;
            setExerciseDrafts((current) => {
              const forConversation = { ...(current[conversationId] ?? {}) };
              delete forConversation[targetExerciseId];
              return { ...current, [conversationId]: forConversation };
            });
            setExerciseDraftImages((current) => {
              const perConv = { ...(current[conversationId] ?? {}) };
              const items = perConv[targetExerciseId] ?? [];
              for (const item of items) {
                URL.revokeObjectURL(item.previewUrl);
              }
              delete perConv[targetExerciseId];
              return { ...current, [conversationId]: perConv };
            });
            setExerciseImageNotices((current) => {
              const perConv = { ...(current[conversationId] ?? {}) };
              delete perConv[targetExerciseId];
              return { ...current, [conversationId]: perConv };
            });
          }
          if (!kindOverride) {
            // 清理本次实际提交的字段；卡片、快捷操作和重试不会清空主输入区。
            setDraft((current) => current.trim() === text ? "" : current);
            setMaterialStudyTarget(null);
            if (explicitFields) {
              setSourcePaperId(""); setTargetProblem(null); setMode("auto");
            }
          }
          if (pending.length > 0) {
            setPendingFiles((current) => current.filter((item) => {
              if (!pending.includes(item)) return true;
              URL.revokeObjectURL(item.previewUrl); return false;
            }));
          }
        }
      } catch (error) {
        if (!isCurrentOperation(conversationId, sequence)) {
          if (triggeredMaterialProgress && currentIdRef.current === conversationId) {
            void loadDetail(conversationId);
            void refreshList();
          }
          return;
        }
        const interruptedDraft = (state: StreamDraft | null): StreamDraft | null =>
          state && state.requestId === requestId ? { ...state, interrupted: true } : state;
        if (isAbortError(error)) {
          // 用户停止或断流：临时正文不能显示成完整成功回复。
          setStreamDraft((current) =>
            current && current.requestId === requestId
              ? { ...current, interrupted: true }
              : null,
          );
          return;
        }

        updateLive(null);
        setStreamDraft(interruptedDraft);
        setErrorNotice(toMessage(error));
        if (
          error instanceof ConversationsApiError &&
          (error.code === "MODEL_QUOTA_EXCEEDED" ||
            error.code === "QUOTA_STORAGE_UNAVAILABLE" ||
            error.code === "IMAGE_QUOTA_EXCEEDED")
        ) {
          void refreshUsage();
          if (error.saved) {
            onLearningChanged?.();
            const savedDetail = await loadDetail(conversationId);
            if (!isCurrentOperation(conversationId, sequence)) {
              return;
            }
            if (savedDetail) {
              setNotice("本轮已保存的业务结果已载入；额度恢复后可继续。");
            }
          }
        }

        const uncertain =
          !(error instanceof ConversationsApiError) ||
          error.status === undefined ||
          error.code === "STREAM_INCOMPLETE" ||
          error.code === "QUERY_FAILED";
        const shouldReload =
          error instanceof ConversationsApiError &&
          (error.status === 409 ||
            error.code === "STORAGE_UNAVAILABLE" ||
            uncertain);

        if (shouldReload) {
          const detail = await loadDetail(conversationId);
          if (!isCurrentOperation(conversationId, sequence)) {
            return;
          }
          const stored = detail?.turns.find((turn) => turn.request_id === requestId);
          if (stored && stored.status !== "running" && turnBusinessStatus(stored) === "succeeded") {
            setErrorNotice(null);
            setNotice("服务端已完成这一轮；已恢复记录，输入草稿保留。");
          } else if (uncertain && !stored) {
            setErrorNotice(
              "连接提前结束，服务端还没有这一轮的记录。请刷新会话后再决定是否重新提交。",
            );
          }
        }
      } finally {
        if (chatAbortByConvRef.current.get(conversationId) === controller) {
          chatAbortByConvRef.current.delete(conversationId);
        }
        if (chatMaterialActiveByConvRef.current.get(conversationId) === requestId) {
          chatMaterialActiveByConvRef.current.delete(conversationId);
        }
        if (triggeredMaterialProgress) {
          finishProgressTask(conversationId, `chat:${requestId}`);
        }
        if (streamSeqRef.current === sequence) {
          submittingRef.current = false;
          setSubmitting(false);
          abortRef.current = null;
        }
        void refreshUsage();
      }
    },
    [
      activeProblem,
      detailState,
      draft,
      isCurrentOperation,
      loadDetail,
      materialStudyTarget,
      mode,
      onLearningChanged,
      refreshList,
      refreshUsage,
      setPendingFiles,
      sourceOptions,
      sourcePaperId,
        scrollToBottom,
      targetProblem,
      updateLive,
      usage,
    ],
  );

  const stopCurrentStream = useCallback(() => {
    const active = liveRef.current;
    const conversationId = currentIdRef.current;
    if (!active) {
      if (conversationId) {
        const bgController = chatAbortByConvRef.current.get(conversationId);
        if (bgController) {
          bgController.abort();
          chatAbortByConvRef.current.delete(conversationId);
          chatMaterialActiveByConvRef.current.delete(conversationId);
        }
      }
      return;
    }

    streamSeqRef.current += 1;
    const sequence = streamSeqRef.current;
    abortRef.current?.abort();
    abortRef.current = null;
    if (conversationId) {
      chatAbortByConvRef.current.get(conversationId)?.abort();
      chatAbortByConvRef.current.delete(conversationId);
      chatMaterialActiveByConvRef.current.delete(conversationId);
    }
    updateLive(null);
    setStreamDraft((current) =>
      current && current.requestId === active.requestId
        ? { ...current, interrupted: true }
        : null,
    );
    setSubmitting(false);
    submittingRef.current = false;
    setNotice("已停止，正在确认服务端状态…");

    if (!conversationId) {
      return;
    }

    void (async () => {
      for (let attempt = 0; attempt < 3; attempt += 1) {
        if (!isCurrentOperation(conversationId, sequence)) {
          return;
        }
        const detail = await loadDetail(conversationId);
        if (!isCurrentOperation(conversationId, sequence)) {
          return;
        }
        const stored = detail?.turns.find((turn) => turn.request_id === active.requestId);
        if (stored && stored.status !== "running") {
          setNotice(null);
          return;
        }
        await delay(300);
      }
      if (isCurrentOperation(conversationId, sequence)) {
        setNotice("已停止。若会话记录仍显示进行中，请稍后刷新确认。", true);
      }
    })();
  }, [isCurrentOperation, loadDetail, updateLive]);

  const handleStop = useCallback(() => {
    const conversationId = currentIdRef.current;
    const active = liveRef.current;
    if (!conversationId || !activeMaterialTurnId || (active && active.requestId !== activeMaterialTurnId)) {
      stopCurrentStream();
      return;
    }
    if (stoppingMaterialRequestId === activeMaterialTurnId) return;
    setStoppingMaterialRequestId(activeMaterialTurnId);
    void stopConversationMaterialTurn(conversationId, activeMaterialTurnId)
      .then(() => {
        if (currentIdRef.current !== conversationId) return;
        if (liveRef.current?.requestId === activeMaterialTurnId) {
          stopCurrentStream();
        } else {
          void loadDetail(conversationId, { preserveOnError: true });
        }
      })
      .catch((error: unknown) => {
        if (currentIdRef.current === conversationId) setErrorNotice(toMessage(error));
      })
      .finally(() => {
        setStoppingMaterialRequestId((current) => current === activeMaterialTurnId ? null : current);
      });
  }, [activeMaterialTurnId, loadDetail, stopCurrentStream, stoppingMaterialRequestId]);

  const handleFeedback = useCallback(
    (problem: ProblemView) => {
      void submit("feedback", "我懂了", { problem });
    },
    [submit],
  );

  const handleRecord = useCallback(
    (problem: ProblemView) => {
      void submit("record", "记录这题", { problem });
    },
    [submit],
  );

  const handleCorrect = useCallback((problem: ProblemView) => {
    setTargetProblem(problem);
    setMode("correction");
    setDraft(problem.statement_text);
    setNotice("发送后将生成新版题干和讲解，历史讲解会标记失效。");
  }, []);

  const handleSupplement = useCallback((_problem: ProblemView) => {
    setTargetProblem(null);
    setMode("supplement");
    setDraft("");
    setNotice("请补充看不清的符号或条件，或换一张更清楚的图片。");
  }, []);

  const handleNew = useCallback((text: string) => {
    setTargetProblem(null);
    setMode("new");
    setDraft(text);
    setNotice("这是新题：发送后会开始一道新题，之前的题目仍保留在历史中。");
  }, []);

  const handleIntentChoice = useCallback(
    (turn: ConversationDetail["turns"][number], option: string) => {
      if (option === "query") {
        void submit("query", turn.query);
        return;
      }
      if (option === "tutoring") {
        void submit("problem", turn.query);
        return;
      }
      if (option === "supplement") {
        setMode("supplement");
        setDraft(turn.query);
        return;
      }
      if (option === "correction") {
        setMode("correction");
        setDraft(turn.query);
      }
    },
    [submit],
  );

  const handleExerciseAnswer = useCallback(
    (
      exerciseId: string,
      answerText: string,
      solutionText?: string,
      attemptKind?: "supplement" | "correction",
      uploadedAttachments?: AttachmentInput[],
    ) => {
      void submit("exercise", answerText, {
        exerciseId,
        studentAnswer: answerText,
        solutionText,
        attemptKind,
        uploadedAttachments,
      });
    },
    [submit],
  );

  const handleSaveExerciseMistake = useCallback(
    async (exercise: ExerciseView) => {
      const conversationId = currentIdRef.current;
      if (!conversationId) return;
      const rootKey = exercise.root_problem_id || exercise.problem_id || exercise.id;
      if (savingMistakeRootId === rootKey) {
        return;
      }
      setSavingMistakeRootId(rootKey);
      setErrorNotice(null);
      try {
        const record = await createMistakeRecord({
          requestId: createRequestId(),
          conversationId,
          problemId: exercise.problem_id || undefined,
        });
        setRecordsByRootOverride((prev) => ({
          ...prev,
          [conversationId]: {
            ...(prev[conversationId] ?? {}),
            [rootKey]: {
              id: record.id,
              problem_version: record.problem_version,
            },
          },
        }));
        onLearningChanged?.();
      } catch (error) {
        setErrorNotice(
          error instanceof MistakesApiError ? error.message : toMessage(error),
        );
      } finally {
        setSavingMistakeRootId((current) => (current === rootKey ? null : current));
      }
    },
    [onLearningChanged, savingMistakeRootId],
  );

  const handleExerciseReveal = useCallback(
    async (exerciseId: string) => {
      const conversationId = currentIdRef.current;
      if (!conversationId) return;
      const sequence = streamSeqRef.current;
      try {
        const revealed = await revealExerciseSolution(conversationId, exerciseId);
        if (!isCurrentOperation(conversationId, sequence)) return;
        setDetailState((prev) => {
          if (prev.phase !== "ready" || prev.detail.conversation.id !== conversationId) {
            return prev;
          }
          const updatedTurns = prev.detail.turns.map((turn) => {
            if (turn.exercise?.id === exerciseId) {
              return { ...turn, exercise: revealed };
            }
            return turn;
          });
          return {
            ...prev,
            detail: {
              ...prev.detail,
              turns: updatedTurns,
              active_exercise:
                prev.detail.active_exercise?.id === exerciseId
                  ? revealed
                  : prev.detail.active_exercise,
            },
          };
        });
        await loadDetail(conversationId);
      } catch (err) {
        if (isCurrentOperation(conversationId, sequence)) {
          setErrorNotice(err instanceof Error ? err.message : "查看参考解答失败");
        }
      }
    },
    [isCurrentOperation, loadDetail],
  );

  const scrollToExercise = useCallback((exerciseId: string) => {
    const element = document.getElementById(`exercise-card-${exerciseId}`);
    element?.scrollIntoView?.({ behavior: "smooth", block: "center" });
  }, []);

  const handleJumpToExercise = useCallback(
    (exerciseId: string) => {
      scrollToExercise(exerciseId);
    },
    [scrollToExercise],
  );

  const handleCorrectExercise = useCallback(
    (exerciseId: string) => {
      setExerciseCorrectionTokens((current) => ({
        ...current,
        [exerciseId]: (current[exerciseId] ?? 0) + 1,
      }));
      scrollToExercise(exerciseId);
    },
    [scrollToExercise],
  );

  /**
   * 建议按钮绑定到“这一轮”的具体对象：看解答直接揭晓这道练习，
   * 提交答案只聚焦本题作答框，保存错题只保存本题；其余仍按自然语言发送。
   */
  const handleSuggestionClick = useCallback(
    (turn: ConversationDetail["turns"][number], suggestion: SuggestionItem) => {
      const intent = classifySuggestion(suggestion.label);
      const exercise = turn.exercise ?? null;
      if (intent === "reveal" && exercise) {
        void handleExerciseReveal(exercise.id);
        return;
      }
      if (intent === "answer" && exercise) {
        setExerciseFocusTokens((current) => ({
          ...current,
          [exercise.id]: (current[exercise.id] ?? 0) + 1,
        }));
        scrollToExercise(exercise.id);
        return;
      }
      if (intent === "save" && turn.tutoring?.kind === "explanation") {
        handleRecord(turn.tutoring.problem);
        return;
      }
      void submit("auto", suggestion.label);
    },
    [handleExerciseReveal, handleRecord, scrollToExercise, submit],
  );

  const handleQuestionAction = useCallback(
    (
      action: "select" | "explain" | "view" | "exercise" | "record" | "answer",
      card: QuestionCard,
      answer?: string,
    ) => {
      if (action === "exercise" || action === "record") {
        if (!card.source_request_id) {
          setErrorNotice("这道题缺少查题轮次信息，请重新查题后再操作。");
          return;
        }
        void submit(
          "auto",
          action === "exercise" ? "为这道真题生成一道变式" : "把这道真题保存到错题本",
          {
            questionId: card.question_id,
            questionSourceTurn: card.source_request_id,
            questionAction: action,
          },
        );
        return;
      }
      if (!card.source_request_id) {
        setErrorNotice("这道题缺少查题轮次信息，请重新查题后再操作。");
        return;
      }
      if (action === "answer") {
        const trimmed = answer?.trim() ?? "";
        if (!trimmed) return;
        if (card.options.length > 0) {
          if (!card.options.some((option) => option.label === trimmed)) return;
          void submit("explain_question", `这道题我选 ${trimmed}，请核对并讲解。`, {
            questionId: card.question_id,
            questionSourceTurn: card.source_request_id,
            studentAnswer: trimmed,
          });
          return;
        }
        void submit("explain_question", `我的答案是：${trimmed}，请核对并讲解。`, {
          questionId: card.question_id,
          questionSourceTurn: card.source_request_id,
          studentAnswer: trimmed,
        });
        return;
      }
      const kind =
        action === "select"
          ? "select_question"
          : action === "explain"
            ? "explain_question"
            : "view_question_answer";
      void submit(kind, "", {
        questionId: card.question_id,
        questionSourceTurn: card.source_request_id,
      }).then(() => {
        if (action !== "view") return;
        // Explicitly asking to view the answer opens the existing main card.
        // Later lecture/history turns still keep it collapsed by default.
        window.setTimeout(() => {
          const key = `${card.question_id}:${card.build_id}:${card.content_sha256}`;
          const answer = document.getElementById(`question-${key}`)?.querySelector<HTMLDetailsElement>("details[data-question-answer]");
          if (answer) {
            answer.open = true;
            answer.scrollIntoView?.({ behavior: "smooth", block: "center" });
          }
        }, 0);
      });
    },
    [submit],
  );

  const handleRetry = useCallback(
    (turn: ConversationDetail["turns"][number]) => {
      void submit("auto", "", { retryOf: turn.request_id });
    },
    [submit],
  );

  const handleRegenerateReply = useCallback(
    async (turn: ConversationDetail["turns"][number]) => {
      const target = currentIdRef.current;
      if (!target || submittingRef.current || regeneratingReplyRef.current || quotaBlocksRequests(usage)) {
        return;
      }
      regeneratingReplyRef.current = true;
      setRegeneratingReplyId(turn.request_id);
      setErrorNotice(null);
      try {
        const reply = await regenerateTurnReply(target, turn.request_id);
        if (currentIdRef.current !== target) {
          return;
        }
        setNotice(
          reply.reply_status === "complete"
            ? "回复已重新生成。"
            : "这次仍不完整，可以稍后再试。",
        );
        await loadDetail(target);
      } catch (error) {
        if (currentIdRef.current !== target) {
          return;
        }
        setErrorNotice(toMessage(error));
      } finally {
        void refreshUsage();
        regeneratingReplyRef.current = false;
        setRegeneratingReplyId(null);
      }
    },
    [loadDetail, refreshUsage, usage],
  );

  const handleUploadPdf = useCallback(
    async (file: File) => {
      const targetConvId = currentIdRef.current;
      if (!targetConvId || pdfUploadsByConv[targetConvId]?.phase === "uploading") {
        return;
      }
      setErrorNotice(null);
      setNotice(null);
      setPdfUploadsByConv((prev) => ({
        ...prev,
        [targetConvId]: {
          phase: "uploading",
          fileName: file.name,
          file,
          message: `正在上传并检查《${file.name}》…`,
        },
      }));
      let uploaded: Awaited<ReturnType<typeof uploadConversationMaterial>>;
      try {
        uploaded = await uploadConversationMaterial(targetConvId, file);
      } catch (error) {
        setPdfUploadsByConv((prev) => ({
          ...prev,
          [targetConvId]: {
            phase: "error",
            fileName: file.name,
            file,
            canRetry: true,
            message: `上传《${file.name}》失败：${toMessage(error)}`,
          },
        }));
        return;
      }

      const displayName = uploaded.item.material.display_name || file.name;
      const isExam = Boolean(
        uploaded.item.material.is_exam ?? looksLikeExam(displayName),
      );
      const pageCount = uploaded.item.material.page_count;

      const successMessage = isExam
        ? `已添加《${displayName}》，可以问我某道题`
        : uploaded.item.readability && !uploaded.item.readability.sufficient
          ? uploaded.item.readability.summary || uploaded.assistant_notice || `已添加《${displayName}》（文字不足，可预览原件或识别扫描页）`
          : `已添加《${displayName}》（${pageCount} 页），可按页检查文字或生成笔记、测试与图谱`;

      // 用户在上传期间已切到其他会话：绝不调用 loadDetail(targetConvId) 推进全局请求序号，
      // 避免新会话 B 的详情请求被误判为过期而卡在加载中。
      if (currentIdRef.current !== targetConvId) {
        setPdfUploadsByConv((prev) => ({
          ...prev,
          [targetConvId]: {
            phase: "succeeded",
            fileName: displayName,
            isExam,
            pageCount,
            displayName,
            message: successMessage,
          },
        }));
        void refreshList();
        return;
      }

      // 先用上传响应乐观合并已挂接的资料，确保即便刷新详情失败也能立即看到资料卡。
      setDetailState((prev) => {
        if (prev.phase !== "ready" || prev.detail.conversation.id !== targetConvId) {
          return prev;
        }
        const existing = prev.detail.materials ?? [];
        if (existing.some((entry) => entry.material.id === uploaded.item.material.id)) {
          return prev;
        }
        return {
          phase: "ready",
          detail: {
            ...prev.detail,
            materials: [uploaded.item, ...existing],
          },
        };
      });

      const [detailRes, listOk] = await Promise.all([
        loadDetail(targetConvId, { preserveOnError: true }),
        refreshList(),
      ]);

      if (currentIdRef.current !== targetConvId) {
        setPdfUploadsByConv((prev) => ({
          ...prev,
          [targetConvId]: {
            phase: "succeeded",
            fileName: displayName,
            isExam,
            pageCount,
            displayName,
            message: successMessage,
          },
        }));
        return;
      }

      if (detailRes === null || !listOk) {
        setPdfUploadsByConv((prev) => ({
          ...prev,
          [targetConvId]: {
            phase: "refresh_failed",
            fileName: displayName,
            isExam,
            pageCount,
            displayName,
            canRetry: true,
            message: `《${displayName}》已上传，刷新列表失败，请点击重试刷新。`,
          },
        }));
        return;
      }

      setPdfUploadsByConv((prev) => ({
        ...prev,
        [targetConvId]: {
          phase: "succeeded",
          fileName: displayName,
          isExam,
          pageCount,
          displayName,
          message: successMessage,
        },
      }));
    },
    [loadDetail, pdfUploadsByConv, refreshList],
  );

  const handleRetryPdfUpload = useCallback(async () => {
    const targetConvId = currentIdRef.current;
    if (!targetConvId) return;
    const entry = pdfUploadsByConv[targetConvId];
    if (!entry) return;
    if (entry.phase === "refresh_failed") {
      setPdfUploadsByConv((prev) => ({
        ...prev,
        [targetConvId]: { ...entry, phase: "uploading", canRetry: false, message: `正在刷新《${entry.fileName}》…` },
      }));
      const [detailRes, listOk] = await Promise.all([
        loadDetail(targetConvId, { preserveOnError: true }),
        refreshList(),
      ]);
      if (detailRes === null || !listOk) {
        setPdfUploadsByConv((prev) => ({
          ...prev,
          [targetConvId]: {
            ...entry,
            phase: "refresh_failed",
            canRetry: true,
            message: `《${entry.fileName}》已上传，刷新列表失败，请点击重试刷新。`,
          },
        }));
        return;
      }
      const msg = entry.isExam
        ? `已添加《${entry.fileName}》，可以问我某道题`
        : `已添加《${entry.fileName}》（${entry.pageCount ?? ""} 页）`;
      setPdfUploadsByConv((prev) => ({
        ...prev,
        [targetConvId]: { phase: "succeeded", fileName: entry.fileName, message: msg },
      }));
      return;
    }
    if (entry.phase === "error" && entry.file) {
      await handleUploadPdf(entry.file);
    }
  }, [handleUploadPdf, loadDetail, pdfUploadsByConv, refreshList]);

  const handleDismissPdfUpload = useCallback(() => {
    const targetConvId = currentIdRef.current;
    if (!targetConvId) return;
    setPdfUploadsByConv((prev) => {
      if (!(targetConvId in prev)) return prev;
      const next = { ...prev };
      delete next[targetConvId];
      return next;
    });
  }, []);

  const addOptimisticProgress = useCallback((conversationId: string, item: MaterialProgressItem) => {
    let set = optimisticTasksRef.current.get(conversationId);
    if (!set) {
      set = new Set();
      optimisticTasksRef.current.set(conversationId, set);
    }
    set.add(item.task_id);
    setOptimisticProgressByConv((prev) => ({
      ...prev,
      [conversationId]: [
        ...(prev[conversationId] ?? []).filter((existing) => existing.task_id !== item.task_id),
        item,
      ],
    }));
  }, []);

  const removeOptimisticProgress = useCallback((conversationId: string, taskId: string) => {
    const set = optimisticTasksRef.current.get(conversationId);
    if (set) {
      set.delete(taskId);
      if (set.size === 0) {
        optimisticTasksRef.current.delete(conversationId);
      }
    }
    setOptimisticProgressByConv((prev) => {
      const list = prev[conversationId];
      if (!list) return prev;
      const filtered = list.filter((item) => item.task_id !== taskId);
      if (filtered.length === 0) {
        const next = { ...prev };
        delete next[conversationId];
        return next;
      }
      return { ...prev, [conversationId]: filtered };
    });
  }, []);

  const stopProgressPolling = useCallback((conversationId: string) => {
    const stop = progressPollStopRef.current.get(conversationId);
    if (stop) {
      progressPollStopRef.current.delete(conversationId);
      stop();
    }
  }, []);

  const syncConversationProgress = useCallback(
    async (
      conversationId: string,
    ): Promise<{ ok: true; active: boolean } | { ok: false }> => {
      try {
        const res = await getConversationMaterialProgress(conversationId);
        const { items, chat_turn_request_id: requestId } = res;
        setMaterialTurnByConv((prev) => {
          if (requestId) {
            return prev[conversationId] === requestId ? prev : { ...prev, [conversationId]: requestId };
          }
          if (!(conversationId in prev)) return prev;
          const next = { ...prev };
          delete next[conversationId];
          return next;
        });
        setMaterialProgressByConv((prev) => {
          if (items.length === 0) {
            if (!(conversationId in prev)) return prev;
            const next = { ...prev };
            delete next[conversationId];
            return next;
          }
          return { ...prev, [conversationId]: items };
        });
        return { ok: true, active: items.length > 0 || requestId !== null };
      } catch {
        return { ok: false };
      }
    },
    [],
  );

  const finishProgressTask = useCallback(
    (conversationId: string, taskId: string) => {
      removeOptimisticProgress(conversationId, taskId);
      const remainingLocal = optimisticTasksRef.current.get(conversationId)?.size ?? 0;
      if (remainingLocal > 0) {
        return;
      }
      void syncConversationProgress(conversationId).then((res) => {
        const stillLocal = optimisticTasksRef.current.get(conversationId)?.size ?? 0;
        if (stillLocal === 0 && res.ok && !res.active) {
          stopProgressPolling(conversationId);
        }
      });
    },
    [removeOptimisticProgress, stopProgressPolling, syncConversationProgress],
  );

  const startProgressPolling = useCallback(
    (conversationId: string, options?: { reloadOnEmpty?: boolean; initialActive?: boolean }) => {
      if (progressPollStopRef.current.has(conversationId)) {
        return;
      }
      let cancelled = false;
      let timerId: number | null = null;
      let seenActive = Boolean(options?.initialActive);
      let nextDelay = MATERIAL_PROGRESS_POLL_MS;

      const tick = async () => {
        if (cancelled) return;
        const res = await syncConversationProgress(conversationId);
        if (cancelled) return;
        if (res.ok) {
          nextDelay = MATERIAL_PROGRESS_POLL_MS;
          if (res.active) {
            seenActive = true;
          } else if (!optimisticTasksRef.current.has(conversationId)) {
            stopProgressPolling(conversationId);
            if (options?.reloadOnEmpty && seenActive && currentIdRef.current === conversationId) {
              void loadDetail(conversationId);
            }
            return;
          }
        } else {
          nextDelay = Math.min(nextDelay * 2, MATERIAL_PROGRESS_MAX_POLL_MS);
        }
        timerId = window.setTimeout(() => {
          void tick();
        }, nextDelay);
      };

      progressPollStopRef.current.set(conversationId, () => {
        cancelled = true;
        if (timerId !== null) {
          window.clearTimeout(timerId);
        }
      });

      timerId = window.setTimeout(() => {
        void tick();
      }, 250);
    },
    [loadDetail, stopProgressPolling, syncConversationProgress],
  );

  useEffect(() => {
    const polls = progressPollStopRef.current;
    return () => {
      for (const stop of polls.values()) {
        stop();
      }
      polls.clear();
    };
  }, []);

  const hasRunningTurn = detailState.phase === "ready" &&
    detailState.detail.turns.some((turn) => turn.status === "running");
  useEffect(() => {
    if (!currentId || detailState.phase !== "ready") return;
    startProgressPolling(currentId, {
      reloadOnEmpty: true,
      initialActive: hasRunningTurn,
    });
  }, [currentId, detailState.phase, hasRunningTurn, startProgressPolling]);

  const resolveMaterialMeta = useCallback(
    (materialId: string) => {
      if (detailState.phase === "ready") {
        const found = (detailState.detail.materials ?? []).find((entry) => entry.material.id === materialId);
        if (found) {
          return {
            name: found.material.display_name,
            pageCount: found.material.page_count,
          };
        }
      }
      return { name: "个人资料", pageCount: 40 };
    },
    [detailState],
  );

  const handleRecognizeMaterial = useCallback(
    async (materialId: string, pages: number[]) => {
      const conversationId = currentIdRef.current;
      if (!conversationId || recognizingMaterialByConv[conversationId]) return;

      const sortedPages = [...pages].sort((a, b) => a - b);
      const meta = resolveMaterialMeta(materialId);
      const optimisticId = `local-ocr:${materialId}`;
      setRecognizingMaterialByConv((prev) => ({ ...prev, [conversationId]: materialId }));
      setNotice(null);
      setErrorNotice(null);
      addOptimisticProgress(conversationId, {
        task_id: optimisticId,
        conversation_id: conversationId,
        material_id: materialId,
        material_name: meta.name,
        kind: "ocr",
        start_page: sortedPages[0] ?? 1,
        end_page: sortedPages[sortedPages.length - 1] ?? 1,
        outputs: [],
        stage: "ocr",
        stage_label: "识别扫描页",
        ocr_done: 0,
        ocr_total: sortedPages.length,
        ocr_failed_pages: [],
        updated_at: new Date().toISOString(),
      });
      startProgressPolling(conversationId);
      try {
        const recognized: number[] = [];
        const excluded: number[] = [];
        const failures: string[] = [];
        for (let index = 0; index < sortedPages.length; index += MAX_OCR_PAGES_PER_REQUEST) {
          if (currentIdRef.current !== conversationId) return;
          const batch = sortedPages.slice(index, index + MAX_OCR_PAGES_PER_REQUEST);
          const result = await recognizeMaterialPages(materialId, batch, conversationId);
          recognized.push(...result.recognized_pages);
          excluded.push(...(result.excluded_pages ?? []));
          failures.push(...result.failures.map(({ page_no, message }) => "第 " + page_no + " 页：" + message));
          if (currentIdRef.current !== conversationId) return;
          await loadDetail(conversationId);
        }
        if (currentIdRef.current !== conversationId) return;
        const notices: string[] = [];
        if (recognized.length > 0) notices.push("已识别第 " + recognized.join("、") + " 页，可查看识别文字并对照原页。");
        if (excluded.length > 0) notices.push("第 " + excluded.join("、") + " 页无需复习。");
        if (notices.length > 0) setNotice(notices.join(" "));
        if (failures.length > 0) setErrorNotice(failures.join(" "));
      } catch (error) {
        if (currentIdRef.current === conversationId) {
          setErrorNotice(toMessage(error));
          await loadDetail(conversationId);
        }
      } finally {
        finishProgressTask(conversationId, optimisticId);
        setRecognizingMaterialByConv((prev) => ({ ...prev, [conversationId]: null }));
        void refreshUsage();
      }
    },
    [
      addOptimisticProgress,
      finishProgressTask,
      loadDetail,
      recognizingMaterialByConv,
      refreshUsage,
      resolveMaterialMeta,
      startProgressPolling,
    ],
  );

  const handleCheckMaterialReadability = useCallback(
    async (materialId: string, startPage: number, endPage: number) => {
      const conversationId = currentIdRef.current;
      if (!conversationId || checkingReadabilityId) return;
      setCheckingReadabilityId(materialId);
      setNotice(null);
      setErrorNotice(null);
      try {
        const report = await checkMaterialReadability(materialId, startPage, endPage);
        if (currentIdRef.current !== conversationId) return;
        await loadDetail(conversationId);
        if (currentIdRef.current !== conversationId) return;
        setNotice(report.reason);
      } catch (error) {
        if (currentIdRef.current === conversationId) {
          setErrorNotice(toMessage(error));
        }
      } finally {
        setCheckingReadabilityId(null);
      }
    },
    [checkingReadabilityId, loadDetail],
  );

  const handleGenerateMaterial = useCallback(
    async (
      materialId: string,
      outputs: OutputKind[],
      startPage?: number,
      endPage?: number,
      courseHint?: string,
      explicitConvId?: string,
      coverageMode?: ArtifactInputMode,
    ) => {
      const targetConvId = explicitConvId ?? currentIdRef.current;
      if (!targetConvId || generatingMaterialByConv[targetConvId] || recognizingMaterialByConv[targetConvId]) {
        return;
      }
      if (quotaBlocksRequests(usage)) {
        void refreshUsage();
        setErrorNotice("今日模型请求额度已用完，请明日重置后再生成复习成果。");
        return;
      }
      const key = `${materialId}:${outputs.join(",")}`;
      const meta = resolveMaterialMeta(materialId);
      const effectiveStart = startPage ?? 1;
      const effectiveEnd = endPage ?? Math.min(meta.pageCount, 40);
      const optimisticId = `local-gen:${key}`;

      setGeneratingMaterialByConv((prev) => ({ ...prev, [targetConvId]: key }));
      setErrorNotice(null);
      setNotice(null);
      addOptimisticProgress(targetConvId, {
        task_id: optimisticId,
        conversation_id: targetConvId,
        material_id: materialId,
        material_name: meta.name,
        kind: "generate",
        start_page: effectiveStart,
        end_page: effectiveEnd,
        outputs,
        stage: "checking_text",
        stage_label: "检查文字",
        ocr_done: 0,
        ocr_total: 0,
        ocr_failed_pages: [],
        updated_at: new Date().toISOString(),
      });
      startProgressPolling(targetConvId);
      try {
        const res = await generateConversationMaterialArtifact(targetConvId, materialId, {
          outputs,
          startPage,
          endPage,
          courseHint,
          coverageMode,
        });
        let refreshedDetail = false;
        if (currentIdRef.current === targetConvId) {
          const [detail] = await Promise.all([
            loadDetail(targetConvId, { preserveOnError: true }), refreshList(),
          ]);
          refreshedDetail = Boolean(detail);
        } else {
          void refreshList();
        }
        void refreshUsage();
        if (currentIdRef.current !== targetConvId) {
          return;
        }
        setReaderModal((current) =>
          current && current.artifact.id === res.artifact.id
            ? { ...current, artifact: res.artifact }
            : current,
        );
        const failedPart =
          res.artifact.notes_status === "failed"
            ? "章节笔记"
            : res.artifact.quiz_status === "failed"
              ? "章节测试"
              : res.artifact.graph_status === "failed"
                ? "知识图谱"
                : null;
        if (failedPart) {
          setNotice(`${failedPart}生成未成功，已完成部分仍可打开，可随时只重试失败项。`);
        } else if (!refreshedDetail) {
          setNotice("成果已保存。重新读取会话记录后即可打开。", true);
        }
      } catch (error) {
        if (currentIdRef.current === targetConvId) {
          setErrorNotice(toMessage(error));
          await loadDetail(targetConvId);
        }
        void refreshUsage();
      } finally {
        finishProgressTask(targetConvId, optimisticId);
        setGeneratingMaterialByConv((prev) => ({ ...prev, [targetConvId]: null }));
      }
    },
    [
      addOptimisticProgress,
      finishProgressTask,
      generatingMaterialByConv,
      recognizingMaterialByConv,
      loadDetail,
      refreshList,
      refreshUsage,
      resolveMaterialMeta,
      startProgressPolling,
      usage,
    ],
  );

  const handleRetryMessageMaterial = useCallback(
    async (artifactId: string, output: OutputKind) => {
      const targetConvId = currentIdRef.current;
      if (!targetConvId) return;

      try {
        const artifact = await getArtifact(artifactId);
        await handleGenerateMaterial(
          artifact.material_id,
          [output],
          artifact.start_page,
          artifact.end_page,
          artifact.course_hint,
          targetConvId,
        );
      } catch (error) {
        if (currentIdRef.current === targetConvId) {
          setErrorNotice(toMessage(error));
        }
      }
    },
    [handleGenerateMaterial],
  );

  const handleOpenArtifact = useCallback(
    async (artifactId: string, displayName: string, pageCount: number, initial?: ReviewPosition) => {
      const targetConvId = currentIdRef.current;
      if (!targetConvId) return;
      const sequence = ++readerLoadSequence.current;
      setErrorNotice(null);
      try {
        const artifact = await getArtifact(artifactId);
        if (currentIdRef.current !== targetConvId || sequence !== readerLoadSequence.current) return;
        setReaderModal({ artifact, displayName, pageCount, initial, openSequence: sequence, requestIds: [] });
      } catch (error) {
        if (currentIdRef.current === targetConvId && sequence === readerLoadSequence.current) {
          clearReviewHash();
          setErrorNotice(toMessage(error));
        }
      }
    }, [],
  );

  useEffect(() => {
    const target = initialReview.current;
    if (!target || detailState.phase !== "ready" || !currentId) return;
    initialReview.current = null;
    if (target.conversationId !== currentId) {
      clearReviewHash();
      return;
    }
    const entry = (detailState.detail.materials ?? []).find((item) => item.artifacts.some((artifact) => artifact.id === target.artifactId));
    void handleOpenArtifact(target.artifactId, entry?.material.display_name ?? "复习成果",
      entry?.material.page_count ?? 0, { view: target.view, kcId: target.kcId });
  }, [currentId, detailState, handleOpenArtifact]);

  const handleRetryReaderOutput = useCallback(
    async (artifact: ArtifactDetailView, output: OutputKind) => {
      const targetConvId = currentIdRef.current;
      if (!targetConvId || retryingReaderOutput) {
        return;
      }
      if (quotaBlocksRequests(usage)) {
        void refreshUsage();
        setErrorNotice("今日模型请求额度已用完，请明日重置后再重试生成。");
        return;
      }
      const optimisticId = `local-modal:${artifact.id}:${output}`;
      const meta = resolveMaterialMeta(artifact.material_id);
      setRetryingReaderOutput(output);
      setErrorNotice(null);
      addOptimisticProgress(targetConvId, {
        task_id: optimisticId,
        conversation_id: targetConvId,
        material_id: artifact.material_id,
        material_name: meta.name,
        kind: "generate",
        start_page: artifact.start_page,
        end_page: artifact.end_page,
        outputs: [output],
        stage: "checking_text",
        stage_label: "检查文字",
        ocr_done: 0,
        ocr_total: 0,
        ocr_failed_pages: [],
        updated_at: new Date().toISOString(),
      });
      startProgressPolling(targetConvId);
      try {
        const res = await generateConversationMaterialArtifact(targetConvId, artifact.material_id, {
          outputs: [output],
          startPage: artifact.start_page,
          endPage: artifact.end_page,
          courseHint: artifact.course_hint,
        });
        if (currentIdRef.current === targetConvId) {
          setReaderModal((current) =>
            current && current.artifact.id === artifact.id
              ? { ...current, artifact: res.artifact }
              : current,
          );
          await loadDetail(targetConvId);
        }
        void refreshUsage();
      } catch (error) {
        if (currentIdRef.current === targetConvId) {
          setErrorNotice(toMessage(error));
          await loadDetail(targetConvId);
        }
        void refreshUsage();
      } finally {
        finishProgressTask(targetConvId, optimisticId);
        setRetryingReaderOutput(null);
      }
    },
    [
      addOptimisticProgress,
      finishProgressTask,
      loadDetail,
      refreshUsage,
      resolveMaterialMeta,
      retryingReaderOutput,
      startProgressPolling,
      usage,
    ],
  );

  const handleDetachMaterial = useCallback(
    async (materialId: string) => {
      const targetConvId = currentIdRef.current;
      if (!targetConvId) return;
      setErrorNotice(null);
      try {
        await detachConversationMaterial(targetConvId, materialId);
        if (currentIdRef.current !== targetConvId) {
          return;
        }
        setReaderModal((current) =>
          current && current.artifact.material_id === materialId ? null : current,
        );
        await loadDetail(targetConvId);
        setNotice("已从当前会话移出该资料（原 PDF 与已生成成果仍可在「资料与成果」找回）。");
      } catch (error) {
        if (currentIdRef.current === targetConvId) {
          setErrorNotice(toMessage(error));
        }
      }
    },
    [loadDetail],
  );

  const handleAttachFromLibrary = useCallback(
    async (materialId: string) => {
      const targetConvId = currentIdRef.current;
      if (!targetConvId) return;
      await attachConversationMaterial(targetConvId, materialId);
      if (currentIdRef.current !== targetConvId) {
        return;
      }
      await loadDetail(targetConvId);
      setNotice("已将该资料加入当前会话，可在卡片中按需生成或查看已有成果。");
    },
    [loadDetail],
  );

  const handleStartStudyFromMaterial = useCallback(
    (
      conversationId: string,
      prompt: string,
      target: {
        artifactId?: string;
        questionId?: string;
        kcId?: string;
        guideId?: string;
        itemId?: string;
        mode?: "explain" | "exercise" | "evaluate";
      },
    ) => {
      if (currentIdRef.current !== conversationId) {
        openConversation(conversationId);
      }
      setDraft(prompt);
      setMaterialStudyTarget(target);
      setLibraryOpen(false);
      setNotice("已在输入区绑定原资料与原题，发送即可在当前会话继续追问。");
    },
    [openConversation],
  );

  const handleRequestDeleteConversation = useCallback(async (conversationId: string) => {
    setErrorNotice(null);
    try {
      const impact = await getConversationDeleteImpact(conversationId);
      setDeleteConfirm({ impact, deleting: false, error: null });
    } catch (error) {
      setErrorNotice(toMessage(error));
    }
  }, []);

  const handleConfirmDeleteConversation = useCallback(async () => {
    if (!deleteConfirm || deleteConfirm.deleting) {
      return;
    }
    const targetId = deleteConfirm.impact.conversation_id;
    setDeleteConfirm((current) => (current ? { ...current, deleting: true, error: null } : null));
    try {
      await deleteConversation(targetId);
      composerDraftsRef.current.delete(targetId);
      setListState((current) =>
        current.phase === "ready"
          ? { phase: "ready", items: current.items.filter((item) => item.id !== targetId) }
          : current,
      );
      if (currentIdRef.current === targetId) {
        abortRef.current?.abort();
        abortRef.current = null;
        streamSeqRef.current += 1;
        submittingRef.current = false;
        setSubmitting(false);
        updateLive(null);
        setStreamDraft(null);
        closeWorkbench();
        setLibraryOpen(false);
        currentIdRef.current = null;
        setCurrentId(null);
        setDetailState({ phase: "idle" });
        clearStoredId(activeStorageKey);
      }
      setDeleteConfirm(null);
      setNotice("已删除会话；引用的个人 PDF 与已生成成果已保留在个人资料库中。");
      void refreshList();
    } catch (error) {
      setDeleteConfirm((current) =>
        current ? { ...current, deleting: false, error: toMessage(error) } : null,
      );
    }
  }, [activeStorageKey, closeWorkbench, deleteConfirm, refreshList, updateLive]);

  // 错误提示固定在输入区附近；详情读取失败时输入区会卸载，因此也要在
  // 加载/错误分支渲染同一份告警，避免 409 等业务错误随输入区一起消失。
  const renderErrorNotice = () => {
    if (!errorNotice) {
      return null;
    }
    return (
      <div
        role="alert"
        className="inline-alert composer-conflict"
      >
        <p className="whitespace-pre-wrap break-words">{errorNotice}</p>
        <p className="mt-1">草稿已保留。可继续编辑，或切回普通聊天后发送指令。</p>
        <div className="mt-2 flex flex-wrap gap-2">
          <button
            type="button"
            onClick={() => {
              setMode("auto");
              setTargetProblem(null);
            }}
            className="rounded border bg-white px-2 py-1"
          >
            切回普通聊天
          </button>
          <button
            type="button"
            onClick={() => setDetailAttempt((value) => value + 1)}
            className="rounded border bg-white px-2 py-1"
          >
            刷新会话记录
          </button>
          <button
            type="button"
            onClick={() => setErrorNotice(null)}
            className="rounded border bg-white px-2 py-1"
          >
            关闭提示
          </button>
        </div>
      </div>
    );
  };

  const listItems = listState.phase === "ready" ? listState.items : [];
  const listError = listState.phase === "error" ? listState.message : null;
  const visibleConversations = useMemo(
    () =>
      listItems.filter(
        (item) =>
          item.id === currentId ||
          !((item.title === "新会话" || item.title === "新建学习会话") && item.created_at === item.updated_at),
      ),
    [currentId, listItems],
  );
  useEffect(() => {
    if (listState.phase === "ready") onCountChange?.(visibleConversations.length);
  }, [listState.phase, visibleConversations.length, onCountChange]);
  const currentConversation =
    detailState.phase === "ready" ? detailState.detail.conversation : null;
  const canSubmit =
    currentId !== null &&
    detailState.phase === "ready" &&
    !submitting &&
    regeneratingReplyId === null &&
    !quotaBlocksRequests(usage);
  const busy = submitting || live !== null || regeneratingReplyId !== null;
  const imagesBlocked = quotaBlocksImages(usage);

  // 每道练习只保留一张题目主卡；其余出现该练习的轮次按 request_id 对应到具体一次作答，
  // 渲染成紧凑回执。历史数据没有 request_id 时回退为“查看完整题目”链接，不丢记录。
  const savedTurns = detailState.phase === "ready" ? detailState.detail.turns : [];
  const draftTurn = streamDraft?.conversationId === currentId ? streamDraft : null;
  const showDraftTurn = draftTurn && !savedTurns.some((turn) => turn.request_id === draftTurn.requestId);
  const visibleTurns = showDraftTurn ? [...savedTurns, {
    ...draftTurn.turn,
    status: draftTurn.interrupted ? "interrupted" as const : "running" as const,
    assistant_message: !draftTurn.paperQueryActive || draftTurn.interrupted ? draftTurn.text : "",
  }] : savedTurns;

  const questionPresentation = useMemo(() => {
    const owners = new Map<string, string>();
    const snapshots = new Map<string, QuestionCard>();
    if (detailState.phase === "ready") {
      for (const turn of detailState.detail.turns) {
        if (!turn.question) continue;
        const card = turn.question;
        const key = `${card.question_id}:${card.build_id}:${card.content_sha256}`;
        if (!owners.has(key)) owners.set(key, turn.request_id);
        const previous = snapshots.get(key);
        snapshots.set(key, {
          ...previous, ...card,
          answer_revealed: Boolean(previous?.answer_revealed || card.answer_revealed),
          answer_detail: card.answer_detail ?? previous?.answer_detail,
          answer_revealed_at: card.answer_revealed_at ?? previous?.answer_revealed_at,
          source_request_id: previous?.source_request_id ?? card.source_request_id,
          reference_code: card.reference_code ?? previous?.reference_code,
          code_blocks: card.code_blocks?.length ? card.code_blocks : previous?.code_blocks,
          code_annotations: card.code_annotations?.length ? card.code_annotations : previous?.code_annotations,
          independent_solutions: card.independent_solutions?.length ? card.independent_solutions : previous?.independent_solutions,
          code_examples: card.code_examples?.length ? card.code_examples : previous?.code_examples,
        });
      }
    }
    return { owners, snapshots };
  }, [detailState]);

  const exercisePresentation = useMemo(() => {
    const ownerByExercise = new Map<string, string>();
    const attemptByTurn = new Map<string, ExerciseAttemptView>();
    const receiptAttemptIds = new Set<string>();
    const receiptSeqs = new Map<string, number[]>();
    if (detailState.phase !== "ready") {
      return { ownerByExercise, attemptByTurn, receiptAttemptIds, receiptSeqs };
    }
    for (const turn of detailState.detail.turns) {
      if (turn.exercise && !ownerByExercise.has(turn.exercise.id)) {
        ownerByExercise.set(turn.exercise.id, turn.request_id);
      }
    }
    for (const turn of detailState.detail.turns) {
      const exercise = turn.exercise;
      if (!exercise) {
        continue;
      }
      // 主卡所在轮次的尝试留在主卡，只有后续轮次才渲染成回执；否则
      // “首轮即作答”的历史数据会把唯一一次作答从主卡和回执里同时藏掉。
      if (ownerByExercise.get(exercise.id) === turn.request_id) {
        continue;
      }
      const matched = (exercise.attempts ?? []).find(
        (attempt) =>
          attempt.request_id !== null && attempt.request_id === turn.request_id,
      );
      if (matched) {
        attemptByTurn.set(turn.request_id, matched);
        receiptAttemptIds.add(matched.id);
        const seqs = receiptSeqs.get(exercise.id) ?? [];
        seqs.push(turn.seq);
        receiptSeqs.set(exercise.id, seqs);
      }
    }
    return { ownerByExercise, attemptByTurn, receiptAttemptIds, receiptSeqs };
  }, [detailState]);

  const latestTurn =
    detailState.phase === "ready" && detailState.detail.turns.length > 0
      ? detailState.detail.turns[detailState.detail.turns.length - 1]
      : null;
  const composerDisabledReason: "no-conversation" | "loading" | null =
    currentId === null
      ? "no-conversation"
      : detailState.phase === "ready"
        ? null
        : "loading";
  const confirmedCourse =
    detailState.phase === "ready"
      ? detailState.detail.context?.confirmed_course ?? null
      : null;
  // 动态轮可能保留旧题引用。按本轮最后一个成功改变话题的动作判断输入提示。
  const latestFocus = latestTurn?.kind === "dynamic"
    ? [...(latestTurn.composite_results ?? [])].reverse().find(({ action, status }) =>
        status === "success" && (CATALOG_FOCUS_ACTIONS.has(action) || TUTORING_FOCUS_ACTIONS.has(action)))
    : undefined;
  const isNonTutoringLatest =
    latestTurn?.kind === "query" || (latestFocus !== undefined && CATALOG_FOCUS_ACTIONS.has(latestFocus.action));
  const tutoringActive = Boolean(activeProblem && !isNonTutoringLatest);
  const conversationMaterials =
    detailState.phase === "ready" ? (detailState.detail.materials ?? []) : [];
  const materialArtifactsById = useMemo(() => {
    const map = new Map<string, ArtifactSummary>();
    for (const entry of conversationMaterials) {
      for (const art of entry.artifacts) {
        map.set(art.id, art);
      }
    }
    return map;
  }, [conversationMaterials]);
  const activeMaterialProgress = useMemo(() => {
    if (!currentId) return [];
    const serverItems = materialProgressByConv[currentId] ?? [];
    if (serverItems.length > 0) return serverItems;
    return optimisticProgressByConv[currentId] ?? [];
  }, [currentId, materialProgressByConv, optimisticProgressByConv]);
  const attachedMaterialIds = useMemo(
    () => new Set(conversationMaterials.map((item) => item.material.id)),
    [conversationMaterials],
  );

  const conversationList = (
    <ConversationList
      conversations={visibleConversations}
      currentId={currentId}
      loading={listState.phase === "loading"}
      error={listError}
      creating={creating}
      createTarget={rail?.create}
      onCreate={() => {
        onNavigateToConversation?.();
        void handleCreate();
      }}
      onSelect={(id) => {
        openConversation(id);
        setSidebarOpen(false);
        onNavigateToConversation?.();
      }}
      onRequestDelete={(id) => void handleRequestDeleteConversation(id)}
      onRetry={() => setListAttempt((value) => value + 1)}
    />
  );
  type ResultCard = { id: string; materialId?: string; title: string; endPage: number };
  const outputsByTurn = new Map<string, ResultCard[]>();
  const shownArtifacts = new Set<string>();
  for (const turn of visibleTurns) {
    for (const result of turn.composite_results ?? []) {
      const details = result.details;
      const id = details?.artifact_id;
      if (result.action !== "generate_material_artifact" || !details || typeof id !== "string" || shownArtifacts.has(id)) continue;
      const summary = materialArtifactsById.get(id);
      const hasReadyOutput = (["notes", "graph", "quiz"] as const).some((view) =>
        (details[`${view}_status`] ?? summary?.[`${view}_status`]) === "ready");
      if (!hasReadyOutput && result.status !== "success") continue;

      shownArtifacts.add(id);
      const cards = outputsByTurn.get(turn.request_id) ?? [];
      cards.push({
        id,
        materialId: typeof details.material_id === "string" ? details.material_id : summary?.material_id,
        title: typeof details.material_title === "string" ? details.material_title : summary?.title ?? "复习成果",
        endPage: typeof details.end_page === "number" ? details.end_page : summary?.end_page ?? 0,
      });
      outputsByTurn.set(turn.request_id, cards);
    }
  }
  const renderReviewCard = ({ id, materialId, title, endPage }: ResultCard) => {
    const material = conversationMaterials.find((entry) => entry.material.id === materialId)?.material;
    const displayName = material?.display_name ?? title;
    return (
      <ReviewResultCard key={id} id={id} displayName={displayName} summary={materialArtifactsById.get(id)}
        updated={updatedArtifacts[id]} progressKey={reviewProgressKey(activeStorageKey, id)}
        onOpen={(initial) => void handleOpenArtifact(id, displayName, material?.page_count ?? endPage, initial)} />
    );
  };
  const openReceiptArtifact = (id: string, title: string, pageCount: number, tab?: OutputKind) => {
    const card = document.getElementById("material-review-" + id);
    if (card) card.scrollIntoView?.({ block: "center" });
    else void handleOpenArtifact(id, title, pageCount, tab ? { view: tab } : undefined);
  };

  useEffect(() => {
    if (detailState.phase !== "ready" || !stickToBottomRef.current) {
      return;
    }
    const timer = window.setTimeout(() => scrollToBottom("auto"), 0);
    return () => window.clearTimeout(timer);
  }, [
    currentId,
    detailState.phase,
    turnCount,
    latestTurnId,
    live?.message,
    scrollToBottom,
  ]);

  // 输入区展开/收起会改变消息区高度；仍然贴底时继续跟随。
  useEffect(() => {
    const container = scrollRef.current;
    if (!container || typeof ResizeObserver === "undefined") {
      return;
    }
    const observer = new ResizeObserver(() => {
      if (stickToBottomRef.current) {
        scrollToBottom("auto");
      }
    });
    observer.observe(container);
    return () => observer.disconnect();
  }, [currentId, detailState.phase, scrollToBottom]);

  const generateFromUploadReceipt = (materialId: string, output: OutputKind) => {
    const entry = conversationMaterials.find(({ material }) => material.id === materialId);
    if (entry) {
      const end = Math.min(entry.material.page_count, 40);
      const checked = entry.readability?.checked_pages ?? entry.readability?.pages?.map((page) => page.page_no);
      const hasUnchecked = !entry.readability || Boolean(checked &&
        Array.from({ length: end }, (_, index) => index + 1).some((page) => !checked.includes(page)));
      const pending = entry.readability?.scanned_pages.some((page) => page <= end);
      if (hasUnchecked || pending) {
        document.getElementById("material-card-" + materialId)?.scrollIntoView?.({ block: "start" });
        if (hasUnchecked) {
          void handleCheckMaterialReadability(materialId, 1, end);
        } else {
          setNotice("所选范围还有待识别的页。请在资料卡选择识别，或用已有文字生成部分成果。");
        }
        return;
      }
    }
    void handleGenerateMaterial(materialId, [output]);
  };
  const renderNotice = () => notice && (
    <div role="status" className="workspace-notice">
      <p>{notice.text}</p>
      {currentId && notice.canRefresh && (
        <button type="button" onClick={() => setDetailAttempt((value) => value + 1)} className="quiet-button">
          刷新会话记录
        </button>
      )}
    </div>
  );
  const dockAvailable = currentId !== null && detailState.phase === "ready";

  return (
    <section className="conversation-workspace" aria-labelledby="conversation-heading">
      {rail?.history && createPortal(conversationList, rail.history)}
      {rail?.usage && createPortal(<UsageNotice usage={usage} error={usageError} placement={dockAvailable ? "summary" : "all"} />, rail.usage)}

      <header className="context-header">
        <div className="context-path">
          {!rail && (
            <button type="button" aria-expanded={sidebarOpen} aria-controls="conversation-list-panel"
              onClick={() => setSidebarOpen((open) => !open)} className="quiet-button">
              {sidebarOpen ? "收起会话列表" : "会话列表"}
            </button>
          )}
          <h2 id="conversation-heading">{confirmedCourse ?? "学习会话"}</h2>
          <span aria-hidden="true">/</span>
          <h3>{currentConversation?.title ?? "新建学习会话"}</h3>
        </div>
        {dockAvailable && (
          <button
            type="button"
            className="quiet-button"
            onClick={() => setLibraryOpen(true)}
          >
            资料与成果
          </button>
        )}
      </header>

      <div className={`conversation-body ${!rail && sidebarOpen ? "conversation-body--with-list" : ""}`}>
        {!rail && (
          <div id="conversation-list-panel" className={`standalone-history ${sidebarOpen ? "" : "hidden"}`}>
            {conversationList}
          </div>
        )}

        <div className="conversation-content">
          {!dockAvailable && renderNotice()}
          {currentId === null && (
            <div className="reading-scroll">
              <div className="empty-study">
                <p className="eyebrow">学习会话</p>
                <h3>今天准备复习哪一门科目？</h3>
                <p>新建一个会话，就能输入题目、上传手写解法或检索真题。</p>
                <button type="button" onClick={() => void handleCreate()} disabled={creating} className="primary-button">
                  {creating ? "创建中…" : "新建会话"}
                </button>
              </div>
            </div>
          )}

          {currentId !== null && detailState.phase === "loading" && (
            <div className="min-h-0 flex-1 overflow-y-auto">
              {renderErrorNotice()}
              <div className="rounded-2xl border border-slate-200/90 bg-white/70 p-6">
                <div className="h-4 w-40 animate-pulse rounded bg-slate-200" />
                <div className="mt-3 h-24 animate-pulse rounded-xl bg-slate-100" />
                <p className="mt-3 text-xs text-slate-500">正在读取会话记录…</p>
              </div>
            </div>
          )}

          {currentId !== null && detailState.phase === "error" && (
            <div className="min-h-0 flex-1 overflow-y-auto">
              {renderErrorNotice()}
              <div className="rounded-2xl border border-rose-200 bg-rose-50/80 p-5">
                <p className="text-sm font-semibold text-rose-900">没能读取会话记录</p>
                <p className="mt-1 break-words text-sm leading-6 text-rose-800">
                  {detailState.message}
                </p>
                <button
                  type="button"
                  onClick={() => setDetailAttempt((value) => value + 1)}
                  className="mt-3 inline-flex items-center rounded-xl border border-rose-300 bg-white px-4 py-2 text-sm font-medium text-rose-800 hover:bg-rose-100"
                >
                  重新读取
                </button>
              </div>
            </div>
          )}

          {currentId !== null && detailState.phase === "ready" && (
            <div className="conversation-content">
              <div ref={scrollRef} onScroll={handleMessageScroll} data-testid="message-scroll" className="reading-scroll">
                <div className="reading-column">
                  {conversationMaterials.length > 0 && (
                    <ConversationMaterialPanel
                      items={conversationMaterials}
                      generatingKey={generatingMaterialKey}
                      recognizingId={recognizingMaterialId}
                      checkingReadabilityId={checkingReadabilityId}
                      onRecognize={handleRecognizeMaterial}
                      onCheckReadability={(materialId, start, end) =>
                        void handleCheckMaterialReadability(materialId, start, end)
                      }
                      quotaBlocked={quotaBlocksRequests(usage)}
                      onGenerate={(id, outputs, start, end, hint, mode) =>
                        void handleGenerateMaterial(id, outputs, start, end, hint, undefined, mode)
                      }
                      onOpenArtifact={(artifactId, name, pageCount, initialTab) =>
                        void handleOpenArtifact(artifactId, name, pageCount, initialTab ? { view: initialTab } : undefined)
                      }
                      onDetach={(materialId) => void handleDetachMaterial(materialId)}
                    />
                  )}


                  {visibleTurns.length === 0 && (
                    <div className="flex h-full min-h-40 items-center justify-center px-4 text-center">
                      <div className="empty-study">
                        <h3>今天准备复习哪一门科目？</h3>
                        <p>这是新会话。可以输入“我要复习高数”，也可以直接粘贴一道题。</p>
                      </div>
                    </div>
                  )}

                  {visibleTurns.length > 0 && (
                    <ul className="conversation-turns">
                      {visibleTurns.map((turn) => {
                        const exercise = turn.exercise ?? null;
                        const role = !exercise
                          ? "full"
                          : exercisePresentation.ownerByExercise.get(exercise.id) ===
                              turn.request_id
                            ? "full"
                            : exercisePresentation.attemptByTurn.has(turn.request_id)
                              ? "attempt"
                              : "reference";
                        const materialOutputs = outputsByTurn.get(turn.request_id);
                        return (
                          <ConversationTurn
                            key={turn.request_id}
                            turn={turn}
                            liveProgress={turn.request_id === live?.requestId ? live.message : undefined}
                            unsavedDraft={turn.request_id === draftTurn?.requestId && !!showDraftTurn}
                            questionOwners={questionPresentation.owners}
                            questionSnapshots={questionPresentation.snapshots}
                            onJumpToQuestion={(key) => document.getElementById(`question-${key}`)?.scrollIntoView({ behavior: "smooth", block: "center" })}
                            downloads={downloads}
                            onDownload={onDownload}
                            onRetry={handleRetry}
                            onRegenerateReply={(item) => void handleRegenerateReply(item)}
                            regeneratingReply={regeneratingReplyId === turn.request_id}
                            replyQuotaBlocked={quotaBlocksRequests(usage)}
                            onFeedback={handleFeedback}
                            onRecord={handleRecord}
                            onCorrect={handleCorrect}
                            onSupplement={handleSupplement}
                            onNew={handleNew}
                            onIntentChoice={handleIntentChoice}
                            onOpenRecord={onOpenRecord}
                            onSubmitExerciseAnswer={handleExerciseAnswer}
                            onRevealExerciseSolution={handleExerciseReveal}
                            onSuggestionClick={handleSuggestionClick}
                            onQuestionAction={handleQuestionAction}
                            onJumpToExercise={handleJumpToExercise}
                            onCorrectExercise={handleCorrectExercise}
                            onGenerateMaterialOutput={generateFromUploadReceipt}
                            onConfirmMaterialCoverage={(id, outputs, start, end, mode, hint) =>
                              void handleGenerateMaterial(id, outputs, start, end, hint, currentId, mode)
                            }
                            onRetryMaterialOutput={handleRetryMessageMaterial}
                            onOpenMaterialArtifact={(artifactId, title, pageCount, tab) =>
                              openReceiptArtifact(artifactId, title, pageCount, tab)
                            }
                            onOpenWorkbenchSource={(source) => {
                              const material = conversationMaterials.find((entry) =>
                                entry.artifacts.some((artifact) => artifact.id === source.artifact_id))?.material;
                              void handleOpenArtifact(source.artifact_id, material?.display_name ?? "复习成果",
                                material?.page_count ?? 0, { view: source.view, kcId: source.kc_id ?? undefined });
                            }}
                            materialArtifactsById={materialArtifactsById}
                            materialResultIds={materialOutputs?.map(({ id }) => id)}
                            materialResultCards={materialOutputs?.map(renderReviewCard)}
                            isLatestTurn={
                              turn.request_id === latestTurn?.request_id &&
                              turn.status !== "running"
                            }
                            hasActiveProblem={Boolean(activeProblem)}
                            exerciseRole={role}
                            exerciseAttempt={
                              exercisePresentation.attemptByTurn.get(turn.request_id) ??
                              null
                            }
                            hiddenAttemptIds={
                              exercise
                                ? (exercise.attempts ?? [])
                                    .filter((attempt) =>
                                      exercisePresentation.receiptAttemptIds.has(
                                        attempt.id,
                                      ),
                                    )
                                    .map((attempt) => attempt.id)
                                : undefined
                            }
                            receiptSeqs={
                              exercise
                                ? exercisePresentation.receiptSeqs.get(exercise.id) ??
                                  []
                                : undefined
                            }
                            focusToken={
                              exercise ? exerciseFocusTokens[exercise.id] ?? 0 : 0
                            }
                            correctionToken={
                              exercise
                                ? exerciseCorrectionTokens[exercise.id] ?? 0
                                : 0
                            }
                            exerciseDrafts={exerciseDrafts[currentId]}
                            exerciseDraftImages={exerciseDraftImages[currentId]}
                            exerciseImageNotices={exerciseImageNotices[currentId]}
                            imagesBlocked={quotaBlocksImages(usage)}
                            recordsByRoot={recordsByRoot}
                            savingMistakeRootId={savingMistakeRootId}
                            onAddExerciseImages={addExerciseImages}
                            onRemoveExerciseImage={removeExerciseImage}
                            onRetryExerciseImage={retryExerciseImage}
                            onSaveExerciseMistake={handleSaveExerciseMistake}
                            onExerciseDraftChange={(exerciseId, value) =>
                              setExerciseDrafts((current) => ({
                                ...current,
                                [currentId]: {
                                  ...current[currentId],
                                  [exerciseId]: value,
                                },
                              }))
                            }
                            busy={busy}
                          />
                        );
                      })}
                    </ul>
                  )}

                </div>
              </div>

              <div
                id="turn-composer"
                data-testid="composer"
                className="composer-position"
              >
                <TurnComposer
                  notices={
                    <>
                      <UsageNotice usage={usage} error={usageError} placement={rail ? "warnings" : "all"} />
                      {activeMaterialProgress.map((item) => {
                        const outputNames = (item.outputs ?? []).map((out) =>
                          out === "notes" ? "章节笔记" : out === "quiz" ? "章节测试" : "知识图谱",
                        );
                        const pageLabel =
                          item.start_page === item.end_page
                            ? `第 ${item.start_page} 页`
                            : `第 ${item.start_page}–${item.end_page} 页`;
                        const failedPages = item.ocr_failed_pages ?? [];
                        return (
                          <div
                            key={item.task_id}
                            data-testid="material-progress-banner"
                            role="status"
                            className="workspace-notice"
                          >
                            <div className="min-w-0 space-y-0.5 text-xs">
                              <p className="font-medium text-slate-800">
                                {item.material_name} · {pageLabel}
                                {outputNames.length > 0 ? ` · ${outputNames.join("、")}` : ""}
                              </p>
                              <p className="text-slate-600">
                                当前阶段：{item.stage_label}
                                {(item.kind === "ocr" || item.stage === "ocr") && item.ocr_total > 0
                                  ? `（已处理 ${item.ocr_done}/${item.ocr_total} 页${
                                      failedPages.length > 0
                                        ? `，失败第 ${failedPages.join("、")} 页`
                                        : ""
                                    }）`
                                  : ""}
                              </p>
                            </div>
                          </div>
                        );
                      })}
                      {activeMaterialTurnId && activeMaterialProgress.length === 0 && (
                        <p className="workspace-notice" role="status">正在整理资料复习成果…</p>
                      )}
                      {materialStudyTarget && (
                        <div className="workspace-notice" role="status">
                          <p>已锁定当前学习资料关联上下文（不换题）。</p>
                          <button
                            type="button"
                            className="quiet-button"
                            onClick={() => setMaterialStudyTarget(null)}
                          >
                            取消锁定
                          </button>
                        </div>
                      )}
                      {renderErrorNotice()}
                      {renderNotice()}
                    </>
                  }
                  remainingRequests={usage?.requests.remaining}
                  value={draft}
                  running={busy}
                  canCancel={(live !== null || Boolean(activeMaterialTurnId)) &&
                    (!stoppingMaterialRequestId || stoppingMaterialRequestId !== activeMaterialTurnId)}
                  disabled={!canSubmit}
                  quotaBlocked={quotaBlocksRequests(usage)}
                  imagesBlocked={imagesBlocked}
                  disabledReason={composerDisabledReason}
                  activeProblem={activeProblem}
                  tutoringActive={tutoringActive}
                  mode={mode}
                  targetProblem={targetProblem}
                  files={files}
                  sourcePaperId={sourcePaperId}
                  sourceOptions={sourceOptions}
                  onChange={setDraft}
                  onAddFiles={addFiles}
                  onUploadPdf={(file) => void handleUploadPdf(file)}
                  uploadingPdf={uploadingPdf}
                  pdfUploadStatus={currentPdfUpload}
                  onRetryPdfUpload={() => void handleRetryPdfUpload()}
                  onDismissPdfUpload={handleDismissPdfUpload}
                  onRemoveFile={removeFile}
                  onSourceChange={setSourcePaperId}
                  onResetMode={() => {
                    setMode("auto");
                    setTargetProblem(null);
                    setNotice(null);
                  }}
                  onSubmit={() => void submit()}
                  onCancel={handleStop}
                />
              </div>
            </div>
          )}
        </div>
      </div>


      {readerModal && currentId && (
        <ReviewWorkbench
          key={readerModal.openSequence}
          artifact={readerModal.artifact}
          displayName={readerModal.displayName}
          pageCount={readerModal.pageCount}
          conversationId={currentId}
          accountKey={activeStorageKey}
          progressKey={reviewProgressKey(activeStorageKey, readerModal.artifact.id)}
          initial={readerModal.initial}
          retrying={retryingReaderOutput}
          actionError={errorNotice}
          onClose={closeWorkbench}
          onUpdate={(updated) => {
            setUpdatedArtifacts((current) => ({ ...current, [updated.id]: updated }));
            setReaderModal((current) => current?.artifact.id === updated.id ? { ...current, artifact: updated } : current);
          }}
          onGenerate={handleRetryReaderOutput}
          renderAsk={readerModal.artifact.conversation_id === currentId || attachedMaterialIds.has(readerModal.artifact.material_id)
            ? (target, view, close) => {
              const { artifact, requestIds, openSequence } = readerModal;
              const question = artifact.questions.find((item) => item.id === target.questionId);
              const label = (target.kcId ? reviewKcIndex(artifact)[target.kcId]?.label : undefined)
                ?? (question ? "章节测试题" : artifact.title);
              const turns = visibleTurns.filter((turn) => requestIds.includes(turn.request_id));
              const running = requestIds.includes(live?.requestId ?? draftTurn?.requestId ?? "") && busy;
              return (
                <ReviewWorkbenchAsk
                  key={target.questionId ?? target.kcId ?? "artifact"}
                  label={label} pages={target.pages} question={question?.stem} turns={turns}
                  busy={busy} running={running} blocked={quotaBlocksRequests(usage)}
                  error={requestIds.length ? errorNotice : null}
                  onClose={close} onStop={handleStop}
                  onSend={(text) => void submit("auto", text, {
                    files: [],
                    workbenchSource: {
                      artifact_id: artifact.id, view, kc_id: target.kcId, question_id: target.questionId,
                      pages: target.pages, label,
                    },
                    onRequestStarted: (requestId) => setReaderModal((current) => current?.openSequence === openSequence
                      ? { ...current, requestIds: [...current.requestIds, requestId] } : current),
                  })}
                />
              );
            }
            : undefined}
        />
      )}

      {libraryOpen && currentId && (
        <MaterialLibraryModal
          conversationId={currentId}
          attachedMaterialIds={attachedMaterialIds}
          onAttachMaterial={handleAttachFromLibrary}
          onOpenArtifact={(artifactId, name, pageCount, initialTab) => {
            setLibraryOpen(false);
            void handleOpenArtifact(artifactId, name, pageCount, initialTab ? { view: initialTab } : undefined);
          }}
          onStartGuideStudy={(convId, prompt, guideId, itemId, studyMode) =>
            handleStartStudyFromMaterial(convId, prompt, { guideId, itemId, mode: studyMode })
          }
          onClose={() => setLibraryOpen(false)}
        />
      )}

      {deleteConfirm && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-slate-900/50 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="delete-conv-title"
        >
          <div className="w-full max-w-md space-y-3 rounded-2xl border border-slate-200 bg-white p-5 shadow-xl">
            <header className="space-y-1">
              <p className="eyebrow">删除会话确认</p>
              <h3 id="delete-conv-title" className="text-base font-semibold text-slate-900">
                确定删除会话「{deleteConfirm.impact.title}」吗？
              </h3>
            </header>
            <p className="text-sm text-slate-700">
              {deleteConfirm.impact.summary}
            </p>
            <p className="text-sm text-slate-700">
              本会话保存到错题本的记录也会被移除。
            </p>
            {deleteConfirm.impact.linked_material_count > 0 && (
              <div className="rounded-lg bg-slate-50 p-2.5 text-xs text-slate-700">
                <p>
                  关联资料：{deleteConfirm.impact.linked_material_count} 份 · 相关成果：{deleteConfirm.impact.artifact_count} 份
                </p>
                <p className="mt-1 text-slate-500">
                  {deleteConfirm.impact.materials_still_accessible
                    ? "删除会话后，原 PDF 与已生成成果仍保留在个人资料库中，可在其他会话找回。"
                    : "该会话关联的资料与成果将一并保留到个人资料库。"}
                </p>
              </div>
            )}
            {deleteConfirm.error && (
              <p role="alert" className="text-xs text-rose-700">
                {deleteConfirm.error}
              </p>
            )}
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                className="quiet-button"
                disabled={deleteConfirm.deleting}
                onClick={() => setDeleteConfirm(null)}
              >
                取消
              </button>
              <button
                type="button"
                className="rounded-xl bg-rose-600 px-3 py-1.5 text-sm font-medium text-white hover:bg-rose-700 disabled:opacity-50"
                disabled={deleteConfirm.deleting}
                onClick={() => void handleConfirmDeleteConversation()}
              >
                {deleteConfirm.deleting ? "正在删除…" : "确认删除会话"}
              </button>
            </div>
          </div>
        </div>
      )}
    </section>
  );
}
