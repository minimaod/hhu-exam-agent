import katex from "katex";
import { useEffect, useId, useMemo, useRef, useState } from "react";
import {
  getMaterialPage,
  materialPageImageUrl,
  materialPreviewUrl,
  type MaterialPageView,
} from "../lib/materialsApi.ts";

const MATH_RE =
  /(?<!\\)\$\$([\s\S]+?)\$\$|(?<!\\)\\\[([\s\S]+?)\\\]|(?<!\\)\\\(([\s\S]+?)\\\)|(?<!\\|\$)\$(?!\$)([^\s$](?:(?:\\.|[^\\$\n])*?[^\s$])?)\$(?!\d|\$)/g;

type TocEntry = {
  id: string;
  level: 2 | 3;
  text: string;
  pageNo: number | null;
};

function parseFirstSlidePage(raw: string | null): number | null {
  if (!raw) return null;
  const m = raw.match(/\d+/);
  if (!m) return null;
  const n = Number.parseInt(m[0], 10);
  return Number.isFinite(n) && n >= 1 ? n : null;
}

export function renderMathInElement(root: HTMLElement): void {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  const textNodes: Text[] = [];
  let cur = walker.nextNode();
  while (cur) {
    const parentTag = cur.parentElement?.tagName.toLowerCase();
    if (parentTag !== "code" && parentTag !== "pre" && cur.nodeValue) {
      textNodes.push(cur as Text);
    }
    cur = walker.nextNode();
  }

  for (const node of textNodes) {
    const text = node.nodeValue ?? "";
    const matches = [...text.matchAll(MATH_RE)];
    if (matches.length === 0) continue;

    const frag = document.createDocumentFragment();
    let lastIndex = 0;
    for (const m of matches) {
      const idx = m.index ?? 0;
      if (idx > lastIndex) {
        frag.appendChild(document.createTextNode(text.slice(lastIndex, idx)));
      }
      const blockLatex = m[1] ?? m[2];
      const inlineLatex = m[3] ?? m[4];
      const latex = blockLatex ?? inlineLatex ?? "";
      const displayMode = blockLatex !== undefined;
      try {
        const rendered = katex.renderToString(latex, {
          displayMode,
          throwOnError: false,
          errorColor: "var(--katex-parse-error)",
          trust: false,
          output: "htmlAndMathml",
        });
        const span = document.createElement("span");
        if (displayMode) span.className = "math-display";
        span.innerHTML = rendered;
        frag.appendChild(span);
      } catch {
        frag.appendChild(document.createTextNode(m[0]));
      }
      lastIndex = idx + m[0].length;
    }
    if (lastIndex < text.length) {
      frag.appendChild(document.createTextNode(text.slice(lastIndex)));
    }
    node.replaceWith(frag);
  }
}

