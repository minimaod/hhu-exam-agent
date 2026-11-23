import { useEffect, useRef, useState } from "react";
import type { ConversationTurn } from "../lib/conversationsApi";
import AssistantDraft from "./AssistantDraft";
import MathText from "./MathText";

export default function ReviewWorkbenchAsk({
  label, pages, question, turns, busy, running, blocked, error, onClose, onSend, onStop,
}: {
  label: string;
  pages: number[];
  question?: string;
  turns: ConversationTurn[];
  busy: boolean;
  running: boolean;
  blocked: boolean;
  error?: string | null;
  onClose: () => void;
  onSend: (text: string) => void;
  onStop: () => void;
}) {
  const [draft, setDraft] = useState("");
  const lastTurn = turns[turns.length - 1];
  useEffect(() => {
    if (lastTurn?.status === "completed" && lastTurn.assistant_message) {
      setDraft((current) => current.trim() === lastTurn.query ? "" : current);
    }
  }, [lastTurn?.assistant_message, lastTurn?.query, lastTurn?.status]);
  const input = useRef<HTMLTextAreaElement>(null);
  const drawer = useRef<HTMLElement>(null);
  const [top, setTop] = useState(0);
  useEffect(() => {
    const panel = drawer.current;
    const pane = panel?.closest(".review-workbench-scroll");
    input.current?.focus({ preventScroll: true });
    if (window.matchMedia?.("(max-width: 720px)").matches) panel?.scrollIntoView({ block: "start" });
    if (!panel || !pane || typeof ResizeObserver === "undefined") return;
    const update = () => setTop(Math.min(0, pane.clientHeight - panel.offsetHeight));
    const observer = new ResizeObserver(update);
    observer.observe(panel);
    observer.observe(pane);
    update();
    return () => observer.disconnect();
  }, []);
  return (
    <aside ref={drawer} className="review-side-pane review-ask" aria-label="问 AI 抽屉" style={{ top }}>
      <div className="review-result-heading">
        <strong>问 AI</strong>
        <button type="button" className="review-button" onClick={onClose}>关闭问 AI</button>
      </div>
      <p className="review-muted">{label}{pages.length ? ` · 原页 ${pages.join("、")}` : ""}</p>
      {question && <p className="review-muted"><MathText text={question} /></p>}
      <p className="review-muted">问答会保存到当前会话。</p>
      {turns.map((turn) => (
        <div className="review-ask-message" key={turn.request_id}>
          <p><strong>你</strong> <MathText text={turn.query} /></p>
          {turn.assistant_message && <AssistantDraft text={turn.assistant_message}
            running={turn.status === "running"} interrupted={turn.status === "interrupted"} />}
          {!turn.assistant_message && turn.status === "running" && <p role="status">正在回复…</p>}
          {turn.error?.message && <p role="alert">{turn.error.message}</p>}
        </div>
      ))}
      {error && <p role="alert">{error}</p>}
      <form onSubmit={(event) => {
        event.preventDefault();
        if (!draft.trim() || busy || blocked) return;
        onSend(draft.trim());
      }}>
        <label>
          <span className="review-muted">你的问题</span>
          <textarea ref={input} rows={3} maxLength={2000} disabled={running} value={draft}
            onChange={(event) => setDraft(event.target.value)} />
        </label>
        <div className="review-result-links">
          <button type="submit" className="review-primary" disabled={busy || blocked || !draft.trim()}>发送</button>
          {running && <button type="button" className="review-button" onClick={onStop}>停止回复</button>}
          {blocked && <span className="review-muted">当前模型额度不可用</span>}
        </div>
      </form>
    </aside>
  );
}

