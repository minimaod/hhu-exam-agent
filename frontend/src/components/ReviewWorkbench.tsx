import "./reviewWorkbench.css";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  evaluateQuizQuestion, getMaterialPage, materialPageImageUrl, materialPreviewUrl, revealQuizSolution, submitQuizAttempt, updateKnowledgePointNote,
  type ArtifactDetailView, type MaterialPageView, type OutputKind, type QuizQuestionPublicView,
} from "../lib/materialsApi";
import {
  outputAvailable, readCollapsedDirectory, readReviewProgress, reviewKcIndex, reviewStatus, reviewStatusLabels,
  saveReviewPreference, saveReviewProgress, writeReviewHash, type ReviewPosition,
} from "../lib/reviewWorkbench";
import MaterialQuizView from "./MaterialQuizView";
import { ReviewGraph } from "./ReviewWorkbenchGraph";
import { readWorkbenchNotes, ReviewNotes } from "./ReviewWorkbenchNotes";

const tabs = [["notes", "笔记"], ["graph", "图谱"], ["quiz", "测试"]] as const;
const isCompact = () => window.matchMedia?.("(max-width: 720px)").matches ?? false;

export type ReviewAskTarget = { kcId?: string; questionId?: string; pages: number[] };
export type ReviewAskPanel = (target: ReviewAskTarget, view: OutputKind, close: () => void) => React.ReactNode;

function Annotation({ initial, save }: { initial: string; save: (text: string) => Promise<void> }) {
  const [draft, setDraft] = useState(initial);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(false);
  return (
    <details className="review-annotation">
      <summary>我的批注{initial ? " · 已保存" : ""}</summary>
      <textarea aria-label="知识点批注" rows={3} maxLength={10000} value={draft} onChange={(event) => setDraft(event.target.value)} />
      <button type="button" className="review-button" disabled={saving || draft.trim() === initial}
        onClick={() => {
          setSaving(true);
          setError(false);
          void save(draft.trim()).catch(() => setError(true)).finally(() => setSaving(false));
        }}>
        {saving ? "保存中…" : "保存批注"}
      </button>
      {error && <p role="alert">批注保存失败，请重试。</p>}
    </details>
  );
}

function OriginalPage({ materialId, page, total, onPage, close }: {
  materialId: string; page: number; total: number; onPage: (page: number) => void; close: () => void;
}) {
  const [failed, setFailed] = useState(false);
  const [showText, setShowText] = useState(false);
  const [text, setText] = useState<MaterialPageView | null>();
  useEffect(() => {
    if (!showText) return;
    let active = true;
    void getMaterialPage(materialId, page).then((value) => {
      if (active) setText(value);
    }).catch(() => { if (active) setText(null); });
    return () => { active = false; };
  }, [materialId, page, showText]);
  return (
    <aside className="review-side-pane" aria-label="原页对照" data-show-text={showText}>
      <div className="review-result-heading">
        <strong>原页 · 第 {page} / {total} 页</strong>
        <button type="button" className="review-button" onClick={close}>关闭</button>
      </div>
      <div className="review-result-links">
        <button type="button" className="review-button" disabled={page <= 1} onClick={() => onPage(page - 1)}>上一页</button>
        <button type="button" className="review-button" disabled={page >= total} onClick={() => onPage(page + 1)}>下一页</button>
        <a className="review-link" href={materialPreviewUrl(materialId, page)} target="_blank" rel="noreferrer">打开原 PDF</a>
        <button type="button" className="review-button" aria-expanded={showText} onClick={() => setShowText(!showText)}>
          {showText ? "收起提取文本" : "查看提取文本"}
        </button>
      </div>
      {failed ? <p>原页图片加载失败，可打开原 PDF。</p> : (
        <img src={materialPageImageUrl(materialId, page)} alt={`资料第 ${page} 页原件`} onError={() => setFailed(true)} />
      )}
      {showText && (text === undefined ? <p role="status">正在读取提取文本…</p>
        : text === null ? <p role="alert">无法读取该页提取文本。</p>
        : <pre className="review-source-text">{text.extracted_text || "该页没有可用的提取文本。"}</pre>)}
    </aside>
  );
}

