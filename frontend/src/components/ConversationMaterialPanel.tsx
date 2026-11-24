import { useState } from "react";
import AssistantMarkdown from "./AssistantMarkdown.tsx";
import {
  getMaterialPage,
  looksLikeExam,
  materialPreviewUrl,
  type ArtifactInputMode,
  type ArtifactSummary,
  type ConversationMaterialItem,
  type MaterialPageStatus,
  type OutputKind,
  type OutputStatus,
} from "../lib/materialsApi.ts";
import { formatFileSize } from "../lib/display.ts";

const MAX_RANGE_PAGES = 40;

const OUTPUT_OPTIONS: ReadonlyArray<{ id: OutputKind; label: string }> = [
  { id: "notes", label: "章节笔记" },
  { id: "quiz", label: "章节测试" },
  { id: "graph", label: "知识图谱" },
];

function statusText(status: OutputStatus | undefined): string {
  if (status === "ready") return "可打开";
  if (status === "generating") return "生成中";
  if (status === "failed") return "失败";
  return "未生成";
}

type ConversationMaterialPanelProps = {
  items: ConversationMaterialItem[];
  generatingKey: string | null;
  recognizingId: string | null;
  checkingReadabilityId?: string | null;
  onRecognize: (materialId: string, pages: number[]) => void;
  onCheckReadability?: (materialId: string, startPage: number, endPage: number) => void;
  quotaBlocked: boolean;
  onGenerate: (
    materialId: string,
    outputs: OutputKind[],
    startPage: number,
    endPage: number,
    courseHint?: string,
    coverageMode?: ArtifactInputMode,
  ) => void;
  onOpenArtifact: (
    artifactId: string,
    materialName: string,
    pageCount: number,
    initialTab?: OutputKind,
  ) => void;
  onDetach: (materialId: string) => void;
};

const PAGE_STATUS_TEXT: Record<MaterialPageStatus["status"], string> = {
  unchecked: "待检查",
  usable: "原文可用",
  pending_ocr: "待识别",
  ocr_failed: "识别失败",
  excluded: "无需复习",
};

function OcrTranscript({ materialId, pages }: { materialId: string; pages: number[] }) {
  const [selectedPage, setSelectedPage] = useState(pages[0]);
  const [transcript, setTranscript] = useState<{ page: number; text: string; error: boolean } | null>(null);
  const page = pages.includes(selectedPage) ? selectedPage : pages[0];
  const visible = transcript?.page === page ? transcript : null;

  const openTranscript = async () => {
    try {
      const result = await getMaterialPage(materialId, page);
      setTranscript({ page, text: result.extracted_text, error: false });
    } catch (error) {
      setTranscript({ page, text: error instanceof Error ? error.message : "无法读取识别文字。", error: true });
    }
  };

  return (
    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
      <select
        aria-label="识别文字页码"
        className="quiet-button"
        value={page}
        onChange={(event) => setSelectedPage(Number(event.target.value))}
      >
        {pages.map((pageNo) => <option key={pageNo} value={pageNo}>第 {pageNo} 页</option>)}
      </select>
      <button type="button" className="quiet-button" onClick={() => void openTranscript()}>
        查看识别文字
      </button>
      <a href={materialPreviewUrl(materialId, page)} target="_blank" rel="noreferrer" className="quiet-button">
        对照原页
      </a>
      {visible && (
        <div
          role={visible.error ? "alert" : undefined}
          className="max-h-64 w-full overflow-auto whitespace-pre-wrap rounded-lg border border-slate-200 bg-white p-2"
        >
          {visible.error ? visible.text : <AssistantMarkdown text={visible.text} />}
        </div>
      )}
    </div>
  );
}

