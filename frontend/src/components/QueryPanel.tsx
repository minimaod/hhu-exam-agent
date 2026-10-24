import PaperCard from "./PaperCard";
import type { PaperItem } from "../lib/papersApi";
import type { QueryKind, ResultEvent } from "../lib/queryApi";

export type QueryUiState =
  | { phase: "idle" }
  | {
      phase: "running";
      stage: "starting" | "understanding" | "retrieving";
      message: string;
    }
  | { phase: "done"; result: ResultEvent }
  | { phase: "error"; message: string; code?: string };

type QueryPanelProps = {
  state: QueryUiState;
  downloadingId: string | null;
  downloadError: { id: string; message: string } | null;
  onDownload: (paper: PaperItem) => void;
  onRetry: () => void;
};

const STEPS = [
  { stage: "understanding", label: "理解复习需求" },
  { stage: "retrieving", label: "查找真实试卷" },
] as const;

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
    hint: "当前支持按课程和期中/期末查询、下载原文件。",
  },
};

function ProgressCard({
  stage,
  message,
}: {
  stage: "starting" | "understanding" | "retrieving";
  message: string;
}) {
  const activeIndex = stage === "retrieving" ? 1 : stage === "understanding" ? 0 : -1;

  return (
    <div
      aria-live="polite"
      className="mt-4 rounded-2xl border border-slate-200/90 bg-white/90 p-5 shadow-card backdrop-blur"
    >
      <p className="text-sm font-medium text-ink">{message}</p>
      <ol className="mt-3 flex flex-wrap items-center gap-x-6 gap-y-2 text-xs">
        {STEPS.map((step, index) => {
          const done = activeIndex > index;
          const active = activeIndex === index;
          return (
            <li
              key={step.stage}
              className={
                done
                  ? "text-river"
                  : active
                    ? "font-medium text-ink"
                    : "text-slate-400"
              }
            >
              <span aria-hidden="true" className="mr-1.5">
                {done ? "✓" : active ? "●" : "○"}
              </span>
              {step.label}
            </li>
          );
        })}
      </ol>
    </div>
  );
}

function ResultCard({
  result,
  downloadingId,
  downloadError,
  onDownload,
}: Omit<QueryPanelProps, "state" | "onRetry"> & { result: ResultEvent }) {
  if (result.kind === "found") {
    return (
      <div aria-live="polite" className="mt-4">
        <div className="flex flex-wrap items-baseline justify-between gap-2">
          <h3 className="text-sm font-semibold text-ink">自然语言查询结果</h3>
          <p className="text-[11px] text-slate-500">共 {result.total} 份</p>
        </div>
        <p className="mt-1 text-sm leading-6 text-slate-700">{result.message}</p>
        <ul className="mt-3 space-y-3">
          {result.items.map((paper) => (
            <PaperCard
              key={paper.id}
              paper={paper}
              downloading={downloadingId === paper.id}
              downloadError={
                downloadError?.id === paper.id ? downloadError.message : null
              }
              onDownload={onDownload}
            />
          ))}
        </ul>
      </div>
    );
  }

  const copy = NON_FOUND_COPY[result.kind];

  return (
    <div
      aria-live="polite"
      className="mt-4 rounded-2xl border border-slate-200/90 bg-white/90 p-5 shadow-card backdrop-blur"
    >
      <p className="text-sm font-semibold text-ink">{copy.title}</p>
      <p className="mt-1 text-sm leading-6 text-slate-700">{result.message}</p>
      <p className="mt-2 text-xs leading-5 text-slate-500">{copy.hint}</p>
    </div>
  );
}

/**
 * 自然语言查询的结果区：进度、业务结果、错误与重试。
 */
export default function QueryPanel({
  state,
  downloadingId,
  downloadError,
  onDownload,
  onRetry,
}: QueryPanelProps) {
  if (state.phase === "idle") {
    return null;
  }

  if (state.phase === "running") {
    return <ProgressCard stage={state.stage} message={state.message} />;
  }

  if (state.phase === "done") {
    return (
      <ResultCard
        result={state.result}
        downloadingId={downloadingId}
        downloadError={downloadError}
        onDownload={onDownload}
      />
    );
  }

  return (
    <div
      role="alert"
      className="mt-4 rounded-2xl border border-rose-200 bg-rose-50/80 p-5"
    >
      <p className="text-sm font-semibold text-rose-900">查询没有完成</p>
      <p className="mt-1 break-words text-sm leading-6 text-rose-800">
        {state.message}
      </p>
      {state.code && (
        <p className="mt-1 text-[11px] text-rose-500">错误码：{state.code}</p>
      )}
      <button
        type="button"
        onClick={onRetry}
        className="mt-3 inline-flex items-center rounded-xl border border-rose-300 bg-white px-4 py-2 text-sm font-medium text-rose-800 transition hover:bg-rose-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 focus-visible:ring-offset-2"
      >
        重试
      </button>
    </div>
  );
}