export default function ReviewWorkbench({
  artifact, displayName, pageCount, conversationId, accountKey, progressKey, initial, retrying, actionError,
  onClose, onUpdate, onGenerate, renderAsk,
}: {
  artifact: ArtifactDetailView;
  displayName: string;
  pageCount: number;
  conversationId: string;
  accountKey: string;
  progressKey: string;
  initial?: ReviewPosition;
  retrying: OutputKind | null;
  actionError?: string | null;
  onClose: () => void;
  onUpdate: (artifact: ArtifactDetailView) => void;
  onGenerate: (artifact: ArtifactDetailView, output: OutputKind) => Promise<void>;
  renderAsk?: ReviewAskPanel;
}) {
  const index = useMemo(() => reviewKcIndex(artifact), [artifact]);
  const points = Object.entries(index);
  const blocks = useMemo(() => readWorkbenchNotes(artifact.notes_html ?? "", index), [artifact.notes_html, index]);
  const [position, setPosition] = useState<ReviewPosition>(() => ({
    view: initial?.view ?? tabs.find(([view]) => outputAvailable(artifact, view))?.[0] ?? "notes",
    kcId: initial?.kcId && index[initial.kcId] ? initial.kcId : undefined,
  }));
  const [collapsed, setCollapsed] = useState(() => readCollapsedDirectory(accountKey));
  const [quick, setQuick] = useState(false);
  const [original, setOriginal] = useState(false);
  const [manualPage, setManualPage] = useState<number>();
  const [askTarget, setAskTarget] = useState<(ReviewAskTarget & { view: OutputKind })>();
  const [highlighted, setHighlighted] = useState<string>();
  const [progress, setProgress] = useState(() => readReviewProgress(progressKey));
  const [pendingQuestion, setPendingQuestion] = useState<string | null>(null);
  const pendingQuestionRef = useRef(false);
  const [quizError, setQuizError] = useState<string>();
  const [annotationEdits, setAnnotationEdits] = useState<Record<string, string>>({});
  const dialogRef = useRef<HTMLDialogElement>(null);
  const scroller = useRef<HTMLDivElement>(null);
  const directoryRef = useRef<HTMLElement>(null);
  const [directoryTop, setDirectoryTop] = useState(0);
  const skipSpy = useRef(false);
  const [jump, setJump] = useState<ReviewPosition | null>(position);
  const totalPages = Math.max(pageCount, artifact.end_page);
  const noteIds = blocks.flatMap((block) => block.kcId ?? []);
  const notePages = (kcId?: string) => [
    ...new Set([...(blocks.find((block) => block.kcId === kcId)?.pages ?? []), ...(kcId ? index[kcId]?.pages ?? [] : [])]),
  ].filter((page) => page >= 1 && page <= totalPages);
  const page = manualPage ?? notePages(position.kcId)[0] ?? artifact.start_page;
  const questionsByKc = new Map<string, QuizQuestionPublicView[]>();
  const unlinked: QuizQuestionPublicView[] = [];
  for (const question of artifact.questions) {
    if (!index[question.kc_id]) {
      unlinked.push(question);
      continue;
    }
    const group = questionsByKc.get(question.kc_id) ?? [];
    group.push(question);
    questionsByKc.set(question.kc_id, group);
  }
  const statuses = Object.fromEntries(points.map(([id]) => [id, reviewStatus(questionsByKc.get(id) ?? [], progress.read.includes(id))]));
  const groups = new Map<string, Array<[string, typeof index[string]]>>();
  for (const [id, point] of points) {
    const group = groups.get(point.module) ?? [];
    group.push([id, point]);
    groups.set(point.module, group);
  }

  useEffect(() => {
    const dialog = dialogRef.current;
    if (dialog && !dialog.open) {
      if (typeof dialog.showModal === "function") dialog.showModal();
      else dialog.setAttribute("open", "");
    }
    return () => { if (dialog?.open) dialog.close?.(); };
  }, []);
  useEffect(() => {
    const pane = scroller.current;
    const directory = directoryRef.current;
    if (!pane || !directory || typeof ResizeObserver === "undefined") return;
    const update = () => setDirectoryTop(Math.min(0, pane.clientHeight - directory.offsetHeight));
    const observer = new ResizeObserver(update);
    observer.observe(directory);
    observer.observe(pane);
    update();
    return () => observer.disconnect();
  }, []);
  useEffect(() => { writeReviewHash(conversationId, artifact.id, position); }, [artifact.id, conversationId, position]);
  useEffect(() => {
    if (!highlighted) return;
    const timer = window.setTimeout(() => setHighlighted(undefined), 1450);
    return () => window.clearTimeout(timer);
  }, [highlighted]);

  const markRead = (kcId: string) => {
    saveReviewProgress(progressKey, kcId);
    setProgress(readReviewProgress(progressKey));
  };
  const onScroll = () => {
    const pane = scroller.current;
    if (skipSpy.current || position.view !== "notes" || !pane || askTarget) return;
    const line = pane.getBoundingClientRect().top + Math.min(140, pane.clientHeight / 3);
    const sections = [...pane.querySelectorAll<HTMLElement>("[data-review-section]")];
    // Ignore legacy sections without a stable link, but don't mark the next one before it arrives.
    let section: HTMLElement | undefined;
    for (const item of sections) {
      if (item.getBoundingClientRect().top > line) break;
      section = item;
    }
    const id = section?.dataset.reviewSection;
    if (id && id !== position.kcId) {
      setPosition({ view: "notes", kcId: id });
      setManualPage(undefined);
      markRead(id);
    }
  };

  useEffect(() => {
    const pane = scroller.current;
    if (!pane || !jump) return;
    const selector = jump.view === "notes" ? "[data-review-section]" : jump.view === "quiz" ? "[data-review-question-group]" : "[data-review-graph-node]";
    const target = [...pane.querySelectorAll<HTMLElement>(selector)].find((node) =>
      (node.dataset.reviewSection ?? node.dataset.reviewQuestionGroup ?? node.dataset.reviewGraphNode) === jump.kcId);
    skipSpy.current = true;
    if (jump.view === "graph") {
      pane.scrollTop = 0;
      const bounds = pane.getBoundingClientRect();
      if (target && target.getBoundingClientRect().bottom > bounds.bottom - 16) {
        pane.scrollTop = Math.max(0, target.getBoundingClientRect().top - bounds.top - pane.clientHeight / 2);
      }
    } else {
      pane.scrollTop = target ? pane.scrollTop + target.getBoundingClientRect().top - pane.getBoundingClientRect().top - 16 : 0;
    }
    const id = jump.view === "notes" ? target?.dataset.reviewSection : undefined;
    if (id) {
      saveReviewProgress(progressKey, id);
      setProgress(readReviewProgress(progressKey));
    }
    const frame = requestAnimationFrame(() => {
      skipSpy.current = false;
      setJump(null);
      if (!target && jump.view === "notes") onScroll();
    });
    return () => cancelAnimationFrame(frame);
    // A jump runs once after its destination is rendered.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [jump, progressKey]);

  const go = (kcId: string | undefined, view: OutputKind) => {
    const next = { view, kcId };
    setPosition(next);
    setJump(next);
    setManualPage(undefined);
    if (view === "notes" && kcId && index[kcId]?.note_anchor) setHighlighted(kcId);
  };
  const selectGraph = (kcId?: string) => {
    setPosition({ view: "graph", kcId });
    setManualPage(undefined);
  };
  const focusDirectory = (id: string) => {
    const point = index[id];
    const present = { notes: point.note_anchor, graph: point.graph_node_id, quiz: point.question_ids.length };
    const next = present[position.view] ? position.view : tabs.find(([view]) => present[view])?.[0];
    if (next) go(id, next);
  };
  const openOriginal = (pageNo: number, kcId?: string) => {
    setManualPage(Math.max(1, Math.min(pageNo, totalPages)));
    if (kcId) setPosition({ view: position.view, kcId });
    setAskTarget(undefined);
    setOriginal(true);
  };
  const ask = (kcId?: string, question?: QuizQuestionPublicView) => {
    if (!renderAsk) return;
    setAskTarget({ view: position.view, kcId, questionId: question?.id, pages: question?.source_pages.length ? question.source_pages : notePages(kcId) });
  };

  const runQuiz = async (questionId: string, action: () => Promise<ArtifactDetailView>) => {
    if (pendingQuestionRef.current) return;
    pendingQuestionRef.current = true;
    setPendingQuestion(questionId);
    setQuizError(undefined);
    try {
      onUpdate(await action());
    } catch (error) {
      setQuizError(error instanceof Error ? error.message : "作答操作失败，请重试。");
    } finally {
      pendingQuestionRef.current = false;
      setPendingQuestion(null);
    }
  };
  const annotation = (kcId: string) => {
    const point = index[kcId];
    const oldNote = point.graph_node_id ? artifact.graph_notes?.[point.graph_node_id] : undefined;
    const text = annotationEdits[kcId] ?? artifact.kc_notes?.[kcId] ?? oldNote ?? "";
    return (
      <Annotation key={kcId} initial={text} save={async (value) => {
        const note = await updateKnowledgePointNote(artifact.id, kcId, value);
        setAnnotationEdits((current) => ({ ...current, [kcId]: note.note_text }));
      }} />
    );
  };
  const quizGroup = (id: string | undefined, questions: QuizQuestionPublicView[]) => (
    <section className="review-question-group" key={id ?? "unlinked"} data-review-question-group={id}>
      <div className="review-result-heading">
        <h3>{id ? index[id].label : "未关联知识点的题目"}</h3>
        {id && index[id]?.note_anchor && <button type="button" className="review-link" onClick={() => go(id, "notes")}>回看笔记</button>}
      </div>
      <MaterialQuizView questions={questions} submittingId={pendingQuestion} disableActions={pendingQuestion !== null} hideSummary
        onSubmitAttempt={(qid, answer) => runQuiz(qid, () => submitQuizAttempt(artifact.id, qid, answer))}
        onRevealSolution={(qid) => runQuiz(qid, () => revealQuizSolution(artifact.id, qid))}
        onEvaluateQuestion={(qid) => runQuiz(qid, () => evaluateQuizQuestion(artifact.id, qid))}
        onOpenPage={openOriginal}
        onStudyQuestion={renderAsk ? (qid) => {
          const question = questions.find((item) => item.id === qid);
          if (question) ask(id, question);
        } : undefined}
        studyLabel="问 AI"
        onReviewNote={id && index[id]?.note_anchor ? () => go(id, "notes") : undefined} />
    </section>
  );
  useEffect(() => {
    const pane = scroller.current;
    const side = pane?.querySelector(".review-side-pane");
    if (!original || askTarget || !isCompact() || !pane || !side) return;
    pane.scrollTop += side.getBoundingClientRect().top - pane.getBoundingClientRect().top;
  }, [original, askTarget]);
  const closeOriginal = () => {
    setOriginal(false);
    if (isCompact()) setJump({ ...position });
  };
  const closeAsk = () => {
    setAskTarget(undefined);
    if (!original && isCompact()) setJump({ ...position });
  };
  const available = outputAvailable(artifact, position.view);
  const summary = position.kcId ? blocks.find((block) => block.kcId === position.kcId)?.quickHtml : undefined;
  const readCount = noteIds.filter((id) => progress.read.includes(id)).length;
  const hasSidePane = original || Boolean(askTarget && renderAsk);
  const linkWarnings = (artifact.coverage_warnings ?? []).filter((warning) =>
    warning.startsWith("知识点索引：") || warning.startsWith(position.view === "graph" ? "图谱覆盖：" : "笔记锚点："));
  const toggleDirectory = () => {
    saveReviewPreference(accountKey + ".review-directory", collapsed ? "expanded" : "collapsed");
    setCollapsed(!collapsed);
  };
  return (
    <dialog className="review-workbench" ref={dialogRef} aria-label="复习工作台"
      onCancel={(event) => { event.preventDefault(); if (askTarget) closeAsk(); else onClose(); }}>
      <header className="review-workbench-header">
        <button type="button" className="review-button" onClick={onClose}>返回会话</button>
        <div className="review-workbench-title"><strong>{displayName}</strong><span>第 {artifact.start_page}–{artifact.end_page} 页</span></div>
        <nav aria-label="复习成果切换" className="review-tabs">
          {tabs.map(([view, label]) => (
            <button type="button" key={view} className="review-button" aria-pressed={position.view === view}
              onClick={() => position.view !== view && go(position.kcId, view)}>{label}</button>
          ))}
        </nav>
        <div className="review-toolbar">
          <button type="button" className="review-button review-mobile-directory"
            aria-expanded={!collapsed} aria-label={collapsed ? "展开目录" : "收起目录"} onClick={() => {
              toggleDirectory();
              if (scroller.current && collapsed) scroller.current.scrollTop = 0;
            }}>目录</button>
          <button type="button" className="review-button" aria-pressed={quick} disabled={position.view !== "notes" || !available}
            onClick={() => setQuick(!quick)}>速览</button>
          <button type="button" className="review-button" aria-pressed={original && !askTarget} onClick={() => {
            if (askTarget) { setAskTarget(undefined); setOriginal(true); }
            else if (original) closeOriginal();
            else setOriginal(true);
          }}>原页对照</button>
          {renderAsk && <button type="button" className="review-primary" disabled={!available && !askTarget} aria-pressed={Boolean(askTarget)}
            onClick={() => askTarget ? closeAsk() : ask(position.kcId)}>问 AI</button>}
        </div>
        <progress aria-label={`阅读进度 ${readCount}/${noteIds.length}`} max={noteIds.length || 1} value={readCount} />
      </header>
      <div ref={scroller} className="review-workbench-scroll" onScroll={onScroll}>
        <div className="review-workbench-grid" data-collapsed={collapsed} data-side={hasSidePane}>
          <aside ref={directoryRef} className="review-directory" aria-label="知识点目录" style={{ top: directoryTop }}>
            <button type="button" className="review-button review-desktop-directory" aria-expanded={!collapsed}
              aria-label={collapsed ? "展开目录" : "收起目录"} onClick={toggleDirectory}>{collapsed ? "› 目录" : "‹ 目录"}</button>
            {!collapsed && <nav>
              {[...groups].map(([name, items]) => (
                <div className="review-directory-group" key={name}>
                  {name && <strong>{name}</strong>}
                  {items.map(([id, point]) => (
                    <button type="button" key={id} data-review-status={statuses[id]}
                      aria-current={id === position.kcId ? "location" : undefined}
                      title={`${point.label} · ${reviewStatusLabels[statuses[id]]}`}
                      disabled={!point.note_anchor && !point.graph_node_id && !point.question_ids.length}
                      onClick={() => focusDirectory(id)}>
                      <span className="review-dot" aria-label={reviewStatusLabels[statuses[id]]} />
                      <span>{point.label}</span>
                    </button>
                  ))}
                </div>
              ))}
              {points.length === 0 && <p className="review-muted">暂无知识点目录</p>}
            </nav>}
          </aside>
          <main className="review-reading" data-view={position.view}>
            {artifact.coverage?.partial && <p className="review-muted">资料有缺页</p>}
            {position.view !== "quiz" && linkWarnings.length > 0 && (
              <details className="review-coverage">
                <summary>链接覆盖提醒</summary>
                <ul>{linkWarnings.map((warning) => <li key={warning}>{warning}</li>)}</ul>
              </details>
            )}
            {!available && (
              <div className="review-unavailable">
                <p>{artifact[`${position.view}_error_message`] ?? "这份成果尚未生成。"}</p>
                {actionError && <p role="alert" className="review-quiz-error">{actionError}</p>}
                <button type="button" className="review-primary"
                  disabled={retrying !== null || artifact[`${position.view}_status`] === "generating"}
                  onClick={() => void onGenerate(artifact, position.view)}>
                  {retrying === position.view ? "生成中…" : `生成${tabs.find(([view]) => view === position.view)?.[1]}`}
                </button>
              </div>
            )}
            {available && position.view === "notes" && (
              <div className="review-article">
                <h1>{artifact.title}</h1>
                <ReviewNotes blocks={blocks} index={index} quick={quick} highlighted={highlighted} go={go}
                  onPage={openOriginal} onAsk={renderAsk ? ask : undefined} annotation={annotation} />
              </div>
            )}
            {available && position.view === "graph" && (
              <ReviewGraph index={index} statuses={statuses} selected={position.kcId} onSelect={selectGraph}
                go={go} annotation={annotation} summary={summary} legacyTree={artifact.graph_tree} />
            )}
            {available && position.view === "quiz" && (
              <div className="review-article">
                <h2>章节测试 · {artifact.questions.length} 题</h2>
                {quizError && <p className="review-quiz-error" role="alert">{quizError}</p>}
                {points.map(([id]) => {
                  const questions = questionsByKc.get(id);
                  return questions?.length ? quizGroup(id, questions) : null;
                })}
                {unlinked.length > 0 && quizGroup(undefined, unlinked)}
              </div>
            )}
          </main>
          {askTarget && renderAsk ? renderAsk(askTarget, askTarget.view, closeAsk) : original && (
            <OriginalPage key={page} materialId={artifact.material_id} page={page} total={totalPages}
              onPage={setManualPage} close={closeOriginal} />
          )}
        </div>
      </div>
    </dialog>
  );
}

