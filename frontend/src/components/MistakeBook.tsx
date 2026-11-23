import { useCallback, useEffect, useRef, useState } from "react";
import SourceBadge from "./SourceBadge";
import MathText from "./MathText";
import {
  AttachmentThumbnails,
  EvaluationBlock,
  KIND_LABELS,
  resolveExerciseAttemptStatus,
} from "./ExerciseCard";
import {
  MistakesApiError,
  adoptProblem,
  deleteMistakeRecord,
  getMistakeRecord,
  listMistakeRecords,
  streamRecordCorrection,
  updateMistakeRecord,
  type MistakeRecordDetail,
  type RevisionView,
} from "../lib/mistakesApi";
import {
  uploadAttachment,
  validateLocalImage,
  AttachmentsApiError,
} from "../lib/attachmentsApi";
import { createRequestId, type Source } from "../lib/conversationsApi";
import type { RecordSummary } from "../lib/conversationsApi";
import { formatDateTime } from "../lib/display";

type ListState =
  | { phase: "loading" }
  | { phase: "ready"; items: RecordSummary[]; total: number }
  | { phase: "error"; message: string };

type DetailState =
  | { phase: "idle" }
  | { phase: "loading" }
  | { phase: "ready"; detail: MistakeRecordDetail }
  | { phase: "error"; message: string };

type MistakeBookProps = {
  selectedId: string | null;
  onSelectRecord: (recordId: string | null) => void;
  onCountChange?: (count: number) => void;
  onOpenConversation?: (conversationId: string) => void;
};

function messageOf(error: unknown): string {
  if (error instanceof MistakesApiError || error instanceof AttachmentsApiError) {
    return error.message;
  }
  return "操作失败，请稍后重试。";
}

function sourceLabel(source: Source): string {
  if (source.type === "student_text") {
    return source.text ?? "学生自述";
  }
  if (source.type === "registered_paper") {
    return source.paper_title ?? source.paper_id ?? "已登记试卷";
  }
  return "未关联来源";
}

/** 识别自动生成的题干摘要，包括早期截在公式内部的标题。 */
function isStatementTitle(title: string, statement: string): boolean {
  const squash = (value: string) => value.replace(/\s+/g, "").replace(/…$/, "");
  const head = squash(title);
  const body = squash(statement);
  if (!head || !body) return false;
  if (head === body) return true;
  return title.trimEnd().endsWith("…") &&
    (body.startsWith(head) || (statement.trimEnd().endsWith("…") && head.startsWith(body)));
}

/** 桌面双栏布局（与 styles.css 的 860px 断点一致）下才自动选中第一条。 */
function isDesktopLayout(): boolean {
  return typeof window !== "undefined" &&
    typeof window.matchMedia === "function" &&
    window.matchMedia("(min-width: 860px)").matches;
}

