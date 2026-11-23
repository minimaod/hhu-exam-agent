import { useLayoutEffect, useRef, useState, type ChangeEvent, type ClipboardEvent, type KeyboardEvent, type ReactNode } from "react";
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

export type PdfUploadStatus = {
  phase: "uploading" | "succeeded" | "refresh_failed" | "error";
  fileName: string;
  message: string;
  canRetry?: boolean;
};

type TurnComposerProps = {
  value: string;
  notices?: ReactNode;
  pdfUploadStatus?: PdfUploadStatus | null;
  onRetryPdfUpload?: () => void;
  onDismissPdfUpload?: () => void;
  remainingRequests?: number;
  running: boolean;
  canCancel?: boolean;
  disabled: boolean;
  quotaBlocked?: boolean;
  imagesBlocked?: boolean;
  disabledReason?: "no-conversation" | "loading" | null;
  activeProblem: ProblemView | null;
  tutoringActive?: boolean;
  mode?: "auto" | "supplement" | "correction" | "new";
  targetProblem?: ProblemView | null;
  files: PendingFile[];
  sourcePaperId: string;
  sourceOptions: PaperItem[];
  onChange: (value: string) => void;
  onAddFiles: (files: File[], role: AttachmentRole) => void;
  onUploadPdf?: (file: File) => void;
  uploadingPdf?: boolean;
  onRemoveFile: (index: number) => void;
  onSourceChange: (paperId: string) => void;
  onResetMode?: () => void;
  onSubmit: () => void;
  onCancel: () => void;
};

