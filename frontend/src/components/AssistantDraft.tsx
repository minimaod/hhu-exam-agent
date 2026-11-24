import AssistantMarkdown from "./AssistantMarkdown";

export default function AssistantDraft({
  text,
  running = false,
  interrupted = false,
}: {
  text: string;
  running?: boolean;
  interrupted?: boolean;
}) {
  return (
    <div
      className="assistant-prose"
      aria-live={running ? "polite" : undefined}
      aria-busy={running || undefined}
      data-testid={running || interrupted ? "assistant-draft" : undefined}
    >
      <AssistantMarkdown text={text} />
      {interrupted && <p role="status">回复已中断，以上为尚未保存的片段。</p>}
    </div>
  );
}