export default function MistakeBook({
  selectedId,
  onSelectRecord,
  onCountChange,
  onOpenConversation,
}: MistakeBookProps) {
  const [listState, setListState] = useState<ListState>({ phase: "loading" });
  const [listAttempt, setListAttempt] = useState(0);
  const [detailState, setDetailState] = useState<DetailState>({ phase: "idle" });
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const selectedRef = useRef<string | null>(null);
  const detailRequestRef = useRef(0);
  const abortRef = useRef<AbortController | null>(null);

  useEffect(() => {
    selectedRef.current = selectedId;
    abortRef.current?.abort();
    abortRef.current = null;
  }, [selectedId]);

  useEffect(() => {
    const controller = new AbortController();
    setListState({ phase: "loading" });
    listMistakeRecords({ limit: 50, offset: 0 }, controller.signal)
      .then((payload) => {
        setListState({ phase: "ready", items: payload.items, total: payload.total });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setListState({ phase: "error", message: messageOf(error) });
      });
    return () => controller.abort();
  }, [listAttempt]);

  useEffect(() => {
    if (listState.phase === "ready") onCountChange?.(listState.total);
  }, [listState, onCountChange]);

  // 桌面端进入错题本时默认打开第一条，避免右侧大面积空白；手机端保持先看列表。
  const autoSelectedRef = useRef(false);
  useEffect(() => {
    if (autoSelectedRef.current || selectedId !== null) return;
    if (listState.phase !== "ready" || listState.items.length === 0) return;
    autoSelectedRef.current = true;
    if (isDesktopLayout()) onSelectRecord(listState.items[0].id);
  }, [listState, selectedId, onSelectRecord]);

  const loadDetail = useCallback(async (recordId: string) => {
    const token = detailRequestRef.current + 1;
    detailRequestRef.current = token;
    setDetailState({ phase: "loading" });
    try {
      const detail = await getMistakeRecord(recordId);
      if (selectedRef.current === recordId && detailRequestRef.current === token) {
        setDetailState({ phase: "ready", detail });
      }
    } catch (error) {
      if (selectedRef.current === recordId && detailRequestRef.current === token) {
        setDetailState({ phase: "error", message: messageOf(error) });
      }
    }
  }, []);

  useEffect(() => {
    if (!selectedId) {
      setDetailState({ phase: "idle" });
      return;
    }
    void loadDetail(selectedId);
  }, [selectedId, loadDetail]);

  const refreshList = useCallback(async () => {
    try {
      const payload = await listMistakeRecords({ limit: 50, offset: 0 });
      setListState({ phase: "ready", items: payload.items, total: payload.total });
    } catch {
      // 保留已有列表，下一次操作或重试会再次读取。
    }
  }, []);

  return (
    <section className="page-frame mistake-page" aria-labelledby="mistake-heading">
      <header className="page-heading">
        <p className="eyebrow">个人错题档案</p>
        <h1 id="mistake-heading">
          错题本
        </h1>
        <p>回顾记录过的题目、错因和练习。</p>
      </header>

      {notice && (
        <div className="mb-4 rounded-2xl border border-amber-200 bg-amber-50/80 p-4">
          <p className="break-words text-sm leading-6 text-amber-900">{notice}</p>
        </div>
      )}

      <div className="mistake-layout">
        <div className="mistake-list">
          {listState.phase === "loading" && (
            <p className="text-sm text-slate-500">正在读取错题本…</p>
          )}
          {listState.phase === "error" && (
            <div className="rounded-xl border border-rose-200 bg-rose-50/80 p-4">
              <p className="text-sm text-rose-800">{listState.message}</p>
              <button
                type="button"
                onClick={() => setListAttempt((value) => value + 1)}
                className="mt-2 rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-xs font-medium text-rose-800"
              >
                重新读取
              </button>
            </div>
          )}
          {listState.phase === "ready" && listState.items.length === 0 && (
            <div className="rounded-xl border border-dashed border-slate-300 bg-slate-50/60 p-6 text-center">
              <p className="text-sm font-semibold text-ink">错题本还是空的</p>
              <p className="mt-1 text-sm leading-6 text-slate-600">
                在会话中完成讲解后，点“加入错题本”或输入“加入错题本”才会加入。
                自动保存会话本身不会加入错题本。
              </p>
            </div>
          )}
          {listState.phase === "ready" && listState.items.length > 0 && (
            <>
              <div className="mb-3 flex items-baseline justify-between">
                <p className="text-xs text-slate-500">共 {listState.total} 条</p>
                <button
                  type="button"
                  onClick={() => setListAttempt((value) => value + 1)}
                  className="text-xs font-medium text-river hover:underline"
                >
                  刷新
                </button>
              </div>
              <ul className="mistake-list-items">
                {listState.items.map((item) => {
                  // 标题若只是题干开头的截取，就不再重复显示摘要，直接用公式渲染后的题干作标题。
                  const titleRepeatsStatement = isStatementTitle(item.title, item.statement_excerpt);
                  return (
                  <li
                    key={item.id}
                    className="mistake-index-entry"
                  >
                    <button
                      type="button"
                      onClick={() => onSelectRecord(item.id)}
                      aria-current={selectedId === item.id ? "true" : undefined}
                      className="mistake-select"
                    >
                      <div className="flex flex-wrap items-baseline justify-between gap-2">
                        <span className={`mistake-index-title${titleRepeatsStatement ? " line-clamp-2" : ""}`}>
                          <MathText text={titleRepeatsStatement ? item.statement_excerpt : item.title} />
                        </span>
                        <span className="mistake-index-meta">
                          更新 {formatDateTime(item.updated_at)}
                        </span>
                      </div>
                      {!titleRepeatsStatement && (
                        <p className="mistake-index-meta line-clamp-2">
                          <MathText text={item.statement_excerpt} />
                        </p>
                      )}
                      <div className="mistake-index-meta mt-2 flex flex-wrap items-center gap-2">
                        {item.problem_version > 1 && (
                          <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs text-slate-500">
                            来源题干第 {item.problem_version} 版
                          </span>
                        )}
                        <span className="mistake-source-chip rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs text-slate-500">
                          {item.is_model_generated
                            ? "模型生成 · 非真题"
                            : sourceLabel(item.source)}
                        </span>
                        {item.self_reported_understood && (
                          <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-xs text-emerald-700">
                            自述已理解
                          </span>
                        )}
                      </div>
                    </button>
                  </li>
                  );
                })}
              </ul>
            </>
          )}
        </div>

        <div className="mistake-detail">
          {selectedId === null && listState.phase === "ready" && listState.items.length > 0 && (
            <p className="page-message">选择一条错题查看题干和讲解。</p>
          )}
          {selectedId !== null && (
            <MistakeDetail
              key={selectedId}
              recordId={selectedId}
              detailState={detailState}
              busy={busy}
              setBusy={setBusy}
              notice={notice}
              setNotice={setNotice}
              onBack={() => onSelectRecord(null)}
              onReload={() => void loadDetail(selectedId)}
              onChanged={(detail) => {
                setDetailState({ phase: "ready", detail });
                void refreshList();
              }}
              onDeleted={() => {
                void refreshList();
                onSelectRecord(null);
              }}
              onOpenConversation={onOpenConversation}
            />
          )}
        </div>
      </div>
    </section>
  );
}

