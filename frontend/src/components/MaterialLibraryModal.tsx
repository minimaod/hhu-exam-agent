import { useEffect, useState } from "react";
import {
  fetchMaterialLibrary,
  getGuide,
  materialPreviewUrl,
  studyFromGuideItem,
  type MaterialLibraryItem,
  type OutputKind,
  type ReviewGuideView,
} from "../lib/materialsApi.ts";
import { formatFileSize } from "../lib/display.ts";

type MaterialLibraryModalProps = {
  conversationId: string;
  attachedMaterialIds: ReadonlySet<string>;
  onAttachMaterial: (materialId: string) => Promise<void>;
  onOpenArtifact: (
    artifactId: string,
    materialName: string,
    pageCount: number,
    initialTab?: OutputKind,
  ) => void;
  onStartGuideStudy: (
    conversationId: string,
    prompt: string,
    guideId: string,
    itemId: string,
    mode: "explain" | "exercise",
  ) => void;
  onClose: () => void;
};

export default function MaterialLibraryModal({
  conversationId,
  attachedMaterialIds,
  onAttachMaterial,
  onOpenArtifact,
  onStartGuideStudy,
  onClose,
}: MaterialLibraryModalProps) {
  const [items, setItems] = useState<MaterialLibraryItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [activeGuide, setActiveGuide] = useState<ReviewGuideView | null>(null);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    fetchMaterialLibrary()
      .then((list) => {
        if (active) setItems(list);
      })
      .catch((err) => {
        if (active) setError(err instanceof Error ? err.message : "无法读取资料与成果列表。");
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  return (
    <div
      className="material-modal-backdrop"
      role="dialog"
      aria-modal="true"
      aria-labelledby="material-library-heading"
    >
      <div className="material-modal-card max-h-[88vh] w-[min(860px,95vw)] overflow-y-auto">
        <header className="material-modal-header">
          <div>
            <p className="eyebrow">账号私有资料库</p>
            <h2 id="material-library-heading">资料与成果</h2>
          </div>
          <div className="material-modal-actions">
            <button type="button" className="quiet-button" onClick={onClose}>
              关闭
            </button>
          </div>
        </header>

        {loading && <p className="page-message">正在读取历史资料与成果…</p>}
        {error && <div role="alert" className="inline-alert">{error}</div>}

        {!loading && !error && items.length === 0 && (
          <p className="page-message">
            你还没有上传个人 PDF 资料。可以在聊天输入框左下角点击「＋」上传 PDF。
          </p>
        )}

        {!loading && items.length > 0 && (
          <div className="space-y-3">
            {items.map((entry) => {
              const mat = entry.material;
              const isAttached = attachedMaterialIds.has(mat.id);
              return (
                <article
                  key={mat.id}
                  className="rounded-xl border border-slate-200 bg-slate-50/70 p-3.5"
                >
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <h3 className="text-sm font-semibold text-slate-800">{mat.display_name}</h3>
                      <p className="mt-0.5 text-xs text-slate-600">
                        {mat.page_count} 页 · {formatFileSize(mat.size_bytes)}
                        {entry.readability ? ` · ${entry.readability.summary}` : ""}
                      </p>
                    </div>
                    <div className="flex flex-wrap items-center gap-2">
                      <a
                        href={materialPreviewUrl(mat.id)}
                        target="_blank"
                        rel="noreferrer"
                        className="quiet-button text-xs"
                      >
                        原件预览
                      </a>
                      <button
                        type="button"
                        className="primary-button text-xs"
                        disabled={isAttached || busyId === mat.id}
                        onClick={() => {
                          setBusyId(mat.id);
                          setError(null);
                          void onAttachMaterial(mat.id)
                            .then(() => onClose())
                            .catch((err) => {
                              setError(err instanceof Error ? err.message : "关联失败。");
                            })
                            .finally(() => setBusyId(null));
                        }}
                      >
                        {isAttached ? "已在当前会话" : busyId === mat.id ? "关联中…" : "引用到当前会话"}
                      </button>
                    </div>
                  </div>

                  {entry.artifacts.length > 0 && (
                    <div className="mt-2.5 flex flex-wrap gap-2">
                      {entry.artifacts.map((art) => (
                        <button
                          key={art.id}
                          type="button"
                          className="quiet-button text-xs"
                          onClick={() => {
                            onOpenArtifact(art.id, mat.display_name, mat.page_count);
                            onClose();
                          }}
                        >
                          打开第 {art.start_page}–{art.end_page} 页成果
                        </button>
                      ))}
                    </div>
                  )}

                  {entry.guides.length > 0 && (
                    <div className="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-600">
                      <span>历史清单：</span>
                      {entry.guides.map((g) => (
                        <button
                          key={g.id}
                          type="button"
                          className="quiet-button text-xs"
                          onClick={() => {
                            void getGuide(g.id)
                              .then((guide) => setActiveGuide(guide))
                              .catch((err) => setError(err instanceof Error ? err.message : "无法读取历史清单。"));
                          }}
                        >
                          第 {g.start_page}–{g.end_page} 页（{g.status === "ready" ? "可回看" : g.status}）
                        </button>
                      ))}
                    </div>
                  )}
                </article>
              );
            })}
          </div>
        )}

        {activeGuide && (
          <section className="mt-4 rounded-xl border border-slate-200 bg-white p-3.5">
            <div className="flex items-center justify-between">
              <h3 className="text-sm font-semibold text-slate-800">
                历史复习清单 · 第 {activeGuide.start_page}–{activeGuide.end_page} 页
              </h3>
              <button type="button" className="quiet-button text-xs" onClick={() => setActiveGuide(null)}>
                收起
              </button>
            </div>
            {activeGuide.status !== "ready" ? (
              <p className="mt-2 text-xs text-slate-600">该历史清单未完成生成。</p>
            ) : (
              <ul className="mt-2 space-y-2">
                {activeGuide.items.map((item) => (
                  <li key={item.id} className="rounded-lg border border-slate-200 p-2.5 text-xs">
                    <p className="font-semibold text-slate-800">{item.title}</p>
                    <p className="mt-1 text-slate-600">{item.ai_explanation}</p>
                    <div className="mt-2 flex gap-2">
                      <button
                        type="button"
                        className="quiet-button text-xs"
                        onClick={() => {
                          void studyFromGuideItem(activeGuide.id, item.id, "explain").then((res) => {
                            onStartGuideStudy(
                              res.conversation_id || conversationId,
                              res.prompt,
                              res.guide_id,
                              res.item_id,
                              "explain",
                            );
                            onClose();
                          });
                        }}
                      >
                        继续讲解
                      </button>
                      <button
                        type="button"
                        className="quiet-button text-xs"
                        onClick={() => {
                          void studyFromGuideItem(activeGuide.id, item.id, "exercise").then((res) => {
                            onStartGuideStudy(
                              res.conversation_id || conversationId,
                              res.prompt,
                              res.guide_id,
                              res.item_id,
                              "exercise",
                            );
                            onClose();
                          });
                        }}
                      >
                        做一道练习
                      </button>
                    </div>
                  </li>
                ))}
              </ul>
            )}
          </section>
        )}
      </div>
    </div>
  );
}
