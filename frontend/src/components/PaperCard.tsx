import type { PaperItem } from "../lib/papersApi";

type PaperCardProps = {
  paper: PaperItem;
  downloading: boolean;
  downloadError: string | null;
  onDownload: (paper: PaperItem) => void;
};

const EXAM_TYPE_LABELS: Record<PaperItem["exam_type"], string> = {
  midterm: "期中",
  final: "期末",
};

function formatDate(value: string | null): string {
  return value ? value : "日期未登记";
}

/** 一条试卷：展示目录信息并提供下载入口。 */
export default function PaperCard({
  paper,
  downloading,
  downloadError,
  onDownload,
}: PaperCardProps) {
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
            <span>{paper.academic_year} 学年</span>
            <span aria-hidden="true" className="text-slate-300">
              ·
            </span>
            <span>{formatDate(paper.exam_date)}</span>
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
          {downloading ? "正在下载…" : "下载原始 PDF"}
        </button>
        <code className="font-mono text-[11px] text-slate-400">{paper.id}</code>
      </div>

      {downloadError && (
        <p role="alert" className="mt-3 break-words text-xs leading-5 text-rose-700">
          {downloadError}
        </p>
      )}
    </li>
  );
}
