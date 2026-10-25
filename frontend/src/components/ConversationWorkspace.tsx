import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type UIEvent,
} from "react";
import ConversationList from "./ConversationList";
import ConversationTurn from "./ConversationTurn";
import TurnComposer, {
  type ComposerMode,
  type PendingFile,
} from "./TurnComposer";
import {
  ConversationsApiError,
  createConversation,
  createRequestId,
  getConversation,
  listConversations,
  revealExerciseSolution,
  streamTurn,
  turnBusinessStatus,
  type AttachmentRole,
  type ConversationDetail,
  type ConversationSummary,
  type ExerciseAttemptView,
  type ProblemView,
  type Source,
  type SuggestionItem,
  type TurnKind,
  type TurnProgressEvent,
  type TurnStage,
} from "../lib/conversationsApi";
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

type ListState =
  | { phase: "loading" }
  | { phase: "ready"; items: ConversationSummary[] }
  | { phase: "error"; message: string };

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

type ConversationWorkspaceProps = {
  downloads: DownloadStates;
  onDownload: (paper: PaperItem) => void;
  onOpenRecord: (recordId: string) => void;
};

const STORAGE_KEY = "hhu-exam-review.current-conversation";
const MAX_FILES = 4;
const MAX_TOTAL_BYTES = 20 * 1024 * 1024;

function readStoredId(): string | null {
  try {
    return window.localStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function writeStoredId(id: string): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, id);
  } catch {
    // 隐私模式等情况下无法持久化，只保留本次页面内的状态。
  }
}

function clearStoredId(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // 同上。
  }
}

