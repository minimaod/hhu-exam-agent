import { useCallback, useEffect, useRef, useState } from "react";
import SourceBadge from "./SourceBadge";
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

export default function MistakeBook({ selectedId, onSelectRecord }: MistakeBookProps) {
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
    <section className="mt-8" aria-labelledby="mistake-heading">
      <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
        <h2 id="mistake-heading" className="text-sm font-semibold text-ink">
          错题本
        </h2>
        <p className="text-[11px] text-slate-500">
          只有明确“记录这题”才会进入这里；删除记录不会删除原会话和图片
        </p>
      </div>

      {notice && (
        <div className="mb-4 rounded-2xl border border-amber-200 bg-amber-50/80 p-4">
          <p className="break-words text-sm leading-6 text-amber-900">{notice}</p>
        </div>
      )}

      {selectedId === null && (
        <div className="rounded-2xl border border-slate-200/90 bg-white/85 p-4 shadow-card">
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
                在会话中完成讲解后，点“记录这题”或输入“记录这题”才会加入。
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
              <ul className="space-y-2">
                {listState.items.map((item) => (
                  <li
                    key={item.id}
                    className="rounded-xl border border-slate-200 bg-white transition hover:border-river/40"
                  >
                    <button
                      type="button"
                      onClick={() => onSelectRecord(item.id)}
                      className="w-full rounded-t-xl p-4 text-left hover:bg-river/5"
                    >
                      <div className="flex flex-wrap items-baseline justify-between gap-2">
                        <span className="truncate text-sm font-semibold text-ink">
                          {item.title}
                        </span>
                        <span className="text-[11px] text-slate-400">
                          更新 {formatDateTime(item.updated_at)}
                        </span>
                      </div>
                      <p className="mt-1 line-clamp-2 text-xs leading-6 text-slate-600">
                        {item.statement_excerpt}
                      </p>
                      <div className="mt-2 flex flex-wrap items-center gap-2">
                        <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] text-slate-500">
                          第 {item.problem_version} 版
                        </span>
                        <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] text-slate-500">
                          {sourceLabel(item.source)}
                        </span>
                        {item.self_reported_understood && (
                          <span className="rounded-full border border-emerald-200 bg-emerald-50 px-2 py-0.5 text-[10px] text-emerald-700">
                            自述已理解
                          </span>
                        )}
                      </div>
                    </button>
                    <div className="flex flex-wrap items-center justify-between gap-2 border-t border-slate-100 px-4 py-2">
                      <span className="text-[10px] text-slate-400">
                        记录于 {formatDateTime(item.created_at)}
                      </span>
                      <button
                        type="button"
                        onClick={() => onSelectRecord(item.id)}
                        className="text-[11px] font-medium text-river hover:underline"
                      >
                        查看完整记录
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            </>
          )}
        </div>
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
        />
      )}
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

  const controllerRef = useRef<AbortController | null>(null);

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
          ? "已记录你的自述：这次听懂了。它不代表系统认定已永久掌握。"
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
      <div className="flex flex-wrap items-center justify-between gap-2">
        <button
          type="button"
          onClick={onBack}
          className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
        >
          ← 返回错题列表
        </button>
        <div className="flex flex-wrap gap-2">
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
            onClick={() => setEditing((value) => !value)}
            className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700"
          >
            编辑信息
          </button>
          <button
            type="button"
            onClick={() => setCorrecting((value) => !value)}
            className="rounded-lg border border-river/40 bg-white px-3 py-1.5 text-xs font-medium text-river"
          >
            纠正题干并重新讲解
          </button>
          <button
            type="button"
            onClick={() => void deleteRecord()}
            disabled={busy}
            className="rounded-lg border border-rose-300 bg-white px-3 py-1.5 text-xs font-medium text-rose-700 disabled:opacity-60"
          >
            删除记录
          </button>
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
                <option value="registered_paper">已登记试卷（按 paper_id 校验）</option>
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
                paper_id
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
                    className="flex items-center gap-1 rounded-lg border border-slate-200 bg-slate-50 px-2 py-1 text-[11px]"
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
              <span className="text-[11px] text-slate-500">
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

      <div className="rounded-2xl border border-slate-200 bg-white/95 p-5">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <h3 className="text-base font-semibold text-ink">{detail.title}</h3>
          <div className="flex flex-wrap gap-2">
            <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-500">
              第 {detail.problem_version} 版
            </span>
            <SourceBadge source={detail.source} />
          </div>
        </div>
        <p className="mt-3 whitespace-pre-wrap break-words text-sm leading-7 text-ink">
          {detail.statement_text}
        </p>
        {detail.attachments.length > 0 && (
          <div className="mt-3 grid grid-cols-2 gap-2 sm:grid-cols-3">
            {detail.attachments.map((image) => (
              <a
                key={image.id}
                href={image.url}
                target="_blank"
                rel="noreferrer"
                className="block overflow-hidden rounded-xl border border-slate-200"
              >
                <img
                  src={image.url}
                  alt={image.role === "solution" ? "解法图片" : "题目图片"}
                  className="h-32 w-full object-cover"
                />
              </a>
            ))}
          </div>
        )}

        {(detail.student_answer_text || detail.solution_text) && (
          <div className="mt-3 rounded-xl border border-amber-200 bg-amber-50/60 p-3">
            <p className="text-[11px] font-medium text-amber-900">学生解法</p>
            {detail.student_answer_text && (
              <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-950">
                答案：{detail.student_answer_text}
              </p>
            )}
            {detail.solution_text && (
              <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-amber-950">
                过程：{detail.solution_text}
              </p>
            )}
          </div>
        )}

        {detail.explanation && <ExplanationBlock detail={detail} />}

        {detail.followups.length > 0 && (
          <div className="mt-4">
            <h4 className="text-xs font-semibold text-slate-500">追问记录</h4>
            <ul className="mt-2 space-y-2">
              {detail.followups.map((item, index) => (
                <li key={`${item.created_at}-${index}`} className="rounded-xl bg-slate-50 p-3">
                  <p className="text-xs font-medium text-slate-700">问：{item.question}</p>
                  <p className="mt-1 whitespace-pre-wrap text-xs leading-6 text-slate-600">
                    答：{item.answer}
                  </p>
                </li>
              ))}
            </ul>
          </div>
        )}

        <div className="mt-4 rounded-xl border border-slate-200 bg-slate-50/60 p-3">
          <p className="text-[11px] font-medium text-slate-600">反馈</p>
          <p className="mt-1 text-xs leading-6 text-slate-700">
            {detail.feedback.text ?? "暂无文字反馈"}
          </p>
          <p className="mt-1 text-[11px] text-slate-500">
            {detail.self_reported_understood
              ? "学生自述已理解；不代表系统认定已永久掌握。"
              : "学生尚未记录“我懂了”。"}
          </p>
        </div>

        <p className="mt-3 text-[11px] text-slate-400">
          创建 {formatDateTime(detail.created_at)} · 更新 {formatDateTime(detail.updated_at)}
        </p>

        {detail.revisions.length > 0 && (
          <details className="mt-3 rounded-xl border border-slate-200 bg-slate-50/60 p-3">
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

        {detail.exercises && detail.exercises.length > 0 && (
          <details open className="mt-3 rounded-xl border border-indigo-200 bg-indigo-50/30 p-3">
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
                return (
                <div
                  key={ex.id}
                  className="rounded-xl border border-slate-200 bg-white p-3 text-xs leading-6 shadow-sm"
                >
                  <div className="flex items-center justify-between text-[11px] text-slate-500">
                    <span className="font-semibold text-indigo-700">
                      {ex.knowledge_point ? `考点：${ex.knowledge_point}` : "AI 类似题"}
                    </span>
                    <span>
                      {attempts.length > 1
                        ? `已作答 ${attempts.length} 次`
                        : ex.status === "submitted"
                          ? "已作答"
                          : ex.status === "revealed"
                            ? "已看解答"
                            : "未提交"}
                    </span>
                  </div>
                  <p className="mt-1 text-slate-800">{ex.statement_text}</p>
                  {answer && (
                    <p className="mt-1 text-slate-600">
                      你的答案：<span className="text-slate-900">{answer}</span>
                    </p>
                  )}
                  {afterReveal && (
                    <p className="mt-1 rounded bg-amber-50 p-1.5 text-[11px] text-amber-800">
                      这次作答是在参考解答展示后完成的，答案正确也不代表独立掌握。
                    </p>
                  )}
                  {evaluation && (
                    <div className="mt-2 rounded-lg bg-slate-50 p-2 text-[11px] text-slate-700">
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
            <span className="font-medium text-ink">{point.name}：</span>
            {point.explanation}
          </p>
        ))}
      </div>
      <ol className="mt-3 space-y-2">
        {explanation.steps.map((step) => (
          <li key={step.index} className="rounded-xl bg-slate-50 p-3">
            <p className="text-sm font-medium text-ink">
              {step.index}. {step.title}
            </p>
            <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-slate-700">
              {step.content}
            </p>
          </li>
        ))}
      </ol>
      <p className="mt-3 rounded-xl border border-emerald-200 bg-emerald-50/70 p-3 text-sm text-emerald-950">
        最终答案：{explanation.final_answer}
      </p>
      {explanation.error_analysis && (
        <p className="mt-2 rounded-xl border border-rose-200 bg-rose-50/80 p-3 text-sm leading-6 text-rose-950">
          {explanation.error_analysis}
        </p>
      )}
    </div>
  );
}

function RevisionItem({ revision }: { revision: RevisionView }) {
  return (
    <li className="rounded-lg border border-slate-200 bg-white p-2">
      <p className="text-[11px] text-slate-500">
        {formatDateTime(revision.created_at)} · {revision.reason === "adopt_problem" ? "采用新版" : revision.reason === "correction" ? "纠正题干" : "重新讲解"}
      </p>
      <p className="mt-1 line-clamp-2 text-xs text-slate-700">{revision.statement_text}</p>
      {revision.explanation && (
        <p className="mt-1 text-[11px] text-slate-500">
          旧讲解最终答案：{revision.explanation.final_answer}
        </p>
      )}
    </li>
  );
}