type DetailProps = {
  recordId: string;
  detailState: DetailState;
  busy: boolean;
  setBusy: (value: boolean) => void;
  notice: string | null;
  setNotice: (value: string | null) => void;
  onBack: () => void;
  onReload: () => void;
  onChanged: (detail: MistakeRecordDetail) => void;
  onDeleted: () => void;
  onOpenConversation?: (conversationId: string) => void;
};

function MistakeDetail({
  recordId,
  detailState,
  busy,
  setBusy,
  notice,
  setNotice,
  onBack,
  onReload,
  onChanged,
  onDeleted,
  onOpenConversation,
}: DetailProps) {
  const [editTitle, setEditTitle] = useState("");
  const [editSourceType, setEditSourceType] = useState<Source["type"]>("none");
  const [editSourceText, setEditSourceText] = useState("");
  const [editPaperId, setEditPaperId] = useState("");
  const [editFeedback, setEditFeedback] = useState("");
  const [editing, setEditing] = useState(false);

  const [correcting, setCorrecting] = useState(false);
  const [statement, setStatement] = useState("");
  const [studentAnswer, setStudentAnswer] = useState("");
  const [solutionText, setSolutionText] = useState("");
  const [keepAttachments, setKeepAttachments] = useState<Set<string>>(new Set());
  const [newFiles, setNewFiles] = useState<{ file: File; role: "statement" | "solution" }[]>([]);
  const [progress, setProgress] = useState<string | null>(null);
  const [questions, setQuestions] = useState<string[]>([]);
  const [showExplanation, setShowExplanation] = useState(false);
  const [showAttempts, setShowAttempts] = useState(false);

  const controllerRef = useRef<AbortController | null>(null);

  useEffect(() => {
    setShowExplanation(false);
    setShowAttempts(false);
  }, [recordId]);

  useEffect(() => {
    if (detailState.phase !== "ready") {
      return;
    }
    const detail = detailState.detail;
    setEditTitle(detail.title);
    setEditSourceType(detail.source.type);
    setEditSourceText(detail.source.text ?? "");
    setEditPaperId(detail.source.paper_id ?? "");
    setEditFeedback(detail.feedback.text ?? "");
    setStatement(detail.statement_text);
    setStudentAnswer(detail.student_answer_text ?? "");
    setSolutionText(detail.solution_text ?? "");
    setKeepAttachments(new Set(detail.attachments.map((item) => item.id)));
    setNewFiles([]);
    setQuestions([]);
  }, [detailState]);

  useEffect(
    () => () => {
      controllerRef.current?.abort();
    },
    [],
  );

  if (detailState.phase === "loading" || detailState.phase === "idle") {
    return (
      <div className="rounded-2xl border border-slate-200 bg-white/80 p-6">
        <p className="text-sm text-slate-500">正在读取错题记录…</p>
      </div>
    );
  }
  if (detailState.phase === "error") {
    return (
      <div className="rounded-2xl border border-rose-200 bg-rose-50/80 p-5">
        <p className="text-sm font-semibold text-rose-900">没能读取错题记录</p>
        <p className="mt-1 text-sm text-rose-800">{detailState.message}</p>
        <div className="mt-3 flex gap-2">
          <button
            type="button"
            onClick={onBack}
            className="rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-xs font-medium text-rose-800"
          >
            返回列表
          </button>
          <button
            type="button"
            onClick={onReload}
            className="rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-xs font-medium text-rose-800"
          >
            重试
          </button>
        </div>
      </div>
    );
  }

  const detail = detailState.detail;

  async function saveMetadata() {
    if (busy) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const source: Source =
        editSourceType === "student_text"
          ? { type: "student_text", text: editSourceText.trim() }
          : editSourceType === "registered_paper"
            ? { type: "registered_paper", paper_id: editPaperId.trim() }
            : { type: "none" };
      const updated = await updateMistakeRecord(recordId, {
        title: editTitle.trim(),
        source,
        feedback_text: editFeedback,
      });
      onChanged(updated);
      setEditing(false);
      setNotice("记录信息已更新。");
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setBusy(false);
    }
  }

  async function markUnderstood() {
    if (busy) {
      return;
    }
    const next = !detail.self_reported_understood;
    setBusy(true);
    setNotice(null);
    try {
      const updated = await updateMistakeRecord(recordId, {
        self_reported_understood: next,
      });
      onChanged(updated);
      setNotice(
        next
          ? "已记录你的自述：这次听懂了。"
          : "已撤回“我懂了”的自述；记录不再显示为已理解。",
      );
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setBusy(false);
    }
  }

  async function adoptNewer() {
    if (!detail.newer_problem || busy) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const updated = await adoptProblem(
        recordId,
        detail.newer_problem.id,
        createRequestId(),
      );
      onChanged(updated);
      setNotice("已采用会话中的新版题目和讲解；旧讲解保存在历史版本中。");
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setBusy(false);
    }
  }

  async function deleteRecord() {
    if (busy) {
      return;
    }
    const confirmed = window.confirm(
      "只删除这条错题记录和它的历史版本，不删除原会话、轮次和图片。确定删除？",
    );
    if (!confirmed) {
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      await deleteMistakeRecord(recordId);
      setNotice("记录已删除；原会话和图片仍保留。");
      onDeleted();
    } catch (error) {
      setNotice(messageOf(error));
    } finally {
      setBusy(false);
    }
  }

  async function applyCorrection() {
    if (busy || !statement.trim()) {
      return;
    }
    const controller = new AbortController();
    controllerRef.current = controller;
    setBusy(true);
    setNotice(null);
    setQuestions([]);
    setProgress("正在准备纠正…");
    try {
      const uploaded: { id: string; role: "statement" | "solution" }[] = [];
      for (const item of newFiles) {
        const meta = await uploadAttachment(detail.conversation_id, item.file, controller.signal);
        uploaded.push({ id: meta.id, role: item.role });
      }
      const kept = detail.attachments
        .filter((item) => keepAttachments.has(item.id))
        .map((item) => ({ id: item.id, role: item.role }));
      const result = await streamRecordCorrection({
        recordId,
        requestId: createRequestId(),
        statementText: statement,
        attachments: [...kept, ...uploaded],
        studentAnswer,
        solutionText,
        signal: controller.signal,
        onProgress: (event) => setProgress(event.message),
      });
      setProgress(null);
      if (result.kind === "record_needs_clarification") {
        setQuestions(result.questions);
        setNotice("这次纠正还不清楚，记录没有改动；请补充后再试。");
        return;
      }
      onChanged(result.record);
      setCorrecting(false);
      setNotice(
        result.revision_created
          ? "记录已更新，旧讲解已归档为历史版本。"
          : "记录已更新。",
      );
    } catch (error) {
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }
      setNotice(messageOf(error));
    } finally {
      setProgress(null);
      setBusy(false);
    }
  }

  function addCorrectionFiles(files: FileList | null, role: "statement" | "solution") {
    const selected = Array.from(files ?? []);
    const accepted: { file: File; role: "statement" | "solution" }[] = [];
    for (const file of selected) {
      const error = validateLocalImage(file);
      if (error) {
        setNotice(error);
        continue;
      }
      accepted.push({ file, role });
    }
    setNewFiles((current) => [...current, ...accepted].slice(0, 4));
  }

  return (
    <div className="space-y-4">
      <div className="mistake-toolbar">
        {/* 桌面端左侧列表常驻，返回按钮只在手机单栏布局显示（见 styles.css .mistake-back） */}
        <button
          type="button"
          onClick={onBack}
          className="mistake-back rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
        >
          ← 返回错题列表
        </button>
        <div className="mistake-actions">
          <button
            type="button"
            onClick={() => void markUnderstood()}
            disabled={busy}
            className="rounded-lg border border-emerald-300 bg-white px-3 py-1.5 text-xs font-medium text-emerald-800 disabled:opacity-60"
          >
            {detail.self_reported_understood ? "撤回自述" : "我懂了"}
          </button>
          <button
            type="button"
            onClick={() => setCorrecting((value) => !value)}
            className="rounded-lg border border-river/40 bg-white px-3 py-1.5 text-xs font-medium text-river"
          >
            纠正题干
          </button>
          <details className="mistake-more">
            <summary className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700">
              更多
            </summary>
            <div className="mistake-more-menu">
              <button
                type="button"
                onClick={() => setEditing((value) => !value)}
                className="text-xs font-medium text-slate-700"
              >
                编辑信息
              </button>
              <button
                type="button"
                onClick={() => void deleteRecord()}
                disabled={busy}
                className="text-xs font-medium text-rose-700 disabled:opacity-60"
              >
                删除记录
              </button>
            </div>
          </details>
        </div>
      </div>

      {notice && (
        <div className="rounded-xl border border-amber-200 bg-amber-50/80 p-3">
          <p className="break-words text-sm leading-6 text-amber-900">{notice}</p>
        </div>
      )}

      {editing && (
        <div className="rounded-2xl border border-slate-200 bg-white p-4">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-xs text-slate-600">
              标题
              <input
                value={editTitle}
                onChange={(event) => setEditTitle(event.target.value)}
                maxLength={80}
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5 text-sm"
              />
            </label>
            <label className="text-xs text-slate-600">
              来源类型
              <select
                value={editSourceType}
                onChange={(event) => setEditSourceType(event.target.value as Source["type"])}
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5 text-sm"
              >
                <option value="none">不关联来源</option>
                <option value="student_text">学生自述（未核实）</option>
                <option value="registered_paper">馆藏试卷</option>
              </select>
            </label>
            {editSourceType === "student_text" && (
              <label className="text-xs text-slate-600 sm:col-span-2">
                自述来源
                <input
                  value={editSourceText}
                  onChange={(event) => setEditSourceText(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5 text-sm"
                  placeholder="例如：2023 期中，第 5 题（学生自述，未核实）"
                />
              </label>
            )}
            {editSourceType === "registered_paper" && (
              <label className="text-xs text-slate-600 sm:col-span-2">
                试卷编号
                <input
                  value={editPaperId}
                  onChange={(event) => setEditPaperId(event.target.value)}
                  className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5 text-sm"
                  placeholder="目录中登记的稳定 ID"
                />
              </label>
            )}
            <label className="text-xs text-slate-600 sm:col-span-2">
              反馈
              <textarea
                value={editFeedback}
                onChange={(event) => setEditFeedback(event.target.value)}
                rows={2}
                maxLength={4000}
                className="mt-1 w-full rounded-lg border border-slate-300 px-2 py-1.5 text-sm"
              />
            </label>
          </div>
          <button
            type="button"
            onClick={() => void saveMetadata()}
            disabled={busy}
            className="mt-3 rounded-lg bg-river px-4 py-2 text-xs font-medium text-white disabled:bg-slate-400"
          >
            保存信息
          </button>
        </div>
      )}

      {correcting && (
        <div className="rounded-2xl border border-river/30 bg-white p-4">
          <p className="text-sm font-semibold text-ink">纠正题干并重新讲解</p>
          <p className="mt-1 text-xs leading-5 text-slate-500">
            纠正会重新识别题目并生成新讲解；旧讲解以历史版本保留，不再作为当前题干的答案。
            原会话不会被改写。
          </p>
          <textarea
            value={statement}
            onChange={(event) => setStatement(event.target.value)}
            rows={4}
            maxLength={4000}
            className="mt-2 w-full rounded-lg border border-slate-300 px-3 py-2 text-sm"
          />
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <textarea
              value={studentAnswer}
              onChange={(event) => setStudentAnswer(event.target.value)}
              rows={2}
              maxLength={2000}
              placeholder="学生答案（可选）"
              className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
            />
            <textarea
              value={solutionText}
              onChange={(event) => setSolutionText(event.target.value)}
              rows={2}
              maxLength={4000}
              placeholder="解题过程（可选）"
              className="rounded-lg border border-slate-300 px-3 py-2 text-xs"
            />
          </div>
          {detail.attachments.length > 0 && (
            <div className="mt-3">
              <p className="text-xs text-slate-600">保留的图片</p>
              <div className="mt-1 flex flex-wrap gap-2">
                {detail.attachments.map((item) => (
                  <label
                    key={item.id}
                    className="flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-xs"
                  >
                    <input
                      type="checkbox"
                      checked={keepAttachments.has(item.id)}
                      onChange={(event) => {
                        setKeepAttachments((current) => {
                          const next = new Set(current);
                          if (event.target.checked) {
                            next.add(item.id);
                          } else {
                            next.delete(item.id);
                          }
                          return next;
                        });
                      }}
                    />
                    {item.role === "solution" ? "解法图" : "题干图"}
                  </label>
                ))}
              </div>
            </div>
          )}
          <div className="mt-2 flex flex-wrap items-center gap-2">
            <label className="cursor-pointer rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700">
              添加题干图
              <input
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp"
                multiple
                className="hidden"
                onChange={(event) => {
                  addCorrectionFiles(event.target.files, "statement");
                  event.target.value = "";
                }}
              />
            </label>
            <label className="cursor-pointer rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700">
              添加解法图
              <input
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp"
                multiple
                className="hidden"
                onChange={(event) => {
                  addCorrectionFiles(event.target.files, "solution");
                  event.target.value = "";
                }}
              />
            </label>
            {newFiles.length > 0 && (
              <span className="text-xs text-slate-500">
                新增 {newFiles.length} 张
              </span>
            )}
            <button
              type="button"
              onClick={() => void applyCorrection()}
              disabled={busy || !statement.trim()}
              className="ml-auto rounded-lg bg-river px-4 py-2 text-xs font-medium text-white disabled:bg-slate-400"
            >
              {busy ? "重新讲解中…" : "保存并重新讲解"}
            </button>
          </div>
          {progress && <p className="mt-2 text-xs text-slate-500">{progress}</p>}
          {questions.length > 0 && (
            <ul className="mt-2 list-disc space-y-1 pl-5 text-xs text-amber-800">
              {questions.map((question) => (
                <li key={question}>{question}</li>
              ))}
            </ul>
          )}
        </div>
      )}

      {detail.conversation_has_newer_version && detail.newer_problem && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50/80 p-4">
          <p className="text-sm text-amber-900">
            原会话中已有第 {detail.newer_problem.version} 版题干和讲解。
          </p>
          <button
            type="button"
            onClick={() => void adoptNewer()}
            disabled={busy}
            className="mt-2 rounded-lg border border-amber-300 bg-white px-3 py-1.5 text-xs font-medium text-amber-900 disabled:opacity-60"
          >
            采用新版更新记录
          </button>
        </div>
      )}

      <div className="lesson-sheet">
        <div className="flex flex-wrap items-start justify-between gap-2">
          {!isStatementTitle(detail.title, detail.statement_text) && (
            <h3 className="min-w-0 break-words text-base font-semibold text-ink">
              <MathText text={detail.title} />
            </h3>
          )}
          <div className="flex flex-wrap gap-2">
            {(detail.revisions.length > 0 || detail.problem_version > 1) && (
              <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-xs text-slate-500">
                记录第 {detail.revisions.length + 1} 版 · 来源题干第 {detail.problem_version} 版
              </span>
            )}
            <SourceBadge
              source={detail.source}
              isModelGenerated={detail.is_model_generated}
            />
          </div>
        </div>

        {detail.primary_exercise?.knowledge_point &&
          !detail.title.includes(detail.primary_exercise.knowledge_point) && (
          <p className="mt-1.5 text-xs font-medium text-river">
            考点：<MathText text={detail.primary_exercise.knowledge_point} />
          </p>
        )}

        <p className="lesson-statement">
          <MathText text={detail.statement_text} />
        </p>

        {detail.attachments.length > 0 && (
          <div className="mt-3">
            <AttachmentThumbnails
              attachments={detail.attachments}
              conversationId={detail.conversation_id}
            />
          </div>
        )}

        {detail.primary_exercise ? (
          (() => {
            const primaryEx = detail.primary_exercise;
            const attempts = primaryEx.attempts ?? [];
            const latestAttempt = attempts.length ? attempts[attempts.length - 1] : null;
            const latestAnswer =
              latestAttempt?.student_answer_text ?? primaryEx.student_answer_text ?? detail.student_answer_text;
            const latestProcess =
              latestAttempt?.student_solution_text ?? primaryEx.student_solution_text ?? detail.solution_text;
            const latestAttachments =
              latestAttempt?.attachments?.length
                ? latestAttempt.attachments
                : primaryEx.attachments ?? [];
            const latestEvaluation =
              latestAttempt?.evaluation ?? primaryEx.evaluation;
            const latestAfterReveal =
              latestAttempt?.answered_after_revealed ?? primaryEx.answered_after_revealed;
            const statusInfo = resolveExerciseAttemptStatus(primaryEx);
            const solutionUnlocked =
              Boolean(primaryEx.revealed_solution_at) ||
              primaryEx.status === "revealed" ||
              Boolean(primaryEx.evaluation && !primaryEx.evaluation.needs_supplement);
            const allowedRefSolution = solutionUnlocked
              ? primaryEx.reference_solution_text ?? null
              : null;

            return (
              <div className="mt-4 space-y-3">
                <div className="rounded-xl border border-slate-200 bg-slate-50/70 p-3 text-xs leading-6">
                  <div className="flex flex-wrap items-center justify-between gap-2">
                    <span className="font-medium text-slate-700">当前作答情况</span>
                    <span className="rounded-full border border-slate-200 bg-white px-2 py-0.5 text-[11px] font-medium text-slate-600">
                      {statusInfo.label}
                    </span>
                  </div>
                  {latestAnswer && (
                    <p className="mt-2 whitespace-pre-wrap text-slate-800">
                      <span className="font-medium text-slate-600">你的答案：</span>
                      <MathText text={latestAnswer} />
                    </p>
                  )}
                  {latestProcess && (
                    latestProcess.trim().length > 120 ? (
                      <details className="mt-2 rounded-lg border border-slate-200/80 bg-white/80 p-2">
                        <summary className="cursor-pointer font-medium text-slate-600">
                          解题过程（点击展开）
                        </summary>
                        <p className="mt-1 whitespace-pre-wrap text-slate-700">
                          <MathText text={latestProcess} />
                        </p>
                      </details>
                    ) : (
                      <p className="mt-1.5 whitespace-pre-wrap text-slate-700">
                        <span className="font-medium text-slate-600">解题过程：</span>
                        <MathText text={latestProcess} />
                      </p>
                    )
                  )}
                  {latestAttachments.length > 0 && (
                    <div className="mt-2">
                      <AttachmentThumbnails
                        attachments={latestAttachments}
                        conversationId={detail.conversation_id}
                      />
                    </div>
                  )}
                  {latestEvaluation && (
                    <div className="mt-2.5">
                      <EvaluationBlock
                        evaluation={latestEvaluation}
                        answeredAfterRevealed={latestAfterReveal}
                        studentAnswerText={null}
                        referenceSolutionText={allowedRefSolution}
                        hasProcess={Boolean(
                          latestProcess?.trim() || latestAttachments.length > 0,
                        )}
                      />
                    </div>
                  )}
                </div>

                <div className="flex flex-wrap items-center gap-2">
                  <button
                    type="button"
                    onClick={() => setShowExplanation((value) => !value)}
                    aria-expanded={showExplanation}
                    className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  >
                    {showExplanation ? "收起解析" : "查看解析"}
                  </button>
                  <button
                    type="button"
                    onClick={() => setShowAttempts((value) => !value)}
                    aria-expanded={showAttempts}
                    className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-50"
                  >
                    {attempts.length > 0 ? `作答记录（${attempts.length}）` : "作答记录"}
                  </button>
                  {onOpenConversation && detail.conversation_id && (
                    <button
                      type="button"
                      onClick={() => onOpenConversation(detail.conversation_id)}
                      className="rounded-lg border border-river/40 bg-white px-3 py-1.5 text-xs font-medium text-river hover:bg-slate-50"
                    >
                      打开原会话
                    </button>
                  )}
                </div>

                {showExplanation && (
                  <div className="rounded-xl border border-slate-200 bg-white p-3">
                    {detail.explanation ? (
                      <ExplanationBlock detail={detail} />
                    ) : allowedRefSolution ? (
                      <div>
                        <h4 className="text-xs font-semibold text-slate-600">参考解答</h4>
                        <div className="mt-2 whitespace-pre-wrap text-sm leading-6 text-slate-700">
                          <MathText text={allowedRefSolution} />
                        </div>
                      </div>
                    ) : (
                      <p className="text-xs leading-5 text-slate-600">
                        参考解答尚未解锁，请先在原会话完成独立作答或查看参考解答后再查看解析。
                      </p>
                    )}
                  </div>
                )}

                {showAttempts && (
                  <div className="rounded-xl border border-slate-200 bg-white p-3">
                    <h4 className="text-xs font-semibold text-slate-600">作答记录</h4>
                    {attempts.length === 0 ? (
                      <p className="mt-1.5 text-xs text-slate-500">暂无作答记录。</p>
                    ) : (
                      <ul className="mt-2 space-y-2.5">
                        {attempts.map((attempt) => (
                          <li
                            key={attempt.id}
                            className="rounded-lg border border-slate-200 bg-slate-50/60 p-2.5 text-xs leading-6"
                          >
                            <div className="flex flex-wrap items-center justify-between gap-2 text-slate-500">
                              <span className="font-medium text-slate-700">
                                第 {attempt.attempt_no} 次作答 ·{" "}
                                {KIND_LABELS[attempt.kind] ?? attempt.kind}
                              </span>
                              <span>{formatDateTime(attempt.created_at)}</span>
                            </div>
                            {attempt.student_answer_text && (
                              <p className="mt-1 text-slate-800">
                                你的答案：<MathText text={attempt.student_answer_text} />
                              </p>
                            )}
                            {attempt.student_solution_text && (
                              <p className="mt-1 whitespace-pre-wrap text-slate-700">
                                过程：<MathText text={attempt.student_solution_text} />
                              </p>
                            )}
                            {attempt.attachments.length > 0 && (
                              <div className="mt-1.5">
                                <AttachmentThumbnails
                                  attachments={attempt.attachments}
                                  conversationId={detail.conversation_id}
                                />
                              </div>
                            )}
                            {attempt.evaluation && (
                              <div className="mt-2">
                                <EvaluationBlock
                                  evaluation={attempt.evaluation}
                                  answeredAfterRevealed={attempt.answered_after_revealed}
                                  studentAnswerText={attempt.student_answer_text}
                                  referenceSolutionText={allowedRefSolution}
                                  hasProcess={Boolean(
                                    attempt.student_solution_text?.trim() ||
                                      attempt.attachments.length > 0,
                                  )}
                                />
                              </div>
                            )}
                          </li>
                        ))}
                      </ul>
                    )}
                  </div>
                )}
              </div>
            );
          })()
        ) : (
          <>
            {(detail.student_answer_text || detail.solution_text) && (
              <div className="student-work">
                <p className="text-xs font-medium text-amber-900">学生解法</p>
                {detail.student_answer_text && (
                  <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-950">
                    答案：<MathText text={detail.student_answer_text} />
                  </p>
                )}
                {detail.solution_text && (
                  <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-950">
                    过程：<MathText text={detail.solution_text} />
                  </p>
                )}
              </div>
            )}

            {detail.explanation && <ExplanationBlock detail={detail} />}

            {onOpenConversation && detail.conversation_id && (
              <div className="mt-3">
                <button
                  type="button"
                  onClick={() => onOpenConversation(detail.conversation_id)}
                  className="rounded-lg border border-river/40 bg-white px-3 py-1.5 text-xs font-medium text-river hover:bg-slate-50"
                >
                  打开原会话
                </button>
              </div>
            )}
          </>
        )}

        <details className="mt-4 rounded-xl border border-slate-200 bg-slate-50/50 p-3">
          <summary className="cursor-pointer text-xs font-medium text-slate-600">
            更多记录（日期、追问与历史版本）
          </summary>
          <div className="mt-3 space-y-3">
            <p className="text-xs text-slate-500">
              创建 {formatDateTime(detail.created_at)} · 更新 {formatDateTime(detail.updated_at)}
            </p>

            {detail.followups.length > 0 && (
              <div>
                <h4 className="text-xs font-semibold text-slate-500">追问记录</h4>
                <ul className="mt-2 space-y-2">
                  {detail.followups.map((item, index) => (
                    <li key={`${item.created_at}-${index}`} className="rounded-xl bg-white p-3">
                      <p className="text-xs font-medium text-slate-700">问：{item.question}</p>
                      <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-600">
                        答：<MathText text={item.answer} />
                      </p>
                    </li>
                  ))}
                </ul>
              </div>
            )}

            {(detail.feedback.text || detail.self_reported_understood) && (
              <div className="rounded-xl border border-slate-200 bg-white p-3">
                <p className="text-xs font-medium text-slate-600">反馈</p>
                {detail.feedback.text && (
                  <p className="mt-1 text-xs leading-6 text-slate-700">
                    {detail.feedback.text}
                  </p>
                )}
                {detail.self_reported_understood && (
                  <p className="mt-1 text-xs text-slate-500">
                    已自述理解
                  </p>
                )}
              </div>
            )}

            {detail.revisions.length > 0 && (
              <details className="rounded-xl border border-slate-200 bg-white p-3">
                <summary className="cursor-pointer text-xs font-medium text-slate-700">
                  历史版本（{detail.revisions.length}）
                </summary>
                <ul className="mt-2 space-y-2">
                  {detail.revisions.map((revision) => (
                    <RevisionItem key={revision.id} revision={revision} />
                  ))}
                </ul>
              </details>
            )}
          </div>
        </details>

        {detail.exercises && detail.exercises.length > 0 && (
          <details className="mt-3 rounded-xl border border-indigo-200 bg-indigo-50/30 p-3">
            <summary className="cursor-pointer text-xs font-medium text-indigo-900">
              关联类似练习（{detail.exercises.length}）
            </summary>
            <div className="mt-3 space-y-3">
              {detail.exercises.map((ex) => {
                const attempts = ex.attempts ?? [];
                const latest = attempts.length ? attempts[attempts.length - 1] : null;
                const answer = latest?.student_answer_text ?? ex.student_answer_text;
                const evaluation = latest?.evaluation ?? ex.evaluation;
                const afterReveal =
                  latest?.answered_after_revealed ?? ex.answered_after_revealed;
                const statusInfo = resolveExerciseAttemptStatus(ex);
                const exAttachments = latest?.attachments?.length
                  ? latest.attachments
                  : ex.attachments ?? [];
                return (
                <div
                  key={ex.id}
                  className="rounded-xl border border-slate-200 bg-white p-3 text-xs leading-6 shadow-sm"
                >
                  <div className="flex items-center justify-between text-xs text-slate-500">
                    <span className="font-semibold text-indigo-700">
                      {ex.knowledge_point ? <>考点：<MathText text={ex.knowledge_point} /></> : "AI 类似题"}
                    </span>
                    <span>
                      {attempts.length > 1
                        ? `已作答 ${attempts.length} 次`
                        : statusInfo.label}
                    </span>
                  </div>
                  <p className="mt-1 text-slate-800"><MathText text={ex.statement_text} /></p>
                  {answer && (
                    <p className="mt-1 text-slate-600">
                      你的答案：<span className="text-slate-900"><MathText text={answer} /></span>
                    </p>
                  )}
                  {exAttachments.length > 0 && (
                    <div className="mt-1.5">
                      <AttachmentThumbnails
                        attachments={exAttachments}
                        conversationId={detail.conversation_id}
                      />
                    </div>
                  )}
                  {afterReveal && (
                    <p className="mt-1 text-[11px] font-medium text-amber-800">
                      已看解答后作答
                    </p>
                  )}
                  {evaluation && (
                    <div className="mt-2 rounded-lg bg-slate-50 p-2 text-xs text-slate-700">
                      <span className="font-medium">评价：</span>
                      {evaluation.final_answer_assessment}
                      {latest && (
                        <span className="ml-1 text-slate-500">
                          （第 {latest.attempt_no} 次 ·{" "}
                          {latest.kind === "supplement"
                            ? "补充过程"
                            : latest.kind === "correction"
                              ? "订正"
                              : "首次作答"}）
                        </span>
                      )}
                    </div>
                  )}
                </div>
                );
              })}
            </div>
          </details>
        )}
      </div>
    </div>
  );
}

