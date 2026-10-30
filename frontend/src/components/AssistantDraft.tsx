/**
 * F008：正在流式生成的助手正文。
 *
 * 只展示模型实际产出的片段，不承担“业务完成”的含义：
 * - `interrupted` 为真时明确标注“已中断、未保存”，不显示成功样式；
 * - 收到最终 result 后由工作区清空本组件，改为渲染服务端保存的完整正文，
 *   因此不会出现“临时拼接 + 最终正文”重复显示。
 */
type AssistantDraftProps = {
  text: string;
  interrupted: boolean;
};

export default function AssistantDraft({ text, interrupted }: AssistantDraftProps) {
  if (!text && !interrupted) {
    return null;
  }

  return (
    <div
      data-testid="assistant-draft"
      aria-live="polite"
      aria-busy={!interrupted}
      className={`mt-2 rounded-2xl border p-4 shadow-card backdrop-blur ${
        interrupted
          ? "border-amber-300/80 bg-amber-50/90"
          : "border-slate-200/90 bg-white/90"
      }`}
    >
      <p className="mb-1 text-[11px] font-medium text-slate-500">
        {interrupted ? "回复已中断，未保存" : "正在生成回复…"}
      </p>
      <p className="whitespace-pre-wrap break-words text-sm leading-6 text-ink">
        {text}
        {!interrupted && (
          <span aria-hidden="true" className="ml-0.5 inline-block animate-pulse">
            ▍
          </span>
        )}
      </p>
      {interrupted && (
        <p className="mt-2 text-[11px] text-amber-900">
          这段文字没有保存；刷新会话记录会回到服务端的真实状态。
        </p>
      )}
    </div>
  );
}