import { renderMathInElement } from "./MaterialNotesView";
import type { ReviewKnowledgePoint } from "../lib/reviewWorkbench";

type NoteBlock = {
  key: number;
  heading: string;
  level?: "h2" | "h3";
  kcId?: string;
  pages: number[];
  html: string;
  quickHtml: string;
};

export function readWorkbenchNotes(html: string, index: Record<string, ReviewKnowledgePoint>): NoteBlock[] {
  const doc = new DOMParser().parseFromString(html, "text/html");
  const headings = Array.from(doc.body.querySelectorAll("h2,h3"));
  const matched = new Set<string>();
  return [null, ...headings].map((heading, position) => {
    const range = doc.createRange();
    range.selectNodeContents(doc.body);
    if (heading) range.setStartAfter(heading);
    if (headings[position]) range.setEndBefore(headings[position]);
    const body = doc.createElement("div");
    body.append(range.cloneContents());
    const kc = heading?.getAttribute("data-kc") ?? "";
    const kcId = kc && index[kc]?.note_anchor === kc && !matched.has(kc) ? kc : undefined;
    if (kcId) matched.add(kcId);
    const refs = heading?.querySelector("[data-slides]")?.getAttribute("data-slides") ?? "";
    const pages = (refs.match(/\d+/g) ?? []).map(Number);
    const headingCopy = heading?.cloneNode(true) as HTMLElement | undefined;
    headingCopy?.querySelectorAll("[data-slides]").forEach((source) => source.remove());

    const quick = doc.createElement("div");
    body.querySelectorAll(".tldr, .formula, .key-formula, .math-display").forEach((node) => {
      if (node.parentElement?.closest(".tldr,.formula,.key-formula,.math-display")) return;
      quick.append(node.cloneNode(true));
    });
    const formulas = new Set<string>();
    const walker = doc.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    while (walker.nextNode()) {
      const node = walker.currentNode;
      if (node.parentElement?.closest(".tldr,.formula,.key-formula,.math-display,.checkpoint,details,pre,code")) continue;
      for (const match of (node.nodeValue ?? "").matchAll(/\$\$[\s\S]+?\$\$|\\\[[\s\S]+?\\\]|(?<!\$)\$[^\s$][^$\n]*\$(?!\$)|\\\([^\n]+?\\\)/g)) {
        if (formulas.has(match[0])) continue;
        formulas.add(match[0]);
        const equation = doc.createElement("p");
        equation.textContent = match[0];
        quick.append(equation);
      }
    }

    body.querySelectorAll("details[open]").forEach((node) => node.removeAttribute("open"));
    const fold = (element: Element, label: string) => {
      const details = doc.createElement("details");
      const summary = doc.createElement("summary");
      summary.textContent = label;
      element.replaceWith(details);
      details.append(summary, element);
    };
    for (const block of Array.from(body.children)) {
      if (block.matches("details") || block.querySelector("details") && !block.matches(".checkpoint")) continue;
      const content = block.textContent?.trim() ?? "";
      const label = /^(?:[【「\s]*)(推导|例题|典型例题)(?:[】」\d\s：:、]|$)/.exec(content)?.[1];
      if (block.matches(".checkpoint") || /^(?:随堂)?自测[：:]/.test(content)) fold(block, "随堂自测");
      else if (label) fold(block, label);
    }
    renderMathInElement(body);
    renderMathInElement(quick);
    const headingBody = doc.createElement("div");
    if (headingCopy) {
      headingBody.append(...headingCopy.childNodes);
      renderMathInElement(headingBody);
    }
    return {
      key: position, heading: headingBody.innerHTML, level: heading?.tagName.toLowerCase() as NoteBlock["level"],
      kcId, pages, html: body.innerHTML, quickHtml: quick.innerHTML,
    };
  }).filter((block) => block.heading || block.html.trim());
}

export function ReviewNotes({
  blocks, index, quick, highlighted, go, onPage, onAsk, annotation,
}: {
  blocks: NoteBlock[];
  index: Record<string, ReviewKnowledgePoint>;
  quick: boolean;
  highlighted?: string;
  go: (kcId: string, view: "notes" | "graph" | "quiz") => void;
  onPage: (page: number, kcId?: string) => void;
  onAsk?: (kcId: string) => void;
  annotation: (kcId: string) => React.ReactNode;
}) {
  const links = (ids: string[]) => ids.filter((id) => index[id]?.note_anchor).map((id) => (
    <button key={id} type="button" className="review-link" onClick={() => go(id, "notes")}>{index[id].label}</button>
  ));
  return (
    <div className="review-notes epa-notes-article">
      {blocks.map(({ key, heading, level, kcId, pages, html, quickHtml }) => {
        const point = kcId ? index[kcId] : null;
        const page = pages[0] ?? point?.pages[0];
        const prerequisites = links(point?.deps ?? []);
        const following = links(point?.dependents ?? []);
        return (
          <section key={key} data-review-section={kcId} className={kcId && highlighted === kcId ? "review-highlight" : undefined}>
            {level === "h2" && <h2 dangerouslySetInnerHTML={{ __html: heading }} />}
            {level === "h3" && (
              <div className="review-section-heading">
                <h3 dangerouslySetInnerHTML={{ __html: heading }} />
                {page && <span className="review-muted">P{page}</span>}
                <div className="review-section-tools">
                  {page && <button type="button" className="review-button" onClick={() => onPage(page, kcId)}>原页</button>}
                  {kcId && point?.graph_node_id && (
                    <button type="button" className="review-button" onClick={() => go(kcId, "graph")}>在图谱中定位</button>
                  )}
                  {kcId && onAsk && <button type="button" className="review-button" onClick={() => onAsk(kcId)}>问 AI</button>}
                </div>
              </div>
            )}
            {!quick && prerequisites.length > 0 && <div className="review-crosslinks">先掌握：{prerequisites}</div>}
            {quick && level === "h3" && !quickHtml
              ? <p className="review-muted">本节暂无单列的结论或公式。</p>
              : <div dangerouslySetInnerHTML={{ __html: quick ? quickHtml : html }} />}
            {!quick && kcId && point && (
              <>
                {annotation(kcId)}
                <div className="review-crosslinks">
                  {following.length > 0 && <span>学完可以接着学：{following}</span>}
                  {point.question_ids.length > 0 && (
                    <button type="button" className="review-link" onClick={() => go(kcId, "quiz")}>
                      做本节 {point.question_ids.length} 题
                    </button>
                  )}
                </div>
              </>
            )}
          </section>
        );
      })}
    </div>
  );
}

