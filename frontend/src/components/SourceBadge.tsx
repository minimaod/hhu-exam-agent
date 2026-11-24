import type { Source } from "../lib/conversationsApi";

export default function SourceBadge({
  source,
  isModelGenerated = false,
}: {
  source: Source;
  isModelGenerated?: boolean;
}) {
  if (isModelGenerated) {
    return <span className="source-note">模型生成 · 非真题</span>;
  }
  if (source.type === "none") {
    return (
      <span className="source-note">
        未关联来源
      </span>
    );
  }
  if (source.type === "student_text") {
    return (
      <span
        title={source.text ?? ""}
        className="source-note source-note--unverified"
      >
        学生自述来源（未核实）：{source.text}
      </span>
    );
  }
  return (
    <span
      title={source.paper_title ?? source.paper_id ?? ""}
      className="source-note text-river"
    >
      来源：{source.paper_title ?? source.paper_id}
    </span>
  );
}
