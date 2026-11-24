import { useCallback, useEffect, useRef, useState, type ChangeEvent } from "react";
import {
  checkMaterialReadability,
  deleteMaterial,
  evaluateQuizQuestion,
  generateOrGetArtifact,
  getArtifact,
  getDeleteImpact,
  getGuide,
  getMaterialDetail,
  getMaterialPage,
  listMaterials,
  materialPreviewUrl,
  revealQuizSolution,
  studyFromArtifactKc,
  studyFromArtifactQuestion,
  studyFromGuideItem,
  submitQuizAttempt,
  updateGraphNote,
  uploadMaterialPdf,
  type ArtifactDetailView,
  type ArtifactSummary,
  type DeleteImpactView,
  type GuideCategory,
  type GuideSummary,
  type MaterialPageView,
  type MaterialSummary,
  type ReadabilityReport,
  type ReviewGuideView,
} from "../lib/materialsApi.ts";
import { formatFileSize } from "../lib/display.ts";
import MaterialGraphView from "./MaterialGraphView.tsx";
import MaterialNotesView from "./MaterialNotesView.tsx";
import MaterialPageModal from "./MaterialPageModal.tsx";
import MaterialQuizView from "./MaterialQuizView.tsx";

const CATEGORY_LABELS: Record<GuideCategory, string> = {
  concept: "核心概念",
  formula: "公式与适用条件",
  method: "典型方法",
  pitfall: "易错点",
};

const OUTPUTS = [
  { id: "notes", label: "章节笔记", description: "知识点、公式和例题" },
  { id: "quiz", label: "章节测试", description: "作答、评分和解析" },
  { id: "graph", label: "知识图谱", description: "知识点之间的关系" },
] as const;
type ArtifactTab = (typeof OUTPUTS)[number]["id"];
const MAX_GENERATION_PAGES = 40;

const ARTIFACT_STATE = { ready: "已完成", generating: "生成中", failed: "未完成" } as const;
const STEP_LABELS: Record<string, string> = {
  skeleton: "整理知识点", notes: "编写章节笔记", quiz: "生成章节测试", review: "核对复习内容",
};

export type MaterialStudyTarget = {
  guideId?: string;
  itemId?: string;
  artifactId?: string;
  questionId?: string;
  kcId?: string;
  mode: "explain" | "exercise" | "evaluate";
};

type MaterialWorkspaceProps = {
  onStartStudy: (
    conversationId: string,
    prompt: string,
    target: MaterialStudyTarget,
  ) => void;
};

