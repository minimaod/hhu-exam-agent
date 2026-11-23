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
  return value === "未标注" ? "—" : `${value} 学年`;
}

/** 一条试卷：展示目录信息并提供下载入口。 */
export default function PaperCard({ paper, download, onDownload }: PaperCardProps) {
  const examDate = formatExamDate(paper.exam_date);
  const yearKnown = paper.academic_year !== "未标注";
  const downloading = download?.phase === "downloading";
  const failed = download?.phase === "error";

  return (
    <li className="paper-index-row">
      <span
        className={`paper-year${yearKnown ? "" : " is-unknown"}`}
        title={yearKnown ? undefined : "学年未标注"}
      >
        {formatAcademicYear(paper.academic_year)}
      </span>
      <div className="paper-description">
        <h3>{paper.title}</h3>
        <p>{paper.course}</p>
      </div>
      <span className="paper-kind"><span className="sheet-badge">{EXAM_TYPE_LABELS[paper.exam_type]}</span></span>
      <span
        className={`paper-date${examDate.known ? "" : " is-unknown"}`}
        title={examDate.known ? `考试日期：${examDate.text}` : undefined}
      >
        {examDate.text}
      </span>
      <button type="button" className="quiet-button paper-download" disabled={downloading} onClick={() => onDownload(paper)}>
        {downloading ? "正在下载…" : failed ? "重新下载" : "下载原始 PDF"}
      </button>
      {download?.phase === "triggered" && download.message && (
        <p role="status" className="paper-feedback">{download.message}</p>
      )}
      {download?.phase === "error" && download.message && (
        <p role="alert" className="paper-feedback inline-alert">{download.message}</p>
      )}
    </li>
  );
}
