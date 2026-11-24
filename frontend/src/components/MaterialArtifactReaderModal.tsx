import { useEffect, useState } from "react";
import {
  evaluateQuizQuestion,
  getMaterialPage,
  revealQuizSolution,
  studyFromArtifactKc,
  studyFromArtifactQuestion,
  submitQuizAttempt,
  updateGraphNote,
  type ArtifactDetailView,
  type MaterialPageView,
  type OutputKind,
  type OutputStatus,
} from "../lib/materialsApi.ts";
import MaterialGraphView from "./MaterialGraphView.tsx";
import MaterialNotesView from "./MaterialNotesView.tsx";
import MaterialPageModal from "./MaterialPageModal.tsx";
import MaterialQuizView from "./MaterialQuizView.tsx";

const OUTPUT_TABS: ReadonlyArray<{ id: OutputKind; label: string }> = [
  { id: "notes", label: "章节笔记" },
  { id: "quiz", label: "章节测试" },
  { id: "graph", label: "知识图谱" },
];

function outputStatusLabel(status: OutputStatus | undefined, available: boolean): string {
  if (status === "generating") return "生成中";
  if (status === "failed") return "失败";
  if (status === "ready" || available) return "可打开";
  return "未生成";
}

export type ReaderStudyTarget = {
  artifactId: string;
  questionId?: string;
  kcId?: string;
  mode: "explain" | "exercise" | "evaluate";
};

type MaterialArtifactReaderModalProps = {
  artifact: ArtifactDetailView;
  displayName: string;
  pageCount: number;
  initialTab?: OutputKind;
  conversationId?: string;
  retryingOutput?: OutputKind | null;
  inlineOutput?: OutputKind;
  inConversation?: boolean;
  onUpdateArtifact: (updated: ArtifactDetailView) => void;
  onGenerateOutput?: (artifact: ArtifactDetailView, output: OutputKind) => Promise<void>;
  onStartStudy: (
    conversationId: string,
    prompt: string,
    target: ReaderStudyTarget,
  ) => void;
  onClose?: () => void;
};