function ExplanationBlock({ detail }: { detail: MistakeRecordDetail }) {
  if (!detail.explanation) {
    return null;
  }
  const explanation = detail.explanation;
  return (
    <div className="mt-4">
      <h4 className="text-xs font-semibold text-slate-500">讲解</h4>
      <div className="mt-2 space-y-2">
        {explanation.knowledge_points.map((point) => (
          <p key={point.name} className="text-sm leading-6 text-slate-700">
            <span className="font-medium text-ink"><MathText text={point.name} />：</span>
            <MathText text={point.explanation} />
          </p>
        ))}
      </div>
      <ol className="derivation">
        {explanation.steps.map((step) => (
          <li key={step.index}>
            <span className="step-number">STEP {String(step.index).padStart(2, "0")}</span>
            <p className="step-title"><MathText text={step.title} /></p>
            <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-700">
              <MathText text={step.content} />
            </p>
          </li>
        ))}
      </ol>
      <p className="lesson-answer">
        最终答案：<MathText text={explanation.final_answer} />
      </p>
      {explanation.error_analysis && (
        <p className="red-ink">
          <MathText text={explanation.error_analysis} />
        </p>
      )}
    </div>
  );
}

function RevisionItem({ revision }: { revision: RevisionView }) {
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-2">
      <p className="text-xs text-slate-500">
        {formatDateTime(revision.created_at)} · {revision.reason === "adopt_problem" ? "采用新版" : revision.reason === "correction" ? "纠正题干" : "重新讲解"}
      </p>
      <p className="mt-1 line-clamp-2 text-xs text-slate-700">
        <MathText text={revision.statement_text} />
      </p>
      {revision.explanation && (
        <p className="mt-1 text-xs text-slate-500">
          旧讲解最终答案：<MathText text={revision.explanation.final_answer} />
        </p>
      )}
    </li>
  );
}