export default function MaterialNotesView({
  materialId,
  pageCount,
  notesHtml,
  partial,
  showPageRange = true,
  startPage,
  endPage,
  kcs,
  onStudyKc,
}: {
  materialId: string;
  pageCount: number;
  notesHtml: string;
  partial?: boolean;
  showPageRange?: boolean;
  startPage: number;
  endPage: number;
  kcs?: Array<{ id: string; label: string }>;
  onStudyKc?: (kcId: string, mode: "explain" | "exercise") => void;
}) {
  const [activePage, setActivePage] = useState<number>(startPage || 1);
  const [showToc, setShowToc] = useState(false);
  const [showSlideRail, setShowSlideRail] = useState(false);
  const [showExtractedText, setShowExtractedText] = useState(false);
  const [pageTextData, setPageTextData] = useState<MaterialPageView | null>(null);
  const [pageTextLoading, setPageTextLoading] = useState(false);
  const [imgError, setImgError] = useState(false);
  const notesId = useId();
  const notesBodyRef = useRef<HTMLDivElement | null>(null);

  const { processedHtml, toc } = useMemo(() => {
    if (!notesHtml.trim()) return { processedHtml: "", toc: [] as TocEntry[] };
    const doc = new DOMParser().parseFromString(notesHtml, "text/html");
    const headings = doc.body.querySelectorAll("h2, h3");
    const entries: TocEntry[] = [];
    headings.forEach((h, idx) => {
      const id = `${notesId}-sec-${idx + 1}`;
      h.setAttribute("id", id);
      const slideEl = h.querySelector("[data-slides]");
      const parsedPage = parseFirstSlidePage(slideEl?.getAttribute("data-slides") ?? null);
      const pageNo =
        parsedPage !== null && parsedPage >= startPage && parsedPage <= endPage
          ? parsedPage
          : null;
      if (slideEl) {
        if (pageNo) {
          const btn = doc.createElement("button");
          btn.type = "button";
          btn.className = "epa-slide-jump-btn";
          btn.setAttribute("data-jump-page", String(pageNo));
          btn.textContent = `原件第 ${pageNo} 页`;
          slideEl.replaceWith(btn);
        } else {
          slideEl.remove();
        }
      }
      const headingText = (h.textContent ?? "").replace(/原件第\s*\d+\s*页/g, "").trim();
      if (onStudyKc && h.tagName.toLowerCase() === "h3" && kcs?.length) {
        const matchedKc = kcs.find((k) => k.id === h.getAttribute("data-kc"));
        if (matchedKc) {
          const studyBtn = doc.createElement("button");
          studyBtn.type = "button";
          studyBtn.className = "epa-slide-jump-btn";
          studyBtn.setAttribute("data-study-kc", matchedKc.id);
          studyBtn.textContent = "去聊天学习";
          h.appendChild(studyBtn);
        }
      }
      entries.push({
        id,
        level: h.tagName.toLowerCase() === "h2" ? 2 : 3,
        text: headingText,
        pageNo,
      });
    });
    renderMathInElement(doc.body);
    return { processedHtml: doc.body.innerHTML, toc: entries };
  }, [notesHtml, notesId, startPage, endPage, kcs, onStudyKc]);

  useEffect(() => {
    setActivePage(startPage || 1);
  }, [materialId, startPage]);

  useEffect(() => {
    setImgError(false);
    setPageTextData(null);
    if (!showExtractedText) return;
    let cancelled = false;
    setPageTextLoading(true);
    void getMaterialPage(materialId, activePage)
      .then((data) => {
        if (!cancelled) setPageTextData(data);
      })
      .catch(() => {
        if (!cancelled) setPageTextData(null);
      })
      .finally(() => {
        if (!cancelled) setPageTextLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [materialId, activePage, showExtractedText]);

  const handleNotesClick = (e: React.MouseEvent<HTMLDivElement>) => {
    const target = e.target as HTMLElement | null;
    const studyBtn = target?.closest("[data-study-kc]") as HTMLElement | null;
    if (studyBtn && onStudyKc) {
      const kcId = studyBtn.getAttribute("data-study-kc");
      if (kcId) onStudyKc(kcId, "explain");
      return;
    }
    const btn = target?.closest("[data-jump-page]") as HTMLElement | null;
    if (!btn) return;
    const pageNo = Number.parseInt(btn.getAttribute("data-jump-page") ?? "", 10);
    if (Number.isFinite(pageNo) && pageNo >= 1 && pageNo <= pageCount) {
      setActivePage(pageNo);
      setShowSlideRail(true);
    }
  };

  const scrollToSection = (id: string, pageNo: number | null) => {
    const el = notesBodyRef.current?.querySelector(`#${CSS.escape(id)}`);
    if (el) {
      el.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    if (pageNo && pageNo >= 1 && pageNo <= pageCount) {
      setActivePage(pageNo);
    }
  };

  return (
    <div className={`epa-notes-shell${showSlideRail ? " has-source" : ""}${toc.length === 0 ? " no-toc" : ""}`}>
      {toc.length > 0 && (
        <aside className="epa-toc-pane" aria-label="章节目录">
          <div className="epa-pane-title">章节目录</div>
          <button
            type="button"
            className="epa-toc-toggle"
            aria-expanded={showToc}
            aria-controls={notesId + "-toc"}
            onClick={() => setShowToc((visible) => !visible)}
          >
            {showToc ? "收起章节目录" : "展开章节目录"}
          </button>
          <nav id={notesId + "-toc"} className="epa-toc-list">
            {toc.map((entry) => (
              <button
                key={entry.id}
                type="button"
                className={`epa-toc-item ${entry.level === 3 ? "is-sub" : ""}`}
                onClick={() => scrollToSection(entry.id, entry.pageNo)}
              >
                <span>{entry.text}</span>
                {entry.pageNo && <span className="epa-toc-page">P{entry.pageNo}</span>}
              </button>
            ))}
          </nav>
        </aside>
      )}

      <div className="epa-notes-main">
        <div className="epa-notes-toolbar">
          {showPageRange && <span className="muted-note">
            第 {startPage}–{endPage} 页{partial ? " · 资料有缺页" : ""}
          </span>}
          <button
            type="button"
            className="ghost-btn compact-btn"
            onClick={() => setShowSlideRail((v) => !v)}
          >
            {showSlideRail ? "隐藏原页对照" : "展开原页对照"}
          </button>
        </div>

        <div
          ref={notesBodyRef}
          className="epa-notes-article"
          onClick={handleNotesClick}
          dangerouslySetInnerHTML={{ __html: processedHtml }}
        />
      </div>

      {showSlideRail && (
        <aside className="epa-slide-rail" aria-label="原件页码对照">
          <div className="epa-slide-rail-head">
            <strong>原页对照 · 第 {activePage} / {pageCount} 页</strong>
            <div className="epa-slide-nav">
              <button
                type="button"
                className="ghost-btn compact-btn"
                disabled={activePage <= 1}
                onClick={() => setActivePage((p) => Math.max(1, p - 1))}
              >
                上一页
              </button>
              <button
                type="button"
                className="ghost-btn compact-btn"
                disabled={activePage >= pageCount}
                onClick={() => setActivePage((p) => Math.min(pageCount, p + 1))}
              >
                下一页
              </button>
            </div>
          </div>

          <div className="epa-slide-image-box">
            {imgError ? (
              <div className="empty-inline">
                原页图片加载失败，可点击下方链接打开原 PDF。
              </div>
            ) : (
              <img
                src={materialPageImageUrl(materialId, activePage)}
                alt={`资料第 ${activePage} 页原件`}
                className="epa-slide-img"
                onError={() => setImgError(true)}
              />
            )}
          </div>

          <div className="epa-slide-actions">
            <a
              href={materialPreviewUrl(materialId, activePage)}
              target="_blank"
              rel="noreferrer"
              className="ghost-btn compact-btn"
            >
              在新标签打开原 PDF 第 {activePage} 页
            </a>
            <button
              type="button"
              className="ghost-btn compact-btn"
              onClick={() => setShowExtractedText((v) => !v)}
            >
              {showExtractedText ? "收起提取文本" : "查看提取文本"}
            </button>
          </div>

          {showExtractedText && (
            <div className="epa-slide-text-box">
              {pageTextLoading ? (
                <p className="muted-note">正在读取第 {activePage} 页提取文本…</p>
              ) : pageTextData ? (
                <>
                  {pageTextData.warnings.length > 0 && (
                    <p className="muted-note">提示：{pageTextData.warnings.join("；")}</p>
                  )}
                  <pre className="material-page-pre">
                    {pageTextData.extracted_text || "（该页无可用文本层）"}
                  </pre>
                </>
              ) : (
                <p className="muted-note">未能读取提取文本。</p>
              )}
            </div>
          )}
        </aside>
      )}
    </div>
  );
}