function toMessage(error: unknown): string {
  if (error instanceof ConversationsApiError || error instanceof AttachmentsApiError) {
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
}: ConversationWorkspaceProps) {
  const [listState, setListState] = useState<ListState>({ phase: "loading" });
  const [listAttempt, setListAttempt] = useState(0);
  const [currentId, setCurrentId] = useState<string | null>(null);
  const [detailState, setDetailState] = useState<DetailState>({ phase: "idle" });
  const [detailAttempt, setDetailAttempt] = useState(0);
  const [draft, setDraft] = useState("");
  const [live, setLive] = useState<LiveState | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [errorNotice, setErrorNotice] = useState<string | null>(null);
  const [creating, setCreating] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [mode, setMode] = useState<ComposerMode>("auto");
  const [files, setFiles] = useState<PendingFile[]>([]);
  const [studentAnswer, setStudentAnswer] = useState("");
  const [solutionText, setSolutionText] = useState("");
  const [sourcePaperId, setSourcePaperId] = useState("");
  const [targetProblem, setTargetProblem] = useState<ProblemView | null>(null);
  const [exerciseDrafts, setExerciseDrafts] = useState<Record<string, Record<string, { answer: string; solution: string }>>>({});
  const [exerciseFocusTokens, setExerciseFocusTokens] = useState<Record<string, number>>({});
  const [exerciseCorrectionTokens, setExerciseCorrectionTokens] = useState<Record<string, number>>({});
  const composerSnapshot = { draft, studentAnswer, solutionText, sourcePaperId, mode, files, targetProblem };
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
        const stored = readStoredId();
        if (!stored) {
          return;
        }
        if (payload.items.some((item) => item.id === stored)) {
          currentIdRef.current = stored;
          setCurrentId(stored);
        } else {
          clearStoredId();
        }
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setListState({ phase: "error", message: toMessage(error) });
      });

    return () => controller.abort();
  }, [listAttempt]);

  const loadDetail = useCallback(
    async (conversationId: string): Promise<ConversationDetail | null> => {
      const requestToken = detailRequestRef.current + 1;
      detailRequestRef.current = requestToken;

      try {
        const detail = await getConversation(conversationId);
        if (
          currentIdRef.current === conversationId &&
          detailRequestRef.current === requestToken
        ) {
          setDetailState({ phase: "ready", detail });
        }
        return detail;
      } catch (error) {
        if (
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
      filesRef.current.forEach((item) => URL.revokeObjectURL(item.previewUrl));
      composerDraftsRef.current.forEach((saved) => saved.files.forEach((item) => URL.revokeObjectURL(item.previewUrl)));
    },
    [],
  );

  const refreshList = useCallback(async () => {
    try {
      const payload = await listConversations();
      setListState({ phase: "ready", items: payload.items });
    } catch {
      // 列表刷新失败不覆盖已有内容；下一次操作或重试会再次读取。
    }
  }, []);

  const openConversation = useCallback(
    (conversationId: string) => {
      if (currentIdRef.current === conversationId) {
        return;
      }

      abortRef.current?.abort();
      abortRef.current = null;
      streamSeqRef.current += 1;
      submittingRef.current = false;
      setSubmitting(false);
      stickToBottomRef.current = true;
      updateLive(null);
      setNotice(null);
      setErrorNotice(null);
      if (currentIdRef.current) composerDraftsRef.current.set(currentIdRef.current, composerRef.current);
      const saved = composerDraftsRef.current.get(conversationId);
      setDraft(saved?.draft ?? "");
      setStudentAnswer(saved?.studentAnswer ?? "");
      setSolutionText(saved?.solutionText ?? "");
      setSourcePaperId(saved?.sourcePaperId ?? "");
      setMode(saved?.mode ?? "auto");
      setTargetProblem(saved?.targetProblem ?? null);
      setPendingFiles(() => saved?.files ?? []);
      currentIdRef.current = conversationId;
      setCurrentId(conversationId);
      setDetailState({ phase: "loading" });
      writeStoredId(conversationId);
    },
    [setPendingFiles, updateLive],
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
        startNew?: boolean;
        studentAnswer?: string;
        solutionText?: string;
        sourcePaperId?: string;
        exerciseId?: string;
        retryOf?: string;
        attemptKind?: "supplement" | "correction";
      } = {},
    ) => {
      if (submittingRef.current) {
        return;
      }
      const conversationId = currentIdRef.current;
      if (!conversationId) {
        return;
      }

      const text = (textOverride ?? draft).trim();
      const pending = options.files ?? (kindOverride ? [] : filesRef.current);
      if (!text && pending.length === 0 && !options.retryOf &&
          !(kindOverride === "exercise" && (options.studentAnswer?.trim() || options.solutionText?.trim()))) {
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
      // 隐藏的附加草稿从不决定普通聊天的意图；只由显式入口读取。
      const submittedAnswer = kind === "exercise" ? options.studentAnswer ?? "" : explicitFields ? options.studentAnswer ?? (!kindOverride ? studentAnswer : "") : "";
      const submittedSolution = kind === "exercise" ? options.solutionText ?? "" : explicitFields ? options.solutionText ?? (!kindOverride ? solutionText : "") : "";

      const requestId = createRequestId();
      const sequence = streamSeqRef.current + 1;
      streamSeqRef.current = sequence;
      submittingRef.current = true;
      setSubmitting(true);

      const controller = new AbortController();
      abortRef.current = controller;
      setNotice(null);
      setErrorNotice(null);
      stickToBottomRef.current = true;
      scrollToBottom("auto");
      updateLive({
        requestId,
        stage: "starting",
        message: pending.length ? "正在上传图片…" : "正在提交…",
      });

      try {
        const uploaded: { id: string; role: AttachmentRole }[] = [];
        for (const item of pending) {
          const meta = await uploadAttachment(conversationId, item.file, controller.signal);
          if (!isCurrentOperation(conversationId, sequence)) {
            return;
          }
          uploaded.push({ id: meta.id, role: item.role });
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
          studentAnswer:
            submittedAnswer,
          solutionText:
            submittedSolution,
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
          startNew: options.startNew ?? (!kindOverride && kind === "problem" && mode === "new"),
          signal: controller.signal,
          onProgress: (event: TurnProgressEvent) => {
            if (!isCurrentOperation(conversationId, sequence)) {
              return;
            }
            updateLive({
              requestId,
              stage: event.stage,
              message: event.message,
            });
          },
        });

        if (!isCurrentOperation(conversationId, sequence)) {
          return;
        }

        updateLive(null);
        setNotice(null);
        const detail = await loadDetail(conversationId);
        // loadDetail 期间用户可能切到另一个会话并开始输入；不能清掉它的草稿。
        if (!isCurrentOperation(conversationId, sequence)) {
          return;
        }
        void refreshList();
        if (result.tutoring?.kind === "record_created") {
          setNotice("已记录到错题本。会话仍会自动保存；删除记录不会删除原会话。");
        } else if (
          result.tutoring?.kind === "record_exists" &&
          result.tutoring.needs_update
        ) {
          setNotice("这道题已有旧版本记录，可在错题本中更新或重新讲解。");
        }
        if (!detail) {
          setNotice("这一步已完成并保存，但读取会话记录失败，请稍后刷新。");
        }
        const stored = detail?.turns.find((turn) => turn.request_id === requestId);
        const outcome = stored ? turnBusinessStatus(stored) : result.business_status;
        if (outcome === "failed" || outcome === "partial") {
          setErrorNotice(result.assistant_message ?? "本轮业务尚未完成，草稿已保留，请查看结果并按提示恢复。");
        } else {
          if (options.exerciseId) {
            setExerciseDrafts((current) => {
              const forConversation = { ...(current[conversationId] ?? {}) };
              delete forConversation[options.exerciseId as string];
              return { ...current, [conversationId]: forConversation };
            });
          }
          if (!kindOverride) {
            // 清理本次实际提交的字段；卡片、快捷操作和重试不会清空主输入区。
            setDraft((current) => current.trim() === text ? "" : current);
            if (explicitFields) {
              setStudentAnswer((current) => current === submittedAnswer ? "" : current);
              setSolutionText((current) => current === submittedSolution ? "" : current);
              setSourcePaperId(""); setTargetProblem(null); setMode("auto");
            }
            setPendingFiles((current) => current.filter((item) => {
              if (!pending.includes(item)) return true;
              URL.revokeObjectURL(item.previewUrl); return false;
            }));
          }
        }
      } catch (error) {
        if (!isCurrentOperation(conversationId, sequence)) {
          return;
        }
        if (isAbortError(error)) {
          return;
        }

        updateLive(null);
        setErrorNotice(toMessage(error));

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
        if (streamSeqRef.current === sequence) {
          submittingRef.current = false;
          setSubmitting(false);
          abortRef.current = null;
        }
      }
    },
    [
      activeProblem,
      draft,
      isCurrentOperation,
      loadDetail,
      mode,
      refreshList,
      setPendingFiles,
      solutionText,
      sourceOptions,
      sourcePaperId,
      studentAnswer,
      scrollToBottom,
      targetProblem,
      updateLive,
    ],
  );

  const handleStop = useCallback(() => {
    const active = liveRef.current;
    const conversationId = currentIdRef.current;
    if (!active) {
      return;
    }

    streamSeqRef.current += 1;
    const sequence = streamSeqRef.current;
    abortRef.current?.abort();
    abortRef.current = null;
    updateLive(null);
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
        setNotice("已停止。若会话记录仍显示进行中，请稍后刷新确认。");
      }
    })();
  }, [isCurrentOperation, loadDetail, updateLive]);

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

  const handleModeChange = useCallback((next: ComposerMode) => {
    setMode(next);
    if (next !== "correction") {
      setTargetProblem(null);
    }
  }, []);

  const handleCorrect = useCallback((problem: ProblemView) => {
    setTargetProblem(problem);
    setMode("correction");
    setDraft(problem.statement_text);
    setNotice(
      `正在纠正第 ${problem.version} 版题干；发送后会创建该题的新版本并重新讲解，` +
        "历史讲解会标记失效。",
    );
  }, []);

  const handleSupplement = useCallback((_problem: ProblemView) => {
    setTargetProblem(null);
    setMode("supplement");
    setDraft("");
    setNotice("请补充看不清的符号或条件，或换一张更清楚的图片。");
  }, []);

  const handleNew = useCallback(() => {
    setTargetProblem(null);
    setMode("new");
    setDraft("");
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
      if (option === "new_problem") {
        setMode("new");
        setDraft(turn.query);
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
    ) => {
      void submit("exercise", answerText, {
        exerciseId,
        studentAnswer: answerText,
        solutionText,
        attemptKind,
      });
    },
    [submit],
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

  const handleRetry = useCallback(
    (turn: ConversationDetail["turns"][number]) => {
      void submit("auto", "", { retryOf: turn.request_id });
    },
    [submit],
  );

  // 错误提示固定在输入区附近；详情读取失败时输入区会卸载，因此也要在
  // 加载/错误分支渲染同一份告警，避免 409 等业务错误随输入区一起消失。
  const renderErrorNotice = () => {
    if (!errorNotice) {
      return null;
    }
    return (
      <div
        role="alert"
        className="mb-2 max-h-36 overflow-y-auto rounded-xl border-2 border-rose-400 bg-rose-50 p-3 text-xs text-rose-900"
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
  const currentConversation =
    detailState.phase === "ready" ? detailState.detail.conversation : null;
  const canSubmit =
    currentId !== null && detailState.phase === "ready" && !submitting;
  const busy = submitting || live !== null;

  // 每道练习只保留一张题目主卡；其余出现该练习的轮次按 request_id 对应到具体一次作答，
  // 渲染成紧凑回执。历史数据没有 request_id 时回退为“查看完整题目”链接，不丢记录。
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

  return (
    <section
      className="flex h-full min-h-0 flex-col"
      aria-labelledby="conversation-heading"
    >
      <div className="mb-2 hidden shrink-0 items-baseline justify-between gap-2 sm:flex">
        <h2 id="conversation-heading" className="text-sm font-semibold text-ink">
          学习会话
        </h2>
        <p className="text-[11px] text-slate-500">
          一次一道题；历史在消息区上下滚动，输入框固定在底部
        </p>
      </div>

      <div className="grid min-h-0 flex-1 gap-3 md:grid-cols-[230px_minmax(0,1fr)]">
        <div className="max-h-44 min-h-0 overflow-y-auto md:h-full md:max-h-none">
          <ConversationList
            conversations={listItems}
            currentId={currentId}
            loading={listState.phase === "loading"}
            error={listError}
            creating={creating}
            onCreate={() => void handleCreate()}
            onSelect={openConversation}
            onRetry={() => setListAttempt((value) => value + 1)}
          />
        </div>

        <div className="flex min-h-0 min-w-0 flex-col">
          {notice && (
            <div
              role="status"
              className="mb-2 max-h-28 shrink-0 overflow-y-auto rounded-xl border border-amber-200 bg-amber-50/80 px-3 py-2"
            >
              <p className="break-words text-xs leading-5 text-amber-900">{notice}</p>
              {currentId && (
                <button
                  type="button"
                  onClick={() => setDetailAttempt((value) => value + 1)}
                  className="mt-1 rounded-lg border border-amber-300 bg-white px-2 py-0.5 text-[10px] font-medium text-amber-900 hover:bg-amber-100"
                >
                  刷新会话记录
                </button>
              )}
            </div>
          )}

          {currentId === null && (
            <div className="flex min-h-0 flex-1 items-center justify-center overflow-y-auto">
              <div className="max-w-md rounded-2xl border border-dashed border-slate-300 bg-white/70 p-6 text-center">
                <p className="text-sm font-semibold text-ink">先新建一个会话</p>
                <p className="mt-1 text-sm leading-6 text-slate-600">
                  题目、讲解、图片和记录关联都会保存在本地；之后可以从左侧列表重新打开。
                </p>
                <button
                  type="button"
                  onClick={() => void handleCreate()}
                  disabled={creating}
                  className="mt-3 inline-flex items-center rounded-xl bg-river px-4 py-2 text-sm font-medium text-white hover:bg-river/90 disabled:bg-slate-400"
                >
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
            <div className="flex min-h-0 flex-1 flex-col">
              <div className="mb-2 flex shrink-0 flex-wrap items-baseline justify-between gap-2 border-b border-slate-200/70 pb-2">
                <h3 className="truncate text-base font-semibold text-ink">
                  {currentConversation?.title}
                </h3>
                <p className="text-[11px] text-slate-500">
                  共 {detailState.detail.turns.length} 轮
                </p>
              </div>

              <div
                ref={scrollRef}
                onScroll={handleMessageScroll}
                data-testid="message-scroll"
                className="min-h-0 flex-1 overflow-y-auto overscroll-contain pr-1"
              >
                {detailState.detail.turns.length === 0 && !live && (
                  <div className="flex h-full min-h-40 items-center justify-center px-4 text-center">
                    <p className="max-w-md text-sm leading-6 text-slate-600">
                      这是新会话。可以输入“我要复习高数”，也可以直接粘贴一道题。
                    </p>
                  </div>
                )}

                {detailState.detail.turns.length > 0 && (
                  <ul className="space-y-4 pb-2">
                    {detailState.detail.turns.map((turn) => {
                      const exercise = turn.exercise ?? null;
                      const role = !exercise
                        ? "full"
                        : exercisePresentation.ownerByExercise.get(exercise.id) ===
                            turn.request_id
                          ? "full"
                          : exercisePresentation.attemptByTurn.has(turn.request_id)
                            ? "attempt"
                            : "reference";
                      return (
                        <ConversationTurn
                          key={turn.request_id}
                          turn={turn}
                          downloads={downloads}
                          onDownload={onDownload}
                          onRetry={handleRetry}
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
                          onJumpToExercise={handleJumpToExercise}
                          onCorrectExercise={handleCorrectExercise}
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

                {live && (
                  <div
                    aria-live="polite"
                    className="mt-2 rounded-2xl border border-slate-200/90 bg-white/90 p-4 shadow-card backdrop-blur"
                  >
                    <p className="text-sm font-medium text-ink">{live.message}</p>
                    <p className="mt-1 text-[11px] text-slate-500">
                      本轮进行中；完成后会先保存再刷新记录。
                    </p>
                  </div>
                )}
              </div>

              <div
                id="turn-composer"
                data-testid="composer"
                className="shrink-0 border-t border-slate-200/70 bg-[#f6f4ee] pt-2"
              >
                {renderErrorNotice()}
                <TurnComposer
                  value={draft}
                  running={busy}
                  disabled={!canSubmit}
                  disabledReason={composerDisabledReason}
                  confirmedCourse={confirmedCourse}
                  activeProblem={activeProblem}
                  mode={mode}
                  files={files}
                  studentAnswer={studentAnswer}
                  solutionText={solutionText}
                  sourcePaperId={sourcePaperId}
                  sourceOptions={sourceOptions}
                  onChange={setDraft}
                  onModeChange={handleModeChange}
                  onAddFiles={addFiles}
                  onRemoveFile={removeFile}
                  onStudentAnswerChange={setStudentAnswer}
                  onSolutionTextChange={setSolutionText}
                  onSourceChange={setSourcePaperId}
                  onSubmit={() => void submit()}
                  onCancel={handleStop}
                />
              </div>
            </div>
          )}
        </div>
      </div>
    </section>
  );
}
