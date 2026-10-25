import type { PaperItem } from "../lib/papersApi";
import { formatExamDate, type DownloadState } from "../lib/display";

type PaperCardProps = {
  paper: PaperItem;
  download?: DownloadState | null;
  onDownload: (paper: PaperItem) => void;
};

const EXAM_TYPE_LABELS: Record<PaperItem["exam_type"], string> = {
  midterm: "期中",
  final: "期末",
  other: "其他",
};

function formatAcademicYear(value: string): string {
  return value === "未标注" ? "学年未标注" : `${value} 学年`;
}

/** 一条试卷：展示目录信息并提供下载入口。 */
export default function PaperCard({ paper, download, onDownload }: PaperCardProps) {
  const examDate = formatExamDate(paper.exam_date);
  const downloading = download?.phase === "downloading";
  const failed = download?.phase === "error";

  return (
    <li className="rounded-2xl border border-slate-200/90 bg-white/90 p-5 shadow-card backdrop-blur">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <h3 className="text-base font-semibold leading-6 text-ink">
            {paper.title}
          </h3>
          <div className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-slate-600">
            <span>{paper.course}</span>
            <span aria-hidden="true" className="text-slate-300">
              ·
            </span>
            <span>{formatAcademicYear(paper.academic_year)}</span>
            <span aria-hidden="true" className="text-slate-300">
              ·
            </span>
            <span
              className={examDate.known ? undefined : "text-slate-400"}
              title={examDate.known ? `考试日期：${examDate.text}` : "目录未登记考试日期"}
            >
              {examDate.text}
            </span>
          </div>
        </div>

        <span className="rounded-full border border-river/30 bg-river/10 px-3 py-1 text-[11px] font-medium text-river">
          {EXAM_TYPE_LABELS[paper.exam_type]}
        </span>
      </div>

      <div className="mt-4 flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={() => onDownload(paper)}
          disabled={downloading}
          className="inline-flex items-center gap-2 rounded-xl border border-ink/15 bg-ink px-4 py-2 text-sm font-medium text-white transition hover:bg-slate-700 focus:outline-none focus-visible:ring-2 focus-visible:ring-river focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-400"
        >
          {downloading
            ? "正在下载…"
            : failed
              ? "重新下载"
              : "下载原始 PDF"}
        </button>
        <span className="text-[11px] text-slate-400">原文件，不压缩</span>
      </div>

      {download?.phase === "triggered" && download.message && (
        <p
          role="status"
          className="mt-3 break-words rounded-lg bg-emerald-50 px-2.5 py-1.5 text-xs leading-5 text-emerald-800"
        >
          {download.message}
        </p>
      )}

      {download?.phase === "error" && download.message && (
        <p
          role="alert"
          className="mt-3 break-words rounded-lg bg-rose-50 px-2.5 py-1.5 text-xs leading-5 text-rose-800"
        >
          {download.message}
        </p>
      )}
    </li>
  );
}