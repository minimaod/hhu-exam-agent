import { useCallback, useEffect, useRef, useState } from "react";
import ConversationWorkspace from "./components/ConversationWorkspace";
import MistakeBook from "./components/MistakeBook";
import PaperCard from "./components/PaperCard";
import PaperFilters from "./components/PaperFilters";
import { fetchHealth, HealthCheckError } from "./lib/healthApi";
import {
  downloadPaper,
  fetchPapers,
  PapersApiError,
  type ExamTypeFilter,
  type PaperItem,
} from "./lib/papersApi";
import type { DownloadState, DownloadStates } from "./lib/display";

type View = "conversations" | "mistakes" | "papers";

type PapersState =
  | { phase: "loading" }
  | { phase: "ready"; items: PaperItem[]; total: number }
  | { phase: "error"; message: string };

type HealthState =
  | { phase: "checking" }
  | { phase: "ok" }
  | { phase: "error"; message: string };

type SubmittedQuery = {
  course: string;
  examType: ExamTypeFilter;
  seq: number;
};

const INITIAL_QUERY: SubmittedQuery = { course: "", examType: "all", seq: 0 };

export default function App() {
  const [view, setView] = useState<View>("conversations");
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(null);

  const [courseInput, setCourseInput] = useState("");
  const [examTypeInput, setExamTypeInput] = useState<ExamTypeFilter>("all");
  const [submitted, setSubmitted] = useState<SubmittedQuery>(INITIAL_QUERY);

  const [papersState, setPapersState] = useState<PapersState>({
    phase: "loading",
  });
  const [downloads, setDownloads] = useState<DownloadStates>({});
  const downloadTimersRef = useRef<Record<string, number>>({});
  const downloadInFlightRef = useRef<Set<string>>(new Set());

  const [healthState, setHealthState] = useState<HealthState>({
    phase: "checking",
  });
  const [healthAttempt, setHealthAttempt] = useState(0);

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

  const setDownloadState = useCallback(
    (id: string, state: DownloadState | null) => {
      setDownloads((current) => {
        const next = { ...current };
        if (state === null) {
          delete next[id];
        } else {
          next[id] = state;
        }
        return next;
      });
    },
    [],
  );

  const handleDownload = useCallback(
    async (paper: PaperItem) => {
      // 同一份文件正在请求时忽略重复点击；不靠按钮禁用兜底。
      if (downloadInFlightRef.current.has(paper.id)) {
        return;
      }
      downloadInFlightRef.current.add(paper.id);
      window.clearTimeout(downloadTimersRef.current[paper.id]);
      setDownloadState(paper.id, { phase: "downloading" });

      try {
        await downloadPaper(paper);
        setDownloadState(paper.id, {
          phase: "triggered",
          message: "已开始下载，请在浏览器下载栏确认是否保存成功。",
        });
        downloadTimersRef.current[paper.id] = window.setTimeout(() => {
          setDownloadState(paper.id, null);
        }, 10_000);
      } catch (error: unknown) {
        setDownloadState(paper.id, {
          phase: "error",
          message:
            error instanceof PapersApiError
              ? error.message
              : "下载时发生未知错误，请重试。",
        });
      } finally {
        downloadInFlightRef.current.delete(paper.id);
      }
    },
    [setDownloadState],
  );

  useEffect(
    () => () => {
      Object.values(downloadTimersRef.current).forEach((timer) =>
        window.clearTimeout(timer),
      );
    },
    [],
  );

  const openRecord = useCallback((recordId: string) => {
    setSelectedRecordId(recordId);
    setView("mistakes");
  }, []);

  const isLoading = papersState.phase === "loading";

  return (
    <div className="relative h-screen h-[100dvh] overflow-hidden">
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_15%_0%,rgba(15,107,92,0.12),transparent_42%),radial-gradient(circle_at_95%_10%,rgba(180,134,56,0.12),transparent_38%)]"
      />

      <main className="relative mx-auto flex h-full w-full max-w-6xl flex-col px-4 py-3 sm:px-6 sm:py-4">
        <header className="flex shrink-0 flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2.5">
            <span className="grid h-9 w-9 place-items-center rounded-xl bg-river text-[11px] font-bold tracking-wide text-white shadow-sm">
              河海
            </span>
            <div>
              <p className="text-[9px] tracking-[0.24em] text-slate-500">
                HHU EXAM REVIEW
              </p>
              <p className="text-sm font-semibold text-ink">考试复习助手</p>
            </div>
          </div>

          <nav className="order-3 flex w-full items-center gap-1 rounded-xl border border-slate-200 bg-white/75 p-1 sm:order-none sm:w-auto">
            {(
              [
                ["conversations", "学习会话"],
                ["mistakes", "错题本"],
                ["papers", "试卷库"],
              ] as const
            ).map(([key, label]) => (
              <button
                key={key}
                type="button"
                onClick={() => {
                  if (key === "mistakes") {
                    setSelectedRecordId(null);
                  }
                  setView(key);
                }}
                aria-current={view === key ? "page" : undefined}
                className={`flex-1 rounded-lg px-3 py-1.5 text-xs font-medium transition sm:flex-none ${
                  view === key
                    ? "bg-river text-white"
                    : "text-slate-600 hover:bg-slate-100"
                }`}
              >
                {label}
              </button>
            ))}
          </nav>

          <div className="flex items-center gap-2 text-[11px] text-slate-500">
            <span
              className={
                healthState.phase === "ok"
                  ? "inline-flex h-2 w-2 rounded-full bg-emerald-500"
                  : healthState.phase === "checking"
                    ? "inline-flex h-2 w-2 animate-pulse rounded-full bg-slate-400"
                    : "inline-flex h-2 w-2 rounded-full bg-rose-500"
              }
            />
            <span className="hidden sm:inline">
              {healthState.phase === "ok" && "服务正常"}
              {healthState.phase === "checking" && "检查服务…"}
              {healthState.phase === "error" && "服务异常"}
            </span>
            {healthState.phase === "error" && (
              <button
                type="button"
                onClick={() => setHealthAttempt((value) => value + 1)}
                className="rounded-lg border border-slate-300 bg-white px-2 py-0.5 text-[10px] font-medium text-slate-700 hover:bg-slate-100"
              >
                重试
              </button>
            )}
          </div>
        </header>

        <div className="mt-3 min-h-0 flex-1">
          {view === "conversations" && (
            <ConversationWorkspace
              downloads={downloads}
              onDownload={handleDownload}
              onOpenRecord={openRecord}
            />
          )}

          {view === "mistakes" && (
            <div className="h-full min-h-0 overflow-y-auto pr-1">
              <MistakeBook
                selectedId={selectedRecordId}
                onSelectRecord={setSelectedRecordId}
              />
            </div>
          )}

          {view === "papers" && (
            <div className="h-full min-h-0 overflow-y-auto pr-1">
              <div className="mx-auto max-w-4xl pb-8">
                <section className="mt-2 max-w-2xl">
                  <p className="text-xs font-medium tracking-[0.2em] text-river">
                    河海大学 · 历年试卷与错题辅导
                  </p>
                  <h1 className="mt-3 text-2xl font-semibold leading-tight tracking-tight text-ink sm:text-3xl">
                    找到卷子，也把不会的那道题弄懂。
                  </h1>
                  <p className="mt-3 text-sm leading-7 text-slate-600">
                    在“学习会话”里可以直接输入题目、上传图片、提交自己的答案或解法。
                    系统会先判断查试卷还是辅导题目；题目不清楚时先补充，再给知识点、步骤和
                    针对你解法的错误分析。会话自动保存，只有“记录这题”才加入错题本。
                  </p>
                </section>

                <section className="mt-8" aria-labelledby="manual-heading">
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
                            className="h-28 animate-pulse rounded-2xl border border-slate-200/90 bg-white/60"
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
                          className="mt-3 inline-flex items-center rounded-xl border border-rose-300 bg-white px-4 py-2 text-sm font-medium text-rose-800 transition hover:bg-rose-100"
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
                              download={downloads[paper.id] ?? null}
                              onDownload={handleDownload}
                            />
                          ))}
                        </ul>
                      </>
                    )}
                  </div>
                </section>

                <p className="mt-8 text-xs leading-6 text-slate-500">
                  页面只展示目录里已登记的资料，不生成虚构试卷；题目讲解来自模型，
                  请结合教材核对。
                </p>
              </div>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
