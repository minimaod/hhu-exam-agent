import { useEffect, useState } from "react";
import { getArtifact, type ArtifactDetailView, type ArtifactSummary, type OutputKind } from "../lib/materialsApi";
import { outputAvailable, readReviewProgress, reviewKcIndex, subscribeReviewProgress, type ReviewPosition } from "../lib/reviewWorkbench";

const tabs = [["notes", "笔记"], ["graph", "图谱"], ["quiz", "测试"]] as const;

export default function ReviewResultCard({
  id, displayName, summary, updated, progressKey, onOpen,
}: {
  id: string;
  displayName: string;
  summary?: ArtifactSummary;
  updated?: ArtifactDetailView;
  progressKey: string;
  onOpen: (position?: ReviewPosition) => void;
}) {
  const [detail, setDetail] = useState<ArtifactDetailView>();
  const [progress, setProgress] = useState(() => readReviewProgress(progressKey));
  useEffect(() => {
    let active = true;
    void getArtifact(id).then((value) => {
      if (active) setDetail(value);
    }).catch(() => {
      // The summary still provides an entry. Opening it reports a read error.
    });
    return () => { active = false; };
  }, [id, summary?.updated_at]);
  useEffect(() => subscribeReviewProgress(() => setProgress(readReviewProgress(progressKey))), [progressKey]);
  const artifact = updated && (!detail || updated.updated_at >= detail.updated_at) ? updated : detail;
  const meta = artifact ?? summary;
  const index = artifact ? reviewKcIndex(artifact) : {};
  const points = Object.values(index);
  const read = progress.read.filter((kcId) => index[kcId]?.note_anchor).length;
  const right = artifact?.questions.filter((question) => question.attempt?.is_correct === true).length;
  const total = artifact?.questions.length ?? summary?.question_count;
  const last = progress.last && index[progress.last];
  const available = (view: OutputKind) => artifact ? outputAvailable(artifact, view) : summary?.[`${view}_status`] === "ready";

  return (
    <section className="review-result-card" id={"material-review-" + id} aria-label="复习成果">
      <div className="review-result-heading">
        <strong>{displayName}</strong>
        <button type="button" className="review-primary" onClick={() => onOpen()}>打开复习工作台</button>
      </div>
      <div className="review-result-meta">
        {meta && <span>第 {meta.start_page}–{meta.end_page} 页</span>}
        {artifact && <span>{points.length} 个知识点</span>}
        {total !== undefined && <span>{total} 题</span>}
        {artifact && <span>已读 {read}/{points.filter((point) => point.note_anchor).length} · 答对 {right}/{total}</span>}
        {meta?.coverage?.partial && <span>资料有缺页</span>}
      </div>
      <div className="review-result-links">
        {tabs.filter(([view]) => available(view)).map(([view, label]) => (
          <button key={view} type="button" className="review-button" onClick={() => onOpen({ view })}>{label}</button>
        ))}
        {last && progress.last && (
          <button type="button" className="review-link" onClick={() => onOpen({ view: "notes", kcId: progress.last })}>
            上次读到：{last.label}
          </button>
        )}
      </div>
    </section>
  );
}