export default function ConversationMaterialPanel({
  items,
  generatingKey,
  recognizingId,
  checkingReadabilityId = null,
  onRecognize,
  onCheckReadability,
  quotaBlocked,
  onGenerate,
  onOpenArtifact,
  onDetach,
}: ConversationMaterialPanelProps) {
  const [ranges, setRanges] = useState<Record<string, { start: number; end: number }>>({});

  if (items.length === 0) {
    return null;
  }

  return (
    <section
      className="mb-4 space-y-3 rounded-2xl border border-slate-200/90 bg-white/85 p-3.5 shadow-sm"
      aria-label="当前会话个人资料与成果"
    >
      <div className="flex items-center justify-between gap-2">
        <p className="text-xs font-semibold tracking-wide text-slate-600">
          本会话资料与成果（{items.length}）
        </p>
        {items.some(({ material }) => !(material.is_exam ?? looksLikeExam(material.display_name))) && (
          <span className="text-xs text-slate-500">单次最多选 {MAX_RANGE_PAGES} 页</span>
        )}
      </div>

      <div className="space-y-3">
        {items.map((entry) => {
          const mat = entry.material;
          const ov = entry.readability;
          const isExam = mat.is_exam ?? looksLikeExam(mat.display_name);
          const range = ranges[mat.id] ?? {
            start: 1,
            end: Math.min(mat.page_count, MAX_RANGE_PAGES),
          };
          const recognizing = recognizingId === mat.id;
          const checkingReadability = checkingReadabilityId === mat.id;
          const busyThisMaterial = recognizing || checkingReadability || Boolean(generatingKey?.startsWith(`${mat.id}:`));
          const pageSpan = range.end - range.start + 1;
          const rangeValid = Number.isInteger(range.start) && Number.isInteger(range.end) &&
            pageSpan >= 1 && pageSpan <= MAX_RANGE_PAGES;
          const selectedPages = rangeValid
            ? Array.from({ length: pageSpan }, (_, index) => range.start + index)
            : [];
          const scannedSet = new Set(ov?.scanned_pages ?? []);
          const statusByPage = new Map(ov?.pages?.map((page) => [page.page_no, page]));
          const failedPages = selectedPages.filter((page) => statusByPage.get(page)?.status === "ocr_failed");
          const pendingPages = selectedPages.filter((page) =>
            statusByPage.get(page)?.status === "pending_ocr" || (!statusByPage.has(page) && scannedSet.has(page)),
          );
          const needsRecognition = pendingPages.length > 0 || failedPages.length > 0;
          const checkedSet = new Set(ov?.checked_pages ?? ov?.pages?.map((page) => page.page_no));
          const hasUncheckedInRange = (!ov || ov.checked_pages || ov.pages) &&
            selectedPages.some((page) => !checkedSet.has(page));
          const requiresCheck = hasUncheckedInRange && Boolean(onCheckReadability);
          const allScanned = selectedPages.length > 0 && selectedPages.every((page) =>
            scannedSet.has(page) || statusByPage.get(page)?.status === "excluded",
          );

          const materialTools = (
            <>
              {hasUncheckedInRange && onCheckReadability && (
                <div className="mt-2 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 bg-white px-2.5 py-1.5 text-xs text-slate-600">
                  <span>第 {range.start}–{range.end} 页尚未检查完。检查后可选择补识别或使用当前可用页生成。</span>
                  <button
                    type="button"
                    className="quiet-button"
                    disabled={!rangeValid || checkingReadabilityId !== null || busyThisMaterial}
                    onClick={() => onCheckReadability(mat.id, range.start, range.end)}
                  >
                    {checkingReadability ? "检查中…" : "检查文字识别"}
                  </button>
                </div>
              )}
              {!requiresCheck && pendingPages.length > 0 && (
                <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-900">
                  <p>待识别 {pendingPages.length} 页，预计 {pendingPages.length} 次模型请求。每批最多将 8 页图片发送给 DeepSeek，并保存结果。</p>
                  <button
                    type="button"
                    className="quiet-button mt-1"
                    disabled={recognizingId !== null || busyThisMaterial || quotaBlocked}
                    onClick={() => onRecognize(mat.id, pendingPages)}
                  >
                    {recognizing ? "识别扫描页中…" : "识别扫描页"}
                  </button>
                  {onCheckReadability && (
                    <button
                      type="button"
                      className="quiet-button mt-1 ml-2"
                      disabled={checkingReadabilityId !== null || busyThisMaterial}
                      onClick={() => onCheckReadability(mat.id, range.start, range.end)}
                    >
                      {checkingReadability ? "检查中…" : "重新检查文字"}
                    </button>
                  )}
                </div>
              )}
              {!requiresCheck && failedPages.length > 0 && (
                <div className="mt-2 rounded-lg border border-amber-200 bg-amber-50 px-2.5 py-1.5 text-xs text-amber-900">
                  <p>第 {failedPages.join("、")} 页识别失败。重试会将这些页的图片发送给 DeepSeek，预计 {failedPages.length} 次模型请求。</p>
                  <button
                    type="button"
                    className="quiet-button mt-1"
                    disabled={recognizingId !== null || busyThisMaterial || quotaBlocked}
                    onClick={() => onRecognize(mat.id, failedPages)}
                  >
                    只重试失败页
                  </button>
                </div>
              )}
              {ov?.pages && rangeValid && (
                <details className="mt-2 text-xs text-slate-600">
                  <summary className="cursor-pointer">查看所选页的识别情况</summary>
                  <ul className="mt-1 max-h-52 space-y-1 overflow-y-auto">
                    {selectedPages.map((page) => {
                      const state = statusByPage.get(page);
                      const label = state?.status === "usable" && state.source === "ocr"
                        ? "OCR 可用" : PAGE_STATUS_TEXT[state?.status ?? "unchecked"];
                      return (
                        <li key={page}>
                          <a className="underline" href={materialPreviewUrl(mat.id, page)} target="_blank" rel="noreferrer">第 {page} 页</a>
                          {" · "}{label}{state?.reason ? " · " + state.reason : ""}
                        </li>
                      );
                    })}
                  </ul>
                </details>
              )}
              {Boolean(ov?.ocr_pages?.length) && (
                <OcrTranscript materialId={mat.id} pages={ov?.ocr_pages ?? []} />
              )}
              <div className="mt-2.5 flex flex-wrap items-center gap-2">
                <label className="flex items-center gap-1 text-xs text-slate-600">
                  页码
                  <input
                    type="number"
                    min={1}
                    max={mat.page_count}
                    aria-label={`${mat.display_name} 起始页`}
                    value={range.start}
                    onChange={(e) => {
                      const val = Math.max(1, Math.min(mat.page_count, Number(e.target.value) || 1));
                      setRanges((prev) => ({
                        ...prev,
                        [mat.id]: { start: val, end: Math.max(val, range.end) },
                      }));
                    }}
                    className="w-14 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs"
                  />
                  <span>–</span>
                  <input
                    type="number"
                    min={1}
                    max={mat.page_count}
                    aria-label={`${mat.display_name} 结束页`}
                    value={range.end}
                    onChange={(e) => {
                      const val = Math.max(1, Math.min(mat.page_count, Number(e.target.value) || 1));
                      setRanges((prev) => ({
                        ...prev,
                        [mat.id]: { start: Math.min(range.start, val), end: val },
                      }));
                    }}
                    className="w-14 rounded-lg border border-slate-300 bg-white px-2 py-1 text-xs"
                  />
                </label>

                {!requiresCheck && (!allScanned || needsRecognition) && OUTPUT_OPTIONS.map((opt) => {
                  const isGenerating = generatingKey === `${mat.id}:${opt.id}`;
                  return (
                    <button
                      key={opt.id}
                      type="button"
                      className="quiet-button text-xs"
                      disabled={!rangeValid || busyThisMaterial || quotaBlocked}
                      onClick={() =>
                        onGenerate(mat.id, [opt.id], range.start, range.end, undefined, needsRecognition ? "recognize" : undefined)
                      }
                    >
                      {isGenerating ? "处理中…" : (needsRecognition ? "识别并生成" : "生成") + opt.label}
                    </button>
                  );
                })}
                {!requiresCheck && needsRecognition && !allScanned && (
                  <details className="w-full text-xs text-slate-600">
                    <summary className="cursor-pointer">仅用当前可用页生成部分成果</summary>
                    <div className="mt-1 flex flex-wrap gap-2">
                      {OUTPUT_OPTIONS.map((opt) => (
                        <button
                          key={opt.id}
                          type="button"
                          className="quiet-button"
                          disabled={!rangeValid || busyThisMaterial || quotaBlocked}
                          onClick={() => onGenerate(mat.id, [opt.id], range.start, range.end, undefined, "partial")}
                        >
                          生成部分{opt.label}
                        </button>
                      ))}
                    </div>
                  </details>
                )}
                {!rangeValid && (
                  <p className="w-full text-xs text-amber-800">请选择最多 {MAX_RANGE_PAGES} 页。</p>
                )}
              </div>
              {entry.artifacts.length > 0 && (
                <div className="mt-2.5 space-y-2 border-t border-slate-200/80 pt-2">
                  {entry.artifacts.map((art: ArtifactSummary) => {
                    const notesSt = art.notes_status ?? (art.status === "ready" ? "ready" : "none");
                    const quizSt = art.quiz_status ?? (art.status === "ready" ? "ready" : "none");
                    const graphSt = art.graph_status ?? (art.status === "ready" ? "ready" : "none");
                    const parts: ReadonlyArray<{ id: OutputKind; label: string; st: OutputStatus }> = [
                      { id: "notes", label: "章节笔记", st: notesSt },
                      { id: "quiz", label: art.question_count > 0 ? `章节测试（${art.question_count}题）` : "章节测试", st: quizSt },
                      { id: "graph", label: "知识图谱", st: graphSt },
                    ];
                    const visibleParts = parts.filter((p) => p.st !== "none");
                    if (visibleParts.length === 0) return null;

                    return (
                      <div
                        key={art.id}
                        data-artifact-card={art.id}
                        className="flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 bg-white px-2.5 py-2 text-xs"
                      >
                        <span className="font-medium text-slate-700">
                          所选第 {art.start_page}–{art.end_page} 页成果{art.coverage?.missing_pages.length ? " · 资料有缺页" : ""}
                        </span>
                        <div className="flex flex-wrap items-center gap-1.5">
                          {visibleParts.map((part) => {
                            if (part.st === "ready") {
                              return (
                                <button
                                  key={part.id}
                                  type="button"
                                  onClick={() =>
                                    onOpenArtifact(art.id, mat.display_name, mat.page_count, part.id)
                                  }
                                  className="rounded-md border border-emerald-200 bg-emerald-50 px-2 py-0.5 font-medium text-emerald-800 hover:bg-emerald-100"
                                >
                                  {part.id === "quiz" ? "打开章节测试" : "打开" + part.label}
                                </button>
                              );
                            }
                            if (part.st === "failed") {
                              return (
                                <button
                                  key={part.id}
                                  type="button"
                                  disabled={busyThisMaterial || quotaBlocked}
                                  onClick={() =>
                                    onGenerate(mat.id, [part.id], art.start_page, art.end_page, art.course_hint)
                                  }
                                  className="rounded-md border border-rose-200 bg-rose-50 px-2 py-0.5 font-medium text-rose-800 hover:bg-rose-100 disabled:opacity-50"
                                >
                                  {part.label}失败 · 重试
                                </button>
                              );
                            }
                            return (
                              <span
                                key={part.id}
                                className="rounded-md border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-800"
                              >
                                {part.label} · {statusText(part.st)}
                              </span>
                            );
                          })}
                        </div>
                      </div>
                    );
                  })}
                </div>
              )}
            </>
          );

          return (
            <article
              key={mat.id}
              id={"material-card-" + mat.id}
              data-material-card={mat.id}
              className="rounded-xl border border-slate-200 bg-slate-50/60 p-3"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0">
                  <h3 className="truncate text-sm font-semibold text-slate-800">
                    {mat.display_name}
                  </h3>
                  <p className="mt-0.5 text-xs text-slate-600">
                    共 {mat.page_count} 页 · {formatFileSize(mat.size_bytes)}
                    {!isExam && ov ? ` · ${ov.summary}` : ""}
                  </p>
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <a
                    href={materialPreviewUrl(mat.id)}
                    target="_blank"
                    rel="noreferrer"
                    className="quiet-button text-xs"
                  >
                    预览原件
                  </a>
                  <button
                    type="button"
                    className="quiet-button text-xs text-slate-600"
                    disabled={busyThisMaterial}
                    onClick={() => onDetach(mat.id)}
                  >
                    移出会话
                  </button>
                </div>
              </div>

              {isExam ? (
                <details className="mt-2 text-xs">
                  <summary className="cursor-pointer text-slate-600">资料工具</summary>
                  {ov && <p className="mt-1 text-slate-600">{ov.summary}</p>}
                  {materialTools}
                </details>
              ) : materialTools}

            </article>
          );
        })}
      </div>
    </section>
  );
}
