import PaperCard from "./PaperCard";
import type { PaperItem } from "../lib/papersApi";
import type { QueryKind, ResultEvent } from "../lib/queryApi";
import type { DownloadStates } from "../lib/display";

type QueryResultProps = {
  result: ResultEvent;
  downloads?: DownloadStates;
  /** 同一段文字已在上方自然语言回复中出现时不再重复。 */
  showMessage?: boolean;
  onDownload: (paper: PaperItem) => void;
};

type NonFoundKind = Exclude<QueryKind, "found">;

const NON_FOUND_COPY: Record<NonFoundKind, { title: string; hint: string }> = {
  empty: {
    title: "没有匹配的试卷",
    hint: "可以换一种课程写法，或检查目录里是否已经登记这份资料。",
  },
  clarification: {
    title: "还需要补充课程",
    hint: "例如直接输入“我要复习高数”，再说明期中或期末。",
  },
  unsupported: {
    title: "当前还不支持",
    hint: "当前支持按课程和期中/期末/其他查询、下载原文件。",
  },
};

/** 一份已保存或刚返回的查询结果；实时结果与会话历史共用。 */
export default function QueryResult({
  result,
  downloads,
  showMessage = true,
  onDownload,
}: QueryResultProps) {
  if (result.kind === "found") {
    return (
      <div>
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-sm font-semibold text-ink">查询结果</h3>
          <p className="text-[11px] text-slate-500">共 {result.total} 份</p>
        </div>
        {showMessage && (
          <p className="mt-1 text-sm leading-6 text-slate-700">{result.message}</p>
        )}
        <ul className="mt-3 space-y-3">
          {result.items.map((paper) => (
            <PaperCard
              key={paper.id}
              paper={paper}
              download={downloads?.[paper.id] ?? null}
              onDownload={onDownload}
            />
          ))}
        </ul>
      </div>
    );
  }

  const copy = NON_FOUND_COPY[result.kind];

  return (
    <div className="rounded-2xl border border-slate-200/90 bg-white/90 p-5 shadow-card backdrop-blur">
      <p className="text-sm font-semibold text-ink">{copy.title}</p>
      {showMessage && (
        <p className="mt-1 text-sm leading-6 text-slate-700">{result.message}</p>
      )}
      <p className="mt-2 text-xs leading-5 text-slate-500">{copy.hint}</p>
    </div>
  );
}