export default function TurnComposer({
  value,
  notices,
  pdfUploadStatus = null,
  onRetryPdfUpload,
  onDismissPdfUpload,
  running,
  canCancel = running,
  disabled,
  quotaBlocked = false,
  imagesBlocked = false,
  disabledReason = null,
  activeProblem,
  tutoringActive,
  mode = "auto",
  targetProblem = null,
  files,
  sourcePaperId,
  sourceOptions,
  onChange,
  onAddFiles,
  onUploadPdf,
  uploadingPdf = false,
  onRemoveFile,
  onSourceChange,
  onResetMode,
  onSubmit,
  onCancel,
}: TurnComposerProps) {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [fileNotice, setFileNotice] = useState<string | null>(null);
  const uploadBlocked = quotaBlocked || imagesBlocked;
  const canUploadPdf = Boolean(onUploadPdf) && !uploadingPdf;

  function handleFiles(event: ChangeEvent<HTMLInputElement>) {
    const selected = Array.from(event.target.files ?? []);
    event.target.value = "";
    const isPdf = (file: File) => file.type === "application/pdf" || /\.pdf$/i.test(file.name);
    const pdfs = selected.filter(isPdf);
    setFileNotice(pdfs.length > 1 ? "PDF 每次上传一份，请重新选择。" : null);
    if (pdfs.length === 1 && canUploadPdf) onUploadPdf?.(pdfs[0]);
    const images = selected.filter((file) => !isPdf(file));
    if (images.length && !uploadBlocked) onAddFiles(images, "statement");
  }

  function handlePaste(event: ClipboardEvent<HTMLTextAreaElement>) {
    const images = Array.from(event.clipboardData.files).filter((file) =>
      file.type.startsWith("image/"),
    );
    if (images.length) {
      event.preventDefault();
      if (!uploadBlocked) onAddFiles(images, "statement");
    }
  }

  const composingRef = useRef(false);
  const textRef = useRef<HTMLTextAreaElement>(null);
  useLayoutEffect(() => {
    const element = textRef.current;
    if (!element) return;
    const resize = () => {
      const mobileMin = typeof window !== "undefined" && window.innerWidth <= 860 ? 60 : 40;
      element.style.height = "auto";
      const nextHeight = Math.max(mobileMin, Math.min(160, element.scrollHeight + 1));
      element.style.height = `${nextHeight}px`;
      element.style.overflowY = element.scrollHeight > 160 ? "auto" : "hidden";
    };
    resize();
    window.addEventListener("resize", resize);
    return () => window.removeEventListener("resize", resize);
  }, [value]);
  const canSend = !running && !disabled && !quotaBlocked && (value.trim().length > 0 || files.length > 0);

  function submitDraft() {
    if (!canSend) return;
    onSubmit();
  }

  function handleKeyDown(event: KeyboardEvent<HTMLTextAreaElement>) {
    if (event.key !== "Enter" || event.shiftKey) return;
    // 输入法选字时由浏览器处理 Enter，兼容部分浏览器报告的组合键码。
    if (composingRef.current || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    event.preventDefault();
    if (!event.repeat) submitDraft();
  }

  const isTutoring = tutoringActive ?? Boolean(activeProblem);
  const placeholder = isTutoring
    ? "围绕当前题目继续追问，或补充/纠正题干"
    : "输入完整题干、题目图片，或一句话复习需求";

  return (
    <div className="command-dock">
      {notices}
      {fileNotice && <p role="status" className="px-4 pt-2 text-sm">{fileNotice}</p>}
      {pdfUploadStatus && (
        <div
          role="status"
          aria-live="polite"
          className="pdf-upload-banner"
          data-phase={pdfUploadStatus.phase}
        >
          <span className="min-w-0 flex-1 break-words">{pdfUploadStatus.message}</span>
          <div className="flex shrink-0 items-center gap-2">
            {pdfUploadStatus.canRetry && onRetryPdfUpload && (
              <button
                type="button"
                onClick={onRetryPdfUpload}
                className="rounded border border-current/30 bg-white/80 px-2 py-0.5 text-xs font-medium hover:bg-white"
              >
                重试
              </button>
            )}
            {pdfUploadStatus.phase !== "uploading" && onDismissPdfUpload && (
              <button
                type="button"
                aria-label="关闭上传提示"
                onClick={onDismissPdfUpload}
                className="text-xs opacity-70 hover:opacity-100"
              >
                关闭
              </button>
            )}
          </div>
        </div>
      )}
      {mode !== "auto" && (
        <div
          role="status"
          className="mode-banner"
        >
          <span>
            {mode === "correction"
              ? `正在纠正第 ${(targetProblem ?? activeProblem)?.version ?? 1} 版题干`
              : mode === "supplement"
                ? "正在补充当前题目"
                : "正在输入新题"}
          </span>
          {onResetMode && (
            <button
              type="button"
              onClick={onResetMode}
              className="quiet-button"
            >
              退出模式
            </button>
          )}
        </div>
      )}

      <form
        onSubmit={(event) => {
          event.preventDefault();
          submitDraft();
        }}
      >
        <div className="composer-fields">
          <textarea
            ref={textRef}
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
            rows={1}
            maxLength={2000}
            disabled={quotaBlocked}
            placeholder={placeholder}
            enterKeyHint="send"
            aria-keyshortcuts="Enter"
            title="Enter 发送 · Shift+Enter 换行"
            aria-label="学习指令"
            className="composer-input"
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
                    alt="待上传图片"
                    className="h-16 w-full object-cover"
                  />
                  <span className="absolute left-1 top-1 rounded bg-black/60 px-1 py-0.5 text-xs text-white">
                    {item.file.name}
                  </span>
                  <button
                    type="button"
                    onClick={() => onRemoveFile(index)}
                    className="absolute right-1 top-1 rounded bg-white/90 px-1 py-0.5 text-xs font-medium text-rose-700"
                  >
                    移除
                  </button>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="composer-footer">
          <button
            type="button"
            className="composer-add"
            aria-label="添加附件"
            title="添加附件"
            disabled={running || disabledReason !== null || (uploadBlocked && !canUploadPdf)}
            onClick={() => fileInputRef.current?.click()}
          >
            <span aria-hidden="true">＋</span>
          </button>
          <input
            ref={fileInputRef}
            type="file"
            aria-label="选择附件"
            accept={[
              !uploadBlocked ? "image/jpeg,image/png,image/gif,image/webp" : "",
              canUploadPdf ? ".pdf,application/pdf" : "",
            ].filter(Boolean).join(",")}
            multiple
            hidden
            onChange={handleFiles}
          />
          <span className="composer-add-hint">可上传题目图片、手写过程图片或 PDF</span>
          {mode !== "auto" && activeProblem && sourceOptions.length > 0 && (
            <label className="flex items-center gap-2 text-xs text-slate-500">
              来源
              <select
                value={sourcePaperId}
                onChange={(event) => onSourceChange(event.target.value)}
                className="min-w-0 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs text-slate-700"
              >
                <option value="">不关联来源</option>
                {sourceOptions.map((paper) => (
                  <option key={paper.id} value={paper.id}>{paper.title}</option>
                ))}
              </select>
            </label>
          )}

          <span className="composer-shortcuts" title="可粘贴截图；最多 4 张">
            {running
              ? "正在处理，请稍候…"
              : quotaBlocked
                ? "今日模型请求额度已用完"
                : disabledReason === "no-conversation"
                  ? "请先新建或选择会话"
                  : disabledReason === "loading"
                    ? "正在读取会话记录…"
                    : <span className="composer-key-hint">Enter 发送 · Shift+Enter 换行</span>}
          </span>
          <div className="composer-actions">
            {canCancel && (
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
              disabled={!canSend}
              className="primary-button"
            >
              {running ? "处理中…" : "发送"}
            </button>
          </div>
        </div>
      </form>
    </div>
  );
}
