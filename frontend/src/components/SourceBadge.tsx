import type { Source } from "../lib/conversationsApi";

export default function SourceBadge({ source }: { source: Source }) {
  if (source.type === "none") {
    return (
      <span className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-0.5 text-[11px] text-slate-500">
        未关联来源
      </span>
    );
  }
  if (source.type === "student_text") {
    return (
      <span
        title={source.text ?? ""}
        className="max-w-full truncate rounded-full border border-amber-200 bg-amber-50 px-2.5 py-0.5 text-[11px] text-amber-800"
      >
        学生自述来源（未核实）：{source.text}
      </span>
    );
  }
  return (
    <span
      title={source.paper_title ?? source.paper_id ?? ""}
      className="max-w-full truncate rounded-full border border-river/30 bg-river/10 px-2.5 py-0.5 text-[11px] text-river"
    >
      已登记来源：{source.paper_title ?? source.paper_id}
    </span>
  );
}