export default function MaterialWorkspace({ onStartStudy }: MaterialWorkspaceProps) {
  const [materials, setMaterials] = useState<MaterialSummary[]>([]);
  const [listLoading, setListLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);

  const [uploading, setUploading] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);

  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [loadedMaterial, setLoadedMaterial] = useState<MaterialSummary | null>(null);
  const selectedMaterial = loadedMaterial?.id === selectedId ? loadedMaterial : null;
  const [guides, setGuides] = useState<GuideSummary[]>([]);

  const [startPage, setStartPage] = useState(1);
  const [endPage, setEndPage] = useState(1);
  const [readability, setReadability] = useState<ReadabilityReport | null>(null);
  const [checkingReadability, setCheckingReadability] = useState(false);

  const [activeGuide, setActiveGuide] = useState<ReviewGuideView | null>(null);
  const [artifacts, setArtifacts] = useState<ArtifactSummary[]>([]);
  const [activeArtifact, setActiveArtifact] = useState<ArtifactDetailView | null>(null);
  const [artifactTab, setArtifactTab] = useState<ArtifactTab>("notes");
  const [readerOpen, setReaderOpen] = useState(false);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [openingArtifactId, setOpeningArtifactId] = useState<string | null>(null);
  const [generatingRange, setGeneratingRange] = useState<{ start: number; end: number } | null>(null);
  const [quizBusyId, setQuizBusyId] = useState<string | null>(null);
  const [studyingItemId, setStudyingItemId] = useState<string | null>(null);

  const [modalPage, setModalPage] = useState<{
    materialId: string;
    pageNo: number;
    quote: string | null;
    loading: boolean;
    error: string | null;
    data: MaterialPageView | null;
  } | null>(null);

  const [deleteImpact, setDeleteImpact] = useState<DeleteImpactView | null>(null);
  const [deleting, setDeleting] = useState(false);

  const selectedIdRef = useRef<string | null>(selectedId);
  selectedIdRef.current = selectedId;
  const activeArtifactRef = useRef<ArtifactDetailView | null>(activeArtifact);
  activeArtifactRef.current = activeArtifact;
  const selectionSeqRef = useRef(0);
  const guideSeqRef = useRef(0);
  const artifactSeqRef = useRef(0);
  const citationSeqRef = useRef(0);
  const studySeqRef = useRef(0);
  const deleteImpactSeqRef = useRef(0);
  const readerHeadingRef = useRef<HTMLHeadingElement | null>(null);

  const isCurrentSelection = useCallback((materialId: string, selectionSeq: number) => (
    selectedIdRef.current === materialId && selectionSeqRef.current === selectionSeq
  ), []);

  const refreshList = useCallback(async (keepSelected?: string | null) => {
    setListLoading(true);
    setListError(null);
    try {
      const items = await listMaterials();
      setMaterials(items);
      const nextId =
        keepSelected && items.some((m) => m.id === keepSelected)
          ? keepSelected
          : items[0]?.id ?? null;
      setSelectedId(nextId);
    } catch (err) {
      setListError(err instanceof Error ? err.message : "无法读取个人资料列表。");
    } finally {
      setListLoading(false);
    }
  }, []);

  useEffect(() => {
    void refreshList();
  }, [refreshList]);

  const loadDetail = useCallback(async (materialId: string, selectionSeq: number) => {
    const guideSeq = ++guideSeqRef.current;
    try {
      const detail = await getMaterialDetail(materialId);
      if (!isCurrentSelection(materialId, selectionSeq)) return;
      setLoadedMaterial(detail.material);
      setGuides(detail.guides);
      setArtifacts(detail.artifacts ?? []);
      setStartPage(1);
      setEndPage(Math.min(detail.material.page_count, MAX_GENERATION_PAGES));
      if (detail.guides.length > 0) {
        const firstGuide = await getGuide(detail.guides[0].id);
        if (!isCurrentSelection(materialId, selectionSeq) || guideSeqRef.current !== guideSeq) {
          return;
        }
        setActiveGuide(firstGuide);
      }
    } catch (err) {
      if (!isCurrentSelection(materialId, selectionSeq) || guideSeqRef.current !== guideSeq) {
        return;
      }
      setActionError(err instanceof Error ? err.message : "无法读取资料详情。");
    }
  }, [isCurrentSelection]);

  useEffect(() => {
    const selectionSeq = ++selectionSeqRef.current;
    artifactSeqRef.current += 1;
    setLoadedMaterial(null);
    setGuides([]);
    setActiveGuide(null);
    setArtifacts([]);
    setActiveArtifact(null);
    setArtifactTab("notes");
    setReaderOpen(false);
    setHistoryOpen(false);
    setOpeningArtifactId(null);
    setGeneratingRange(null);
    setQuizBusyId(null);
    setReadability(null);
    setActionError(null);
    setCheckingReadability(false);
    setModalPage(null);
    setDeleteImpact(null);
    setStudyingItemId(null);
    if (selectedId) {
      void loadDetail(selectedId, selectionSeq);
    }
    return () => {
      selectionSeqRef.current += 1;
    };
  }, [selectedId, loadDetail]);

  useEffect(() => {
    if (readerOpen) {
      readerHeadingRef.current?.focus();
    }
  }, [readerOpen]);

  async function handleFileChange(event: ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setUploading(true);
    setActionError(null);
    try {
      const created = await uploadMaterialPdf(file);
      await refreshList(created.id);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "PDF 上传失败。");
    } finally {
      setUploading(false);
    }
  }

  async function handleCheckReadability() {
    if (!selectedMaterial) return;
    const targetMaterialId = selectedMaterial.id;
    const selectionSeq = selectionSeqRef.current;
    setCheckingReadability(true);
    setActionError(null);
    try {
      const report = await checkMaterialReadability(targetMaterialId, startPage, endPage);
      if (!isCurrentSelection(targetMaterialId, selectionSeq)) return;
      setReadability(report);
    } catch (err) {
      if (!isCurrentSelection(targetMaterialId, selectionSeq)) return;
      setActionError(err instanceof Error ? err.message : "文字识别检查失败。");
    } finally {
      if (isCurrentSelection(targetMaterialId, selectionSeq)) {
        setCheckingReadability(false);
      }
    }
  }

  function showArtifact(artifact: ArtifactDetailView, resetTab: boolean) {
    setActiveArtifact(artifact);
    setReaderOpen(true);
    if (resetTab) setArtifactTab("notes");
    setArtifacts((previous) => [
      { ...artifact, question_count: artifact.questions.length },
      ...previous.filter((item) => item.id !== artifact.id),
    ]);
  }

  async function handleOpenArtifact(artifactId: string) {
    if (!selectedMaterial || openingArtifactId || generatingRange) return;
    const mid = selectedMaterial.id;
    const seq = selectionSeqRef.current;
    const artSeq = ++artifactSeqRef.current;
    const isCurrent = () => isCurrentSelection(mid, seq) && artifactSeqRef.current === artSeq;
    setOpeningArtifactId(artifactId);
    setActionError(null);
    try {
      const artifact = await getArtifact(artifactId);
      if (isCurrent() && artifact.material_id === mid) {
        showArtifact(artifact, activeArtifactRef.current?.id !== artifactId);
      }
    } catch (err) {
      if (isCurrent()) setActionError(err instanceof Error ? err.message : "无法打开复习资料。");
    } finally {
      if (isCurrent()) setOpeningArtifactId(null);
    }
  }

  async function handleGenerateArtifact(previous?: ArtifactDetailView) {
    if (!selectedMaterial || openingArtifactId || generatingRange) return;
    const mid = selectedMaterial.id;
    const seq = selectionSeqRef.current;
    const artSeq = ++artifactSeqRef.current;
    const isCurrent = () => isCurrentSelection(mid, seq) && artifactSeqRef.current === artSeq;
    const start = previous?.start_page ?? startPage;
    const end = previous?.end_page ?? endPage;
    if (!Number.isInteger(start) || !Number.isInteger(end) ||
        start < 1 || end < start || end > selectedMaterial.page_count || end - start + 1 > MAX_GENERATION_PAGES) {
      setActionError(`请在 PDF 总页数内选择有效页段，每次最多生成 ${MAX_GENERATION_PAGES} 页。`);
      return;
    }
    setGeneratingRange({ start, end });
    setActionError(null);
    try {
      const artifact = await generateOrGetArtifact(mid, start, end, previous?.course_hint);
      if (isCurrent() && artifact.material_id === mid) showArtifact(artifact, !previous);
    } catch (err) {
      if (isCurrent()) setActionError(err instanceof Error ? err.message : "生成请求未完成，请重试。");
    } finally {
      if (isCurrent()) setGeneratingRange(null);
    }
  }

  function handleReturnToMaterial() {
    artifactSeqRef.current += 1;
    setReaderOpen(false);
    setActiveArtifact(null);
    setQuizBusyId(null);
    setActionError(null);
  }

  async function handleOpenGuide(guideId: string) {
    if (!selectedMaterial) return;
    const targetMaterialId = selectedMaterial.id;
    const selectionSeq = selectionSeqRef.current;
    const guideSeq = ++guideSeqRef.current;
    studySeqRef.current += 1;
    setStudyingItemId(null);
    setActiveGuide(null);
    setActionError(null);
    try {
      const guide = await getGuide(guideId);
      if (
        !isCurrentSelection(targetMaterialId, selectionSeq) ||
        guideSeqRef.current !== guideSeq ||
        guide.material_id !== targetMaterialId
      ) {
        return;
      }
      setActiveGuide(guide);
    } catch (err) {
      if (!isCurrentSelection(targetMaterialId, selectionSeq) || guideSeqRef.current !== guideSeq) {
        return;
      }
      setActionError(err instanceof Error ? err.message : "读取复习清单失败。");
    }
  }

  async function handleOpenCitation(
    materialId: string,
    pageNo: number,
    quote: string | null,
  ) {
    const selectionSeq = selectionSeqRef.current;
    const seq = ++citationSeqRef.current;
    setModalPage({ materialId, pageNo, quote, loading: true, error: null, data: null });
    try {
      const data = await getMaterialPage(materialId, pageNo);
      if (citationSeqRef.current !== seq || !isCurrentSelection(materialId, selectionSeq)) {
        return;
      }
      setModalPage({ materialId, pageNo, quote, loading: false, error: null, data });
    } catch (err) {
      if (citationSeqRef.current !== seq || !isCurrentSelection(materialId, selectionSeq)) {
        return;
      }
      setModalPage({
        materialId,
        pageNo,
        quote,
        loading: false,
        error: err instanceof Error ? err.message : "读取原文页失败。",
        data: null,
      });
    }
  }

  function handleCloseCitation() {
    citationSeqRef.current += 1;
    setModalPage(null);
  }

  async function handleStudyItem(itemId: string, mode: "explain" | "exercise") {
    if (!activeGuide) return;
    const targetMaterialId = activeGuide.material_id;
    const selectionSeq = selectionSeqRef.current;
    const seq = ++studySeqRef.current;
    setStudyingItemId(itemId);
    setActionError(null);
    try {
      const res = await studyFromGuideItem(activeGuide.id, itemId, mode);
      if (studySeqRef.current !== seq || !isCurrentSelection(targetMaterialId, selectionSeq)) {
        return;
      }
      onStartStudy(res.conversation_id, res.prompt, {
        guideId: res.guide_id,
        itemId: res.item_id,
        mode: res.mode,
      });
    } catch (err) {
      if (studySeqRef.current !== seq || !isCurrentSelection(targetMaterialId, selectionSeq)) {
        return;
      }
      setActionError(err instanceof Error ? err.message : "无法进入学习会话。");
    } finally {
      if (studySeqRef.current === seq && isCurrentSelection(targetMaterialId, selectionSeq)) {
        setStudyingItemId(null);
      }
    }
  }

  async function handleRequestDelete() {
    if (!selectedMaterial) return;
    const targetMaterialId = selectedMaterial.id;
    const selectionSeq = selectionSeqRef.current;
    const seq = ++deleteImpactSeqRef.current;
    setActionError(null);
    try {
      const impact = await getDeleteImpact(targetMaterialId);
      if (
        deleteImpactSeqRef.current !== seq ||
        !isCurrentSelection(targetMaterialId, selectionSeq)
      ) {
        return;
      }
      setDeleteImpact(impact);
    } catch (err) {
      if (
        deleteImpactSeqRef.current !== seq ||
        !isCurrentSelection(targetMaterialId, selectionSeq)
      ) {
        return;
      }
      setActionError(err instanceof Error ? err.message : "无法获取删除影响范围。");
    }
  }

  async function handleConfirmDelete() {
    if (!deleteImpact) return;
    setDeleting(true);
    setActionError(null);
    try {
      await deleteMaterial(deleteImpact.material_id);
      setDeleteImpact(null);
      setActiveGuide(null);
      await refreshList(null);
    } catch (err) {
      setActionError(err instanceof Error ? err.message : "删除资料失败。");
    } finally {
      setDeleting(false);
    }
  }

  const readingArtifact = readerOpen && activeArtifact?.material_id === selectedMaterial?.id;
  const hasNotes = Boolean(activeArtifact?.notes_html.trim());
  const hasGraph = Boolean(activeArtifact?.graph_tree?.nodes.length);
  const artifactReady = activeArtifact?.status === "ready";
  const hasQuiz = artifactReady && Boolean(activeArtifact?.questions.length);
  const availablePreviews = [hasNotes ? "章节笔记" : null, hasGraph ? "知识图谱" : null]
    .filter(Boolean).join("和");

  return (
    <div className={`page-frame material-workspace${readingArtifact ? " material-workspace--reader" : ""}`}>
      {!readingArtifact && <header className="page-heading material-header">
        <div>
          <h1>个人资料</h1>
          <p>从课程 PDF 生成章节笔记、章节测试和知识图谱。</p>
        </div>
        <label className="primary-button material-upload-btn">
          {uploading ? "正在上传解析…" : "上传课程 PDF"}
          <input
            type="file"
            accept="application/pdf,.pdf"
            disabled={uploading}
            onChange={(e) => void handleFileChange(e)}
            className="sr-only"
          />
        </label>
      </header>}

      {actionError && <div className="inline-alert">{actionError}</div>}
      {listError && <div className="inline-alert">{listError}</div>}

      <div className={readingArtifact ? "material-reader-layout" : "material-grid"}>
        {!readingArtifact && <aside className="material-list-pane" aria-label="我的资料">
          <h2>已上传资料（{materials.length}）</h2>
          {listLoading ? (
            <p className="page-message">正在加载个人资料…</p>
          ) : materials.length === 0 ? (
            <p className="page-message">尚未上传个人 PDF，请点击右上角上传。</p>
          ) : (
            <ul className="material-list">
              {materials.map((item) => (
                <li key={item.id}>
                  <button
                    type="button"
                    className={`material-list-item ${selectedId === item.id ? "is-active" : ""}`}
                    onClick={() => setSelectedId(item.id)}
                  >
                    <span className="material-item-name">{item.display_name}</span>
                    <span className="material-item-meta">
                      共 {item.page_count} 页 · {formatFileSize(item.size_bytes)}
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </aside>}

        <section className={readingArtifact ? "material-reader-pane" : "material-detail-pane"}>
          {!selectedMaterial ? (
            <div className="material-empty-state">
              <p>选择左侧一份资料，或上传新的课程 PDF 开始章节复习。</p>
            </div>
          ) : (
            <>
              {!readingArtifact && <>
              <div className="material-detail-bar">
                <div>
                  <h2>{selectedMaterial.display_name}</h2>
                  <p className="material-item-meta">共 {selectedMaterial.page_count} 页</p>
                </div>
                <div className="material-detail-actions">
                  <a
                    className="quiet-button"
                    href={materialPreviewUrl(selectedMaterial.id)}
                    target="_blank"
                    rel="noopener noreferrer"
                  >
                    预览原件 PDF
                  </a>
                  <button
                    type="button"
                    className="quiet-button material-danger-btn"
                    onClick={() => void handleRequestDelete()}
                  >
                    删除资料
                  </button>
                </div>
              </div>

              <section className="material-generate-section" aria-labelledby="material-generate-title">
                <h3 id="material-generate-title">选择复习范围</h3>
                <ul className="material-output-list">
                  {OUTPUTS.map((item) => <li key={item.id}><strong>{item.label}</strong><span>{item.description}</span></li>)}
                </ul>
                <div className="material-range-card">
                <div className="material-range-inputs">
                  <label>
                    起始页
                    <input
                      type="number"
                      min={1}
                      max={selectedMaterial.page_count}
                      value={startPage}
                      onChange={(e) => setStartPage(Number(e.target.value) || 1)}
                    />
                  </label>
                  <label>
                    结束页
                    <input
                      type="number"
                      min={1}
                      max={selectedMaterial.page_count}
                      value={endPage}
                      onChange={(e) => setEndPage(Number(e.target.value) || 1)}
                    />
                  </label>
                </div>
                <div className="material-range-actions">
                  <button
                    type="button"
                    className="quiet-button"
                    disabled={checkingReadability}
                    onClick={() => void handleCheckReadability()}
                  >
                    {checkingReadability ? "检查中…" : "检查文字识别"}
                  </button>
                  <button
                    type="button"
                    className="primary-button"
                    disabled={Boolean(generatingRange || openingArtifactId)}
                    onClick={() => void handleGenerateArtifact()}
                  >
                    {generatingRange ? "正在生成…" : "生成复习资料"}
                  </button>
                </div>
                </div>
                <p className="material-limits-note">单次最多选择 {MAX_GENERATION_PAGES} 页。原件和复习成果仅自己可见。</p>
                {generatingRange && (
                  <p className="material-generation-progress" role="status">
                    正在生成第 {generatingRange.start}–{generatingRange.end} 页的笔记、测试和图谱…
                  </p>
                )}
              </section>

              <section className="material-results" aria-labelledby="material-results-title">
                <h3 id="material-results-title">复习成果</h3>
                {artifacts.length === 0 ? <p className="page-message">这份资料还没有复习成果。</p> : (
                  <ul className="material-result-list">
                    {artifacts.map((artifact) => (
                      <li key={artifact.id}>
                        <div>
                          <strong>{artifact.title}</strong>
                          <p>第 {artifact.start_page}–{artifact.end_page} 页{artifact.is_sample ? " · 导入样例" : ""}</p>
                        </div>
                        <div className="material-result-actions">
                          <span className="material-result-state" data-state={artifact.status}>{ARTIFACT_STATE[artifact.status]}</span>
                          <button type="button" className="quiet-button" disabled={Boolean(openingArtifactId || generatingRange)} onClick={() => void handleOpenArtifact(artifact.id)}>
                            {openingArtifactId === artifact.id ? "正在打开…" : artifact.status === "ready" ? "打开复习资料" : "查看生成结果"}
                          </button>
                        </div>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
              </>}

              {readingArtifact && activeArtifact && (
                <section className="epa-artifact-section" aria-label="复习资料阅读区">
                  <button type="button" className="quiet-button material-reader-back" disabled={Boolean(generatingRange || openingArtifactId)} onClick={handleReturnToMaterial}>
                    ← 返回资料
                  </button>
                  <header className="material-reader-heading">
                    <div>
                      <p>{selectedMaterial.display_name} · 第 {activeArtifact.start_page}–{activeArtifact.end_page} 页</p>
                      <h1 ref={readerHeadingRef} tabIndex={-1}>{activeArtifact.title}</h1>
                    </div>
                    <div className="material-result-actions">
                      {activeArtifact.is_sample && <span className="epa-sample-badge">导入样例</span>}
                      <span className="material-result-state" data-state={generatingRange ? "generating" : activeArtifact.status}>
                        {generatingRange ? "重试中" : ARTIFACT_STATE[activeArtifact.status]}
                      </span>
                    </div>
                  </header>

                  {!artifactReady && (
                    <div className="material-reader-status" role="status">
                      <div>
                        <strong>{generatingRange || activeArtifact.status === "generating" ? "正在生成复习资料" : activeArtifact.current_step === "quiz" ? "章节测试生成失败" : `${STEP_LABELS[activeArtifact.current_step] ?? "生成复习资料"}未完成`}</strong>
                        <p>{availablePreviews ? `${availablePreviews}可预览。整套复习资料尚未完成。` : "复习内容完成后会显示在这里。"}</p>
                      </div>
                      {activeArtifact.status === "failed" && (
                        <button type="button" className="primary-button" disabled={Boolean(generatingRange)} onClick={() => void handleGenerateArtifact(activeArtifact)}>
                          {generatingRange ? "正在重试…" : activeArtifact.current_step === "quiz" ? "重试章节测试" : "继续生成"}
                        </button>
                      )}
                      {activeArtifact.status === "generating" && !generatingRange && (
                        <button type="button" className="quiet-button" disabled={Boolean(openingArtifactId)} onClick={() => void handleOpenArtifact(activeArtifact.id)}>
                          查看最新进度
                        </button>
                      )}
                    </div>
                  )}

                  <div className="epa-tabs" role="tablist" aria-label="复习成果">
                    {OUTPUTS.map(({ id, label }) => {
                      const available = id === "notes" ? hasNotes : id === "quiz" ? hasQuiz : hasGraph;
                      const detail = available
                        ? !artifactReady ? "草稿" : id === "quiz" ? `${activeArtifact.questions.length} 道题` : ""
                        : id === "quiz" && activeArtifact.status === "failed" && activeArtifact.current_step === "quiz" ? "生成失败" : "未就绪";
                      return (
                        <button key={id} type="button" role="tab" aria-selected={artifactTab === id}
                          aria-controls="material-output-panel" id={`material-tab-${id}`}
                          className={`epa-tab-btn ${artifactTab === id ? "is-active" : ""}`} onClick={() => setArtifactTab(id)}>
                          <strong>{label}</strong>{detail && <span>{detail}</span>}
                        </button>
                      );
                    })}
                  </div>
                  {activeArtifact.coverage_warnings.length > 0 && (
                    <details className="material-coverage-details">
                      <summary>查看资料核对提示</summary>
                      <ul>{activeArtifact.coverage_warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
                    </details>
                  )}
                  <div id="material-output-panel" role="tabpanel" aria-labelledby={`material-tab-${artifactTab}`}>
                  {artifactTab === "notes" && !hasNotes && <p className="material-output-empty">章节笔记尚未生成。</p>}
                  {artifactTab === "quiz" && !hasQuiz && <p className="material-output-empty">章节测试就绪后可在这里作答和查看解析。</p>}
                  {artifactTab === "graph" && !hasGraph && <p className="material-output-empty">知识图谱尚未生成。</p>}
                  {artifactTab === "notes" && hasNotes && (
                    <MaterialNotesView
                      materialId={selectedMaterial.id}
                      pageCount={selectedMaterial.page_count}
                      notesHtml={activeArtifact.notes_html}
                      startPage={activeArtifact.start_page}
                      endPage={activeArtifact.end_page}
                      kcs={
                        ((activeArtifact.skeleton?.chapters as Array<{ kcs?: Array<{ id: string; label: string }> }> | undefined) ?? [])
                          .flatMap((ch) => ch.kcs ?? [])
                      }
                      onStudyKc={artifactReady ? (kcId, mode) => {
                        const targetMid = selectedMaterial.id;
                        const targetArtId = activeArtifact.id;
                        const seq = selectionSeqRef.current;
                        const artSeq = artifactSeqRef.current;
                        void studyFromArtifactKc(targetArtId, kcId, mode).then((res) => {
                          if (!isCurrentSelection(targetMid, seq) || artifactSeqRef.current !== artSeq) {
                            return;
                          }
                          onStartStudy(res.conversation_id, res.prompt, {
                            artifactId: res.artifact_id,
                            kcId: res.kc_id,
                            mode: res.mode,
                          });
                        });
                      } : undefined}
                    />
                  )}

                  {artifactTab === "quiz" && hasQuiz && (
                    <MaterialQuizView
                      questions={activeArtifact.questions}
                      submittingId={quizBusyId}
                      onSubmitAttempt={async (questionId, userAnswer) => {
                        const targetMid = selectedMaterial.id;
                        const targetArtId = activeArtifact.id;
                        const seq = selectionSeqRef.current;
                        const artSeq = artifactSeqRef.current;
                        setQuizBusyId(questionId);
                        try {
                          const updated = await submitQuizAttempt(
                            targetArtId,
                            questionId,
                            userAnswer,
                          );
                          if (
                            !isCurrentSelection(targetMid, seq) ||
                            artifactSeqRef.current !== artSeq ||
                            activeArtifactRef.current?.id !== targetArtId ||
                            updated.material_id !== targetMid
                          ) {
                            return;
                          }
                          setActiveArtifact(updated);
                        } finally {
                          if (
                            isCurrentSelection(targetMid, seq) &&
                            artifactSeqRef.current === artSeq
                          ) {
                            setQuizBusyId(null);
                          }
                        }
                      }}
                      onRevealSolution={async (questionId) => {
                        const targetMid = selectedMaterial.id;
                        const targetArtId = activeArtifact.id;
                        const seq = selectionSeqRef.current;
                        const artSeq = artifactSeqRef.current;
                        setQuizBusyId(questionId);
                        try {
                          const updated = await revealQuizSolution(
                            targetArtId,
                            questionId,
                          );
                          if (
                            !isCurrentSelection(targetMid, seq) ||
                            artifactSeqRef.current !== artSeq ||
                            activeArtifactRef.current?.id !== targetArtId ||
                            updated.material_id !== targetMid
                          ) {
                            return;
                          }
                          setActiveArtifact(updated);
                        } finally {
                          if (
                            isCurrentSelection(targetMid, seq) &&
                            artifactSeqRef.current === artSeq
                          ) {
                            setQuizBusyId(null);
                          }
                        }
                      }}
                      onEvaluateQuestion={async (questionId) => {
                        const targetMid = selectedMaterial.id;
                        const targetArtId = activeArtifact.id;
                        const seq = selectionSeqRef.current;
                        const artSeq = artifactSeqRef.current;
                        setQuizBusyId(questionId);
                        try {
                          const updated = await evaluateQuizQuestion(
                            targetArtId,
                            questionId,
                          );
                          if (
                            !isCurrentSelection(targetMid, seq) ||
                            artifactSeqRef.current !== artSeq ||
                            activeArtifactRef.current?.id !== targetArtId ||
                            updated.material_id !== targetMid
                          ) {
                            return;
                          }
                          setActiveArtifact(updated);
                        } finally {
                          if (
                            isCurrentSelection(targetMid, seq) &&
                            artifactSeqRef.current === artSeq
                          ) {
                            setQuizBusyId(null);
                          }
                        }
                      }}
                      onOpenPage={(pageNo) =>
                        void handleOpenCitation(selectedMaterial.id, pageNo, "")
                      }
                      onStudyQuestion={(questionId, mode) => {
                        const targetMid = selectedMaterial.id;
                        const targetArtId = activeArtifact.id;
                        const seq = selectionSeqRef.current;
                        const artSeq = artifactSeqRef.current;
                        void studyFromArtifactQuestion(targetArtId, questionId, mode)
                          .then((res) => {
                            if (
                              !isCurrentSelection(targetMid, seq) ||
                              artifactSeqRef.current !== artSeq
                            ) {
                              return;
                            }
                            onStartStudy(res.conversation_id, res.prompt, {
                              artifactId: res.artifact_id,
                              questionId: res.question_id,
                              mode: res.mode,
                            });
                          })
                          .catch((err) => {
                            if (isCurrentSelection(targetMid, seq)) {
                              setActionError(
                                err instanceof Error ? err.message : "无法跳转到学习会话。",
                              );
                            }
                          });
                      }}
                    />
                  )}

                  {artifactTab === "graph" && hasGraph && (
                    <MaterialGraphView
                      graphTree={activeArtifact.graph_tree}
                      graphNotes={activeArtifact.graph_notes}
                      onStudyKc={artifactReady ? (kcId, mode) => {
                        const targetMid = selectedMaterial.id;
                        const targetArtId = activeArtifact.id;
                        const seq = selectionSeqRef.current;
                        const artSeq = artifactSeqRef.current;
                        void studyFromArtifactKc(targetArtId, kcId, mode).then((res) => {
                          if (!isCurrentSelection(targetMid, seq) || artifactSeqRef.current !== artSeq) {
                            return;
                          }
                          onStartStudy(res.conversation_id, res.prompt, {
                            artifactId: res.artifact_id,
                            kcId: res.kc_id,
                            mode: res.mode,
                          });
                        });
                      } : undefined}
                      onSaveNote={artifactReady ? async (nodeId, noteText) => {
                        const targetMid = selectedMaterial.id;
                        const targetArtId = activeArtifact.id;
                        const seq = selectionSeqRef.current;
                        const artSeq = artifactSeqRef.current;
                        const saved = await updateGraphNote(
                          targetArtId,
                          nodeId,
                          noteText,
                        );
                        if (
                          !isCurrentSelection(targetMid, seq) ||
                          artifactSeqRef.current !== artSeq ||
                          activeArtifactRef.current?.id !== targetArtId
                        ) {
                          return;
                        }
                        setActiveArtifact((prev) =>
                          prev && prev.id === targetArtId
                            ? {
                                ...prev,
                                graph_notes: {
                                  ...prev.graph_notes,
                                  [saved.node_id]: saved.note_text,
                                },
                              }
                            : prev,
                        );
                      } : undefined}
                    />
                  )}
                  </div>
                </section>
              )}

              {!readingArtifact && readability && (
                <div
                  className={`material-readability-box ${
                    readability.sufficient ? "is-ok" : "is-insufficient"
                  }`}
                >
                  <p>
                    <strong>文字识别报告（第 {readability.start_page}–{readability.end_page} 页）：</strong>
                    {readability.reason}
                  </p>
                  {readability.recommendation && <p>{readability.recommendation}</p>}
                  {readability.affected_pages.length > 0 && (
                    <ul className="material-affected-pages">
                      {readability.affected_pages.map((p) => (
                        <li key={p.page_no}>
                          第 {p.page_no} 页（{p.char_count} 字）：{p.warnings.join("；")}
                        </li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              {!readingArtifact && guides.length > 0 && (
                <section className="material-history" aria-label="历史清单">
                  <button type="button" className="quiet-button" aria-expanded={historyOpen}
                    onClick={() => {
                      if (historyOpen) studySeqRef.current += 1;
                      setHistoryOpen(!historyOpen);
                    }}>
                    {historyOpen ? "收起历史清单" : "历史清单"}（{guides.length}）
                  </button>
                </section>
              )}
              {!readingArtifact && historyOpen && guides.length > 0 && (
                <div className="material-guide-tabs" aria-label="已保存清单">
                  {guides.map((g) => (
                    <button
                      key={g.id}
                      type="button"
                      className={`quiet-button ${activeGuide?.id === g.id ? "is-active" : ""}`}
                      onClick={() => void handleOpenGuide(g.id)}
                    >
                      第 {g.start_page}–{g.end_page} 页
                      {g.status === "generating" ? "（生成中）" : g.status === "failed" ? "（失败）" : ""}
                    </button>
                  ))}
                </div>
              )}

              {!readingArtifact && historyOpen && activeGuide && (
                <div className="material-guide-panel">
                  {activeGuide.status === "failed" && (
                    <div className="inline-alert">
                      这份历史清单未生成完成。可使用上方的「生成复习资料」创建新成果。
                    </div>
                  )}
                  {activeGuide.status === "generating" && (
                    <p className="page-message">清单正在生成中，请稍候刷新查看…</p>
                  )}
                  {activeGuide.status === "ready" && (
                    <div className="material-guide-items">
                      {activeGuide.items.map((item) => (
                        <article key={item.id} className="material-guide-card">
                          <header className="material-guide-card-head">
                            <span className="material-category-tag">
                              {CATEGORY_LABELS[item.category]}
                            </span>
                            <h3>{item.title}</h3>
                          </header>

                          <div className="material-block">
                            <span className="material-badge material-badge-source">资料原文</span>
                            {item.source_excerpt ? (
                              <p>{item.source_excerpt}</p>
                            ) : (
                              <p className="material-no-citation">
                                暂无通过校验的资料原文，请点击物理页核对原件。
                              </p>
                            )}
                          </div>

                          <div className="material-block">
                            <span className="material-badge material-badge-ai">AI 解释</span>
                            <p>{item.ai_explanation}</p>
                          </div>

                          <div className="material-block">
                            <span className="material-badge material-badge-practice">模型生成练习方向</span>
                            <p>{item.practice_prompt}</p>
                          </div>

                          <footer className="material-guide-card-foot">
                            <div className="material-citations">
                              {item.verified_citations.length > 0 ? (
                                item.verified_citations.map((cit, idx) => (
                                  <button
                                    key={`${cit.page_no}-${idx}`}
                                    type="button"
                                    className="material-citation-pill"
                                    onClick={() =>
                                      void handleOpenCitation(
                                        activeGuide.material_id,
                                        cit.page_no,
                                        cit.quote,
                                      )
                                    }
                                  >
                                    第 {cit.page_no} 页原文
                                  </button>
                                ))
                              ) : (
                                <span className="material-no-citation">暂无通过校验的页码引文</span>
                              )}
                            </div>

                            <div className="material-study-actions">
                              <button
                                type="button"
                                className="quiet-button"
                                disabled={studyingItemId === item.id}
                                onClick={() => void handleStudyItem(item.id, "explain")}
                              >
                                继续讲解
                              </button>
                              <button
                                type="button"
                                className="primary-button"
                                disabled={studyingItemId === item.id}
                                onClick={() => void handleStudyItem(item.id, "exercise")}
                              >
                                做一道练习
                              </button>
                            </div>
                          </footer>
                        </article>
                      ))}
                    </div>
                  )}
                </div>
              )}
            </>
          )}
        </section>
      </div>

      {modalPage && (
        <MaterialPageModal
          materialId={modalPage.materialId}
          pageNo={modalPage.pageNo}
          highlightQuote={modalPage.quote}
          loading={modalPage.loading}
          error={modalPage.error}
          pageData={modalPage.data}
          onClose={handleCloseCitation}
        />
      )}

      {deleteImpact && (
        <div className="material-modal-backdrop" role="dialog" aria-modal="true">
          <div className="material-modal-card material-delete-dialog">
            <h2>确认删除个人资料？</h2>
            <p>
              即将永久删除《{deleteImpact.display_name}》，并一并回收以下关联数据（删除后不可恢复）：
            </p>
            <ul className="material-impact-list">
              <li>原 PDF 文件与提取文本缓存</li>
              <li>已生成复习清单（旧版）：{deleteImpact.guide_count} 份</li>
              <li>已生成完整复习产物：{deleteImpact.artifact_count ?? 0} 份（含作答 {deleteImpact.quiz_attempt_count ?? 0} 条、图谱笔记 {deleteImpact.graph_note_count ?? 0} 条）</li>
              <li>关联专用学习会话：{deleteImpact.conversation_id ? "1 个" : "0 个"}</li>
              <li>该会话下的练习记录：{deleteImpact.exercise_count} 道</li>
              <li>该会话下的错题记录：{deleteImpact.mistake_count} 条</li>
              <li>该会话下的图片附件：{deleteImpact.attachment_count} 张</li>
            </ul>
            <div className="material-modal-actions">
              <button
                type="button"
                className="quiet-button"
                disabled={deleting}
                onClick={() => setDeleteImpact(null)}
              >
                取消
              </button>
              <button
                type="button"
                className="primary-button material-danger-btn"
                disabled={deleting}
                onClick={() => void handleConfirmDelete()}
              >
                {deleting ? "正在删除…" : "确认永久删除"}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
