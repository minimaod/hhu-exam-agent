import { useCallback, useEffect, useRef, useState } from "react";
import NaturalLanguageQuery from "./components/NaturalLanguageQuery";
import PaperCard from "./components/PaperCard";
import PaperFilters from "./components/PaperFilters";
import QueryPanel, { type QueryUiState } from "./components/QueryPanel";
import { fetchHealth, HealthCheckError } from "./lib/healthApi";
import {
  downloadPaper,
  fetchPapers,
  PapersApiError,
  type ExamTypeFilter,
  type PaperItem,
} from "./lib/papersApi";
import { QueryApiError, streamQuery } from "./lib/queryApi";

type PapersState =
  | { phase: "loading" }
  | { phase: "ready"; items: PaperItem[]; total: number }
  | { phase: "error"; message: string };

type HealthState =
  | { phase: "checking" }
  | { phase: "ok" }
  | { phase: "error"; message: string };

/** 一次已提交的查询；seq 让“重试”和“同样条件再查一次”也能触发新请求。 */
type SubmittedQuery = {
  course: string;
  examType: ExamTypeFilter;
  seq: number;
};

const INITIAL_QUERY: SubmittedQuery = { course: "", examType: "all", seq: 0 };

export default function App() {
  const [courseInput, setCourseInput] = useState("");
  const [examTypeInput, setExamTypeInput] = useState<ExamTypeFilter>("all");
  const [submitted, setSubmitted] = useState<SubmittedQuery>(INITIAL_QUERY);

  const [papersState, setPapersState] = useState<PapersState>({
    phase: "loading",
  });
  const [downloadingId, setDownloadingId] = useState<string | null>(null);
  const [downloadError, setDownloadError] = useState<{
    id: string;
    message: string;
  } | null>(null);

  const [healthState, setHealthState] = useState<HealthState>({
    phase: "checking",
  });
  const [healthAttempt, setHealthAttempt] = useState(0);

  const [naturalInput, setNaturalInput] = useState("");
  const [queryState, setQueryState] = useState<QueryUiState>({ phase: "idle" });
  const queryAbort = useRef<AbortController | null>(null);
  const querySeq = useRef(0);
  const lastNaturalQuery = useRef("");

  // 每次筛选或重试都会换成新的 submitted 对象；旧请求被 abort，页面只认最新一次结果。
  useEffect(() => {
    const controller = new AbortController();
    setPapersState({ phase: "loading" });

    fetchPapers(
      { course: submitted.course, examType: submitted.examType },
      controller.signal,
    )
      .then((payload) => {
        setPapersState({
          phase: "ready",
          items: payload.items,
          total: payload.total,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }

        // 筛选条件变化时，旧请求的错误也不再展示。
        setPapersState({
          phase: "error",
          message:
            error instanceof PapersApiError
              ? error.message
              : "查询试卷时发生未知错误，请重试。",
        });
      });

    return () => controller.abort();
  }, [submitted]);

  // 页脚的服务状态：F001 的健康检查保留，但不再占据页面主体。
  useEffect(() => {
    const controller = new AbortController();
    setHealthState({ phase: "checking" });

    fetchHealth(controller.signal)
      .then(() => setHealthState({ phase: "ok" }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }

        setHealthState({
          phase: "error",
          message:
            error instanceof HealthCheckError
              ? error.message
              : "检查时发生未知错误，请重试。",
        });
      });

    return () => controller.abort();
  }, [healthAttempt]);

  // 组件卸载时停止仍在进行的自然语言请求。
  useEffect(
    () => () => {
      queryAbort.current?.abort();
    },
    [],
  );

  const submitQuery = useCallback(() => {
    setSubmitted((current) => ({
      course: courseInput.trim(),
      examType: examTypeInput,
      seq: current.seq + 1,
    }));
  }, [courseInput, examTypeInput]);

  const retryQuery = useCallback(() => {
    setSubmitted((current) => ({ ...current, seq: current.seq + 1 }));
  }, []);

  const runNaturalQuery = useCallback(async (query: string) => {
    const text = query.trim();
    if (!text) {
      return;
    }

    lastNaturalQuery.current = text;

    // 用递增序号识别过期响应；新请求会 abort 旧请求。
    const seq = querySeq.current + 1;
    querySeq.current = seq;
    queryAbort.current?.abort();
    const controller = new AbortController();
    queryAbort.current = controller;

    setQueryState({
      phase: "running",
      stage: "starting",
      message: "正在提交复习需求…",
    });

    try {
      const result = await streamQuery(text, {
        signal: controller.signal,
        onProgress: (event) => {
          if (querySeq.current !== seq) {
            return;
          }
          setQueryState({
            phase: "running",
            stage: event.stage,
            message: event.message,
          });
        },
      });

      if (querySeq.current !== seq) {
        return;
      }
      setQueryState({ phase: "done", result });
    } catch (error: unknown) {
      if (querySeq.current !== seq || controller.signal.aborted) {
        return;
      }
      if (error instanceof DOMException && error.name === "AbortError") {
        return;
      }

      setQueryState({
        phase: "error",
        message:
          error instanceof QueryApiError
            ? error.message
            : "查询时发生未知错误，请重试。",
        code: error instanceof QueryApiError ? error.code : undefined,
      });
    }
  }, []);

  const cancelNaturalQuery = useCallback(() => {
    querySeq.current += 1;
    queryAbort.current?.abort();
    queryAbort.current = null;
    setQueryState({ phase: "idle" });
  }, []);

  const retryNaturalQuery = useCallback(() => {
    if (lastNaturalQuery.current) {
      void runNaturalQuery(lastNaturalQuery.current);
    }
  }, [runNaturalQuery]);

  const handleDownload = useCallback(async (paper: PaperItem) => {
    setDownloadingId(paper.id);
    setDownloadError(null);

    try {
      await downloadPaper(paper);
    } catch (error: unknown) {
      setDownloadError({
        id: paper.id,
        message:
          error instanceof PapersApiError
            ? error.message
            : "下载时发生未知错误，请重试。",
      });
    } finally {
      setDownloadingId(null);
    }
  }, []);

  const isLoading = papersState.phase === "loading";

  return (
    <div className="relative min-h-full overflow-hidden">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_15%_0%,rgba(15,107,92,0.12),transparent_42%),radial-gradient(circle_at_95%_10%,rgba(180,134,56,0.12),transparent_38%)]"
      />

      <main className="relative mx-auto flex min-h-screen w-full max-w-4xl flex-col px-6 py-8 sm:px-10 sm:py-10">
        <header className="flex items-center justify-between gap-4">
          <div className="flex items-center gap-3">
            <span className="grid h-10 w-10 place-items-center rounded-xl bg-river text-xs font-bold tracking-wide text-white shadow-sm">
              河海
            </span>
            <div>
              <p className="text-[10px] tracking-[0.28em] text-slate-500">
                HHU EXAM REVIEW
              </p>
              <p className="text-sm font-semibold text-ink">考试复习助手</p>
            </div>
          </div>
          <span className="rounded-full border border-slate-300/80 bg-white/70 px-3 py-1 text-[11px] font-medium text-slate-600">
            本地真实试卷库
          </span>
        </header>

        <section className="mt-12 max-w-2xl sm:mt-16">
          <p className="text-xs font-medium tracking-[0.2em] text-river">
            河海大学 · 历年试卷
          </p>
          <h1 className="mt-4 text-3xl font-semibold leading-tight tracking-tight text-ink sm:text-4xl">
            先找到考过的那几张卷子。
          </h1>
          <p className="mt-4 text-sm leading-7 text-slate-600 sm:text-base">
            可以直接用一句话描述复习需求，系统理解课程和期中/期末后，
            从本地已登记的真实试卷中查找；也可以手动筛选。
            下载得到的是原始 PDF，不做任何改写。
          </p>
        </section>

        <section className="mt-8">
          <NaturalLanguageQuery
            value={naturalInput}
            running={queryState.phase === "running"}
            onChange={setNaturalInput}
            onSubmit={() => void runNaturalQuery(naturalInput)}
            onCancel={cancelNaturalQuery}
          />
          <QueryPanel
            state={queryState}
            downloadingId={downloadingId}
            downloadError={downloadError}
            onDownload={handleDownload}
            onRetry={retryNaturalQuery}
          />
        </section>

        <section className="mt-10" aria-labelledby="manual-heading">
          <div className="mb-3 flex flex-wrap items-baseline justify-between gap-2">
            <h2 id="manual-heading" className="text-sm font-semibold text-ink">
              已登记的试卷
            </h2>
            <p className="text-[11px] text-slate-500">
              默认列出目录全部资料，也可手动筛选
            </p>
          </div>

          <PaperFilters
            course={courseInput}
            examType={examTypeInput}
            loading={isLoading}
            onCourseChange={setCourseInput}
            onExamTypeChange={setExamTypeInput}
            onSubmit={submitQuery}
          />

          <div className="mt-6" aria-live="polite">
            {papersState.phase === "loading" && (
              <div className="space-y-3">
                {[0, 1].map((index) => (
                  <div
                    key={index}
                    className="h-32 animate-pulse rounded-2xl border border-slate-200/90 bg-white/60"
                  />
                ))}
                <p className="text-xs text-slate-500">正在读取试卷目录…</p>
              </div>
            )}

            {papersState.phase === "error" && (
              <div className="rounded-2xl border border-rose-200 bg-rose-50/80 p-5">
                <p className="text-sm font-semibold text-rose-900">
                  没能读取试卷列表
                </p>
                <p className="mt-1 break-words text-sm leading-6 text-rose-800">
                  {papersState.message}
                </p>
                <button
                  type="button"
                  onClick={retryQuery}
                  className="mt-3 inline-flex items-center rounded-xl border border-rose-300 bg-white px-4 py-2 text-sm font-medium text-rose-800 transition hover:bg-rose-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-rose-400 focus-visible:ring-offset-2"
                >
                  重试
                </button>
              </div>
            )}

            {papersState.phase === "ready" && papersState.total === 0 && (
              <div className="rounded-2xl border border-slate-200 bg-white/80 p-6 text-center">
                <p className="text-sm font-semibold text-ink">
                  没有符合条件的试卷
                </p>
                <p className="mt-1 text-sm leading-6 text-slate-600">
                  可以换一个课程关键词（例如“高数”），或把考试类型改回“全部类型”。
                </p>
              </div>
            )}

            {papersState.phase === "ready" && papersState.total > 0 && (
              <>
                <div className="mb-3 flex items-baseline justify-between">
                  <h3 className="text-sm font-semibold text-ink">
                    共 {papersState.total} 份试卷
                  </h3>
                  <p className="text-[11px] text-slate-500">
                    按学年、考试日期从近到远排列
                  </p>
                </div>
                <ul className="space-y-3">
                  {papersState.items.map((paper) => (
                    <PaperCard
                      key={paper.id}
                      paper={paper}
                      downloading={downloadingId === paper.id}
                      downloadError={
                        downloadError?.id === paper.id
                          ? downloadError.message
                          : null
                      }
                      onDownload={handleDownload}
                    />
                  ))}
                </ul>
              </>
            )}
          </div>
        </section>

        <footer className="mt-auto space-y-3 pt-10 text-xs leading-6 text-slate-500">
          <p>
            页面只展示目录里已登记的资料，不生成虚构试卷。
            {papersState.phase === "ready" &&
              `当前手动筛选命中 ${papersState.total} 份。`}
          </p>

          <div className="flex flex-wrap items-center gap-2">
            <span
              className={
                healthState.phase === "ok"
                  ? "inline-flex h-2 w-2 rounded-full bg-emerald-500"
                  : healthState.phase === "checking"
                    ? "inline-flex h-2 w-2 animate-pulse rounded-full bg-slate-400"
                    : "inline-flex h-2 w-2 rounded-full bg-rose-500"
              }
            />
            <span>
              {healthState.phase === "ok" && "后端服务连接正常"}
              {healthState.phase === "checking" && "正在检查后端服务…"}
              {healthState.phase === "error" &&
                `后端服务异常：${healthState.message}`}
            </span>
            {healthState.phase === "error" && (
              <button
                type="button"
                onClick={() => setHealthAttempt((value) => value + 1)}
                className="rounded-lg border border-slate-300 bg-white px-2.5 py-1 text-[11px] font-medium text-slate-700 transition hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-river focus-visible:ring-offset-2"
              >
                重新检查
              </button>
            )}
          </div>
        </footer>
      </main>
    </div>
  );
}
