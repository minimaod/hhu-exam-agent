import { useRef, type ChangeEvent, type ClipboardEvent, type KeyboardEvent } from "react";
import type {
  AttachmentRole,
  ProblemView,
} from "../lib/conversationsApi";
import type { PaperItem } from "../lib/papersApi";

export type PendingFile = {
  file: File;
  previewUrl: string;
  role: AttachmentRole;
};

export type ComposerMode = "auto" | "supplement" | "correction" | "new";

type TurnComposerProps = {
  value: string;
  running: boolean;
  disabled: boolean;
  disabledReason?: "no-conversation" | "loading" | null;
  confirmedCourse?: string | null;
  activeProblem: ProblemView | null;
  mode: ComposerMode;
  files: PendingFile[];
  studentAnswer: string;
  solutionText: string;
  sourcePaperId: string;
  sourceOptions: PaperItem[];
  onChange: (value: string) => void;
  onModeChange: (mode: ComposerMode) => void;
  onAddFiles: (files: File[], role: AttachmentRole) => void;
  onRemoveFile: (index: number) => void;
  onStudentAnswerChange: (value: string) => void;
  onSolutionTextChange: (value: string) => void;
  onSourceChange: (paperId: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
};

const GENERIC_EXAMPLES = ["帮我找期末试卷", "这题怎么做", "来一道练习题"];

function examplesFor(course: string | null | undefined): string[] {
  const trimmed = course?.trim();
  if (trimmed) {
    return [`查看${trimmed}试卷`, "来一道类似的题", "这题怎么做"];
  }
  return GENERIC_EXAMPLES;
}

function roleLabel(role: AttachmentRole): string {
  if (role === "solution") {
    return "解法图";
  }
  if (role === "supplement") {
    return "补充图";
  }
  return "题干图";
}

export default function TurnComposer({
  value,
  running,
  disabled,
  disabledReason = null,
  confirmedCourse = null,
  activeProblem,
  mode,
  files,
  studentAnswer,
  solutionText,
  sourcePaperId,
  sourceOptions,
  onChange,
  onModeChange,
  onAddFiles,
  onRemoveFile,
  onStudentAnswerChange,
  onSolutionTextChange,
  onSourceChange,
  onSubmit,
  onCancel,
}: TurnComposerProps) {
  function handleFiles(
    event: ChangeEvent<HTMLInputElement>,
    role: AttachmentRole,
  ) {
    const selected = Array.from(event.target.files ?? []);
    event.target.value = "";
    if (selected.length) {
      onAddFiles(selected, role);
    }
  }

  function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const images = Array.from(event.clipboardData.files).filter((file) =>
      file.type.startsWith("image/"),
    );
    if (images.length) {
      event.preventDefault();
      onAddFiles(images, "statement");
    }
  }

  const composingRef = useRef(false);
  const examples = examplesFor(confirmedCourse);
  const canSend = !running && !disabled && (value.trim().length > 0 || files.length > 0);

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || !(event.ctrlKey || event.metaKey)) {
      return;
    }
    // 中文输入法组合期间不触发发送；单独 Enter 始终保持换行，方便输入多行 Python。
    if (composingRef.current || event.nativeEvent.isComposing) {
      return;
    }
    event.preventDefault();
    if (canSend) {
      onSubmit();
    }
  }

  const placeholder = activeProblem
    ? mode === "correction"
      ? "写出纠正后的完整题干，或只写改动部分"
      : mode === "supplement"
        ? "补充模糊的符号、条件，或换一张更清楚的图片"
        : "围绕当前题目继续追问"
    : "输入完整题干、题目图片，或一句话复习需求";

  return (
    <div className="rounded-2xl border border-river/20 bg-white/95 p-3 shadow-card backdrop-blur">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-xs font-medium text-ink">
            {activeProblem
              ? `第 ${activeProblem.version} 版 · ${activeProblem.status === "explained" ? "已讲解" : "待补充"}`
              : "输入题目或复习需求"}
          </span>
          {activeProblem && (
            <div className="flex flex-wrap gap-1">
              {(
                [
                  ["auto", "追问"],
                  ["supplement", "补充"],
                  ["correction", "纠正"],
                  ["new", "新题"],
                ] as const
              ).map(([key, label]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => onModeChange(key)}
                  className={`rounded-full border px-2 py-0.5 text-[10px] font-medium transition ${
                    mode === key
                      ? "border-river bg-river/10 text-river"
                      : "border-slate-200 bg-slate-50 text-slate-600 hover:border-river/40"
                  }`}
                >
                  {label}
                </button>
              ))}
            </div>
          )}
        </div>
        <span className="text-[10px] text-slate-400">
          {activeProblem ? "一次只处理当前这一道题" : "会话自动保存"}
        </span>
      </div>

      <form
        className="mt-2"
        onSubmit={(event) => {
          event.preventDefault();
          if (!running && !disabled) {
            onSubmit();
          }
        }}
      >
        <div className="max-h-[34dvh] overflow-y-auto pr-1">
          {!value.trim() && !running && !disabled && (
            <div className="mb-2 flex flex-wrap items-center gap-1.5">
              <span className="text-[10px] text-slate-400">示例（固定）</span>
              {examples.map((example) => (
                <button
                  key={example}
                  type="button"
                  onClick={() => onChange(example)}
                  className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[10px] text-slate-600 hover:border-river/40"
                >
                  {example}
                </button>
              ))}
            </div>
          )}

          <textarea
            value={value}
            onChange={(event) => onChange(event.target.value)}
            onPaste={handlePaste}
            onKeyDown={handleKeyDown}
            onCompositionStart={() => {
              composingRef.current = true;
            }}
            onCompositionEnd={() => {
              composingRef.current = false;
            }}
            rows={2}
            maxLength={2000}
            placeholder={placeholder}
            aria-keyshortcuts="Control+Enter Meta+Enter"
            title="Enter 换行；Ctrl/⌘ + Enter 发送"
            className="w-full resize-none rounded-xl border border-slate-300 bg-white px-3 py-2 text-sm leading-6 text-ink outline-none transition placeholder:text-slate-400 focus:border-river focus:ring-2 focus:ring-river/20"
          />

          {files.length > 0 && (
            <div className="mt-2 grid grid-cols-3 gap-2 sm:grid-cols-4">
              {files.map((item, index) => (
                <div
                  key={`${item.file.name}-${index}`}
                  className="relative overflow-hidden rounded-lg border border-slate-200 bg-slate-50"
                >
                  <img
                    src={item.previewUrl}
                    alt={`待上传${roleLabel(item.role)}`}
                    className="h-16 w-full object-cover"
                  />
                  <span className="absolute left-1 top-1 rounded bg-black/60 px-1 py-0.5 text-[9px] text-white">
                    {roleLabel(item.role)}
                  </span>
                  <button
                    type="button"
                    onClick={() => onRemoveFile(index)}
                    className="absolute right-1 top-1 rounded bg-white/90 px-1 py-0.5 text-[9px] font-medium text-rose-700"
                  >
                    移除
                  </button>
                </div>
              ))}
            </div>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <label className="cursor-pointer rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-50">
              题干图
              <input
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp"
                multiple
                className="hidden"
                onChange={(event) => handleFiles(event, "statement")}
              />
            </label>
            <label className="cursor-pointer rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-700 hover:bg-slate-50">
              解法图
              <input
                type="file"
                accept="image/jpeg,image/png,image/gif,image/webp"
                multiple
                className="hidden"
                onChange={(event) => handleFiles(event, "solution")}
              />
            </label>
            <span className="text-[10px] text-slate-400">
              可粘贴截图；最多 4 张
            </span>
          </div>

          <details className="mt-2 rounded-lg border border-slate-200 bg-slate-50/60 px-2.5 py-2">
            <summary className="cursor-pointer text-[11px] font-medium text-slate-600">
              我的答案 / 解题过程（可选）
            </summary>
            <div className="mt-2 space-y-2">
              <textarea
                value={studentAnswer}
                onChange={(event) => onStudentAnswerChange(event.target.value)}
                rows={2}
                maxLength={2000}
                placeholder="我的答案"
                className="max-h-24 w-full resize-none rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs leading-5 outline-none focus:border-river"
              />
              <textarea
                value={solutionText}
                onChange={(event) => onSolutionTextChange(event.target.value)}
                rows={2}
                maxLength={4000}
                placeholder="我的解题过程；也可以添加解法图片"
                className="max-h-32 w-full resize-none rounded-lg border border-slate-300 bg-white px-2.5 py-1.5 text-xs leading-5 outline-none focus:border-river"
              />
            </div>
          </details>
          {mode === "auto" && (studentAnswer.trim() || solutionText.trim() || sourcePaperId) && (
            <p role="status" className="mt-2 rounded-lg bg-amber-50 p-2 text-xs leading-5 text-amber-900">
              已保留附加草稿；普通聊天不会发送这些答案、过程或来源，上方已选图片仍会随本条消息发送。需要提交答案或过程时，请明确选择新题、补充或纠正；练习作答请使用对应练习卡片。
            </p>
          )}

          {activeProblem && sourceOptions.length > 0 && (
            <label className="mt-2 flex items-center gap-2 text-[11px] text-slate-500">
              来源
              <select
                value={sourcePaperId}
                onChange={(event) => onSourceChange(event.target.value)}
                className="min-w-0 flex-1 rounded-lg border border-slate-300 bg-white px-2 py-1 text-[11px] text-slate-700"
              >
                <option value="">不关联来源</option>
                {sourceOptions.map((paper) => (
                  <option key={paper.id} value={paper.id}>
                    {paper.title}
                  </option>
                ))}
              </select>
            </label>
          )}
        </div>

        <div className="mt-2 flex items-center justify-between gap-2 border-t border-slate-100 pt-2">
          <span className="text-[10px] text-slate-400">
            {running
              ? "正在处理，请稍候…"
              : disabledReason === "no-conversation"
                ? "请先新建或选择会话"
                : disabledReason === "loading"
                  ? "正在读取会话记录…"
                  : `${value.length}/2000 · Enter 换行，Ctrl/⌘ + Enter 发送`}
          </span>
          <div className="flex items-center gap-2">
            {running && (
              <button
                type="button"
                onClick={onCancel}
                className="rounded-lg border border-slate-300 bg-white px-3 py-1.5 text-xs font-medium text-slate-700 hover:bg-slate-100"
              >
                停止
              </button>
            )}
            <button
              type="submit"
              disabled={running || disabled || (!value.trim() && files.length === 0)}
              className="inline-flex items-center rounded-lg bg-river px-4 py-1.5 text-xs font-medium text-white transition hover:bg-river/90 disabled:cursor-not-allowed disabled:bg-slate-400"
            >
              {running ? "处理中…" : mode === "new" ? "开始新题" : "发送"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}