export default function MaterialArtifactReaderModal({
  artifact,
  displayName,
  pageCount,
  initialTab,
  conversationId,
  retryingOutput = null,
  inlineOutput,
  inConversation = false,
  onUpdateArtifact,
  onGenerateOutput,
  onStartStudy,
  onClose,
}: MaterialArtifactReaderModalProps) {
  const inline = inConversation || Boolean(inlineOutput);
  const outputLabel = OUTPUT_TABS.find(({ id }) => id === inlineOutput)?.label;
  const hasNotes = Boolean(artifact.notes_html.trim()) || artifact.notes_status === "ready";
  const hasQuiz = artifact.questions.length > 0 || artifact.quiz_status === "ready";
  const graphStatus: OutputStatus =
    artifact.graph_status ?? (artifact.graph_tree?.nodes?.length ? "ready" : "none");
  const hasGraph = graphStatus === "ready";

  const pickDefaultTab = (): OutputKind => {
    if (inlineOutput) return inlineOutput;
    if (initialTab) return initialTab;
    if (hasNotes) return "notes";
    if (hasQuiz) return "quiz";
    if (hasGraph) return "graph";
    return "notes";
  };

  const [tab, setTab] = useState<OutputKind>(pickDefaultTab);
  const [quizBusyId, setQuizBusyId] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [modalPage, setModalPage] = useState<{
    materialId: string;
    pageNo: number;
    loading: boolean;
    error: string | null;
    data: MaterialPageView | null;
  } | null>(null);

  useEffect(() => {
    const nextTab = inlineOutput ?? initialTab;
    if (nextTab) setTab(nextTab);
  }, [artifact.id, initialTab, inlineOutput]);

  useEffect(() => {
    if (inline) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !modalPage) {
        onClose?.();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [inline, modalPage, onClose]);

  const openPage = async (pageNo: number) => {
    const pending = { materialId: artifact.material_id, pageNo, loading: true, error: null, data: null };
    setModalPage(pending);
    try {
      const data = await getMaterialPage(artifact.material_id, pageNo);
      setModalPage((current) => current === pending ? { ...pending, loading: false, data } : current);
    } catch (err) {
      const error = err instanceof Error ? err.message : "无法读取该物理页内容。";
      setModalPage((current) => current === pending ? { ...pending, loading: false, error } : current);
    }
  };

  const notesStatus: OutputStatus =
    artifact.notes_status ?? (hasNotes ? "ready" : "none");
  const quizStatus: OutputStatus =
    artifact.quiz_status ?? (hasQuiz ? "ready" : "none");
  const statusByTab: Record<OutputKind, { status: OutputStatus; available: boolean; error: string | null }> = {
    notes: {
      status: retryingOutput === "notes" ? "generating" : notesStatus,
      available: hasNotes,
      error: artifact.notes_error_message ?? null,
    },
    quiz: {
      status: retryingOutput === "quiz" ? "generating" : quizStatus,
      available: hasQuiz,
      error: artifact.quiz_error_message ?? null,
    },
    graph: {
      status: retryingOutput === "graph" ? "generating" : graphStatus,
      available: hasGraph,
      error: artifact.graph_error_message ?? null,
    },
  };

  const activeInfo = statusByTab[tab];
  const canStudyKc = Boolean(artifact.skeleton) && (hasNotes || hasGraph || hasQuiz);
  const coverageLabel = artifact.coverage?.missing_pages.length ? " · 资料有缺页" : "";

  return (
    <div
      id={inlineOutput === "quiz" ? "material-quiz-" + artifact.id : undefined}
      className={inlineOutput === "graph" ? "conversation-inline-graph" : inline ? "min-w-0" : "material-modal-backdrop"}
      role={inline ? "region" : "dialog"}
      aria-modal={inline ? undefined : true}
      aria-label={(outputLabel ?? "复习阅读区") + "：" + artifact.title}
    >
      <div className={inline ? "rounded-xl border border-slate-200 bg-white p-4" : "material-modal-card max-h-[92vh] w-[min(1080px,96vw)] overflow-y-auto"}>
        <header className="material-modal-header">
          <div>
            <p className="eyebrow">
              {displayName} · 第 {artifact.start_page}–{artifact.end_page} 页{coverageLabel}
            </p>
            <h2>{outputLabel ?? artifact.title}</h2>
          </div>
          {onClose && <div className="material-modal-actions">
            <button type="button" className="quiet-button" onClick={onClose}>
              {inlineOutput === "quiz" ? "收起章节测试" : inline ? "收起复习成果" : "回到会话"}
            </button>
          </div>}
        </header>

        {actionError && (
          <div role="alert" className="inline-alert my-2">
            {actionError}
          </div>
        )}

        {!inlineOutput && <div className="epa-tabs" role="tablist" aria-label="复习成果切换">
          {OUTPUT_TABS.map(({ id, label }) => {
            const info = statusByTab[id];
            const stateText =
              id === "quiz" && info.available
                ? `${artifact.questions.length} 题`
                : outputStatusLabel(info.status, info.available);
            return (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                className={`epa-tab-btn ${tab === id ? "is-active" : ""}`}
                onClick={() => setTab(id)}
              >
                <strong>{label}</strong>
                <span>{stateText}</span>
              </button>
            );
          })}
        </div>}

        {!activeInfo.available && (
          <div className="material-reader-status" role="status">
            <div>
              <strong>
                {activeInfo.status === "generating"
                  ? `正在生成${OUTPUT_TABS.find((t) => t.id === tab)?.label ?? ""}…`
                  : activeInfo.status === "failed"
                    ? `${OUTPUT_TABS.find((t) => t.id === tab)?.label ?? ""}生成失败`
                    : `尚未生成${OUTPUT_TABS.find((t) => t.id === tab)?.label ?? ""}`}
              </strong>
              <p>
                {activeInfo.error ||
                  (activeInfo.status === "failed"
                    ? "可以只重试本项，其他已完成的成果保留可用。"
                    : "点击下方按钮按需生成，会复用本页段已提取的知识骨架。")}
              </p>
            </div>
            {onGenerateOutput && activeInfo.status !== "generating" && (
              <button
                type="button"
                className="primary-button"
                disabled={retryingOutput !== null}
                onClick={() => {
                  setActionError(null);
                  void onGenerateOutput(artifact, tab).catch((err) => {
                    setActionError(err instanceof Error ? err.message : "生成失败，请重试。");
                  });
                }}
              >
                {retryingOutput === tab
                  ? "正在生成…"
                  : activeInfo.status === "failed"
                    ? "重试本项"
                    : "生成本项"}
              </button>
            )}
          </div>
        )}

        {tab === "notes" && hasNotes && (
          <MaterialNotesView
            materialId={artifact.material_id}
            pageCount={pageCount}
            notesHtml={artifact.notes_html}
            showPageRange={!inline}
            partial={Boolean(artifact.coverage?.missing_pages.length)}
            startPage={artifact.start_page}
            endPage={artifact.end_page}
            kcs={
              ((artifact.skeleton?.chapters as Array<{ kcs?: Array<{ id: string; label: string }> }> | undefined) ?? [])
                .flatMap((ch) => ch.kcs ?? [])
            }
            onStudyKc={
              canStudyKc
                ? (kcId, mode) => {
                    setActionError(null);
                    void studyFromArtifactKc(artifact.id, kcId, mode, conversationId)
                      .then((res) => {
                        onStartStudy(res.conversation_id, res.prompt, {
                          artifactId: res.artifact_id,
                          kcId: res.kc_id,
                          mode: res.mode,
                        });
                      })
                      .catch((err) => {
                        setActionError(err instanceof Error ? err.message : "无法在会话中发起追问。");
                      });
                  }
                : undefined
            }
          />
        )}

        {tab === "quiz" && hasQuiz && (
          <MaterialQuizView
            key={artifact.id}
            oneAtATime={inline}
            questions={artifact.questions}
            submittingId={quizBusyId}
            onSubmitAttempt={async (questionId, userAnswer) => {
              setQuizBusyId(questionId);
              setActionError(null);
              try {
                const updated = await submitQuizAttempt(artifact.id, questionId, userAnswer);
                onUpdateArtifact(updated);
              } catch (err) {
                setActionError(err instanceof Error ? err.message : "提交作答失败。");
              } finally {
                setQuizBusyId(null);
              }
            }}
            onRevealSolution={async (questionId) => {
              setQuizBusyId(questionId);
              setActionError(null);
              try {
                const updated = await revealQuizSolution(artifact.id, questionId);
                onUpdateArtifact(updated);
              } catch (err) {
                setActionError(err instanceof Error ? err.message : "无法查看参考答案。");
              } finally {
                setQuizBusyId(null);
              }
            }}
            onEvaluateQuestion={async (questionId) => {
              setQuizBusyId(questionId);
              setActionError(null);
              try {
                const updated = await evaluateQuizQuestion(artifact.id, questionId);
                onUpdateArtifact(updated);
              } catch (err) {
                setActionError(err instanceof Error ? err.message : "AI 评价作答失败。");
              } finally {
                setQuizBusyId(null);
              }
            }}
            onOpenPage={(pageNo) => void openPage(pageNo)}
            onStudyQuestion={(questionId, mode) => {
              setActionError(null);
              void studyFromArtifactQuestion(artifact.id, questionId, mode, conversationId)
                .then((res) => {
                  onStartStudy(res.conversation_id, res.prompt, {
                    artifactId: res.artifact_id,
                    questionId: res.question_id,
                    mode: res.mode,
                  });
                })
                .catch((err) => {
                  setActionError(err instanceof Error ? err.message : "无法发起题目追问。");
                });
            }}
          />
        )}

        {tab === "graph" && hasGraph && (
          <MaterialGraphView
            inline={inline}
            startPage={artifact.start_page}
            endPage={artifact.end_page}
            onOpenPage={(pageNo) => void openPage(pageNo)}
            graphTree={artifact.graph_tree}
            graphNotes={artifact.graph_notes}
            onStudyKc={
              canStudyKc
                ? (kcId, mode) => {
                    setActionError(null);
                    void studyFromArtifactKc(artifact.id, kcId, mode, conversationId)
                      .then((res) => {
                        onStartStudy(res.conversation_id, res.prompt, {
                          artifactId: res.artifact_id,
                          kcId: res.kc_id,
                          mode: res.mode,
                        });
                      })
                      .catch((err) => {
                        setActionError(err instanceof Error ? err.message : "无法在会话中发起追问。");
                      });
                  }
                : undefined
            }
            onSaveNote={async (nodeId, noteText) => {
              const saved = await updateGraphNote(artifact.id, nodeId, noteText);
              onUpdateArtifact({
                ...artifact,
                graph_notes: {
                  ...artifact.graph_notes,
                  [saved.node_id]: saved.note_text,
                },
              });
            }}
          />
        )}

        {modalPage && (
          <MaterialPageModal
            materialId={modalPage.materialId}
            pageNo={modalPage.pageNo}
            highlightQuote={null}
            loading={modalPage.loading}
            error={modalPage.error}
            pageData={modalPage.data}
            onClose={() => setModalPage(null)}
          />
        )}
      </div>
    </div>
  );
}
