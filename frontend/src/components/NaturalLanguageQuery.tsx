import type { FormEvent } from "react";

const EXAMPLES = [
  "我要复习高数",
  "找高数期末试卷",
  "我要复习",
  "找大学物理试卷",
];

type NaturalLanguageQueryProps = {
  value: string;
  running: boolean;
  onChange: (value: string) => void;
  onSubmit: () => void;
  onCancel: () => void;
};

/**
 * 自然语言入口：只维护输入并把动作上报，请求和状态由 App 统一管理。
 */
export default function NaturalLanguageQuery({
  value,
  running,
  onChange,
  onSubmit,
  onCancel,
}: NaturalLanguageQueryProps) {
  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!value.trim() || running) {
      return;
    }
    onSubmit();
  }

  return (
    <div className="rounded-2xl border border-river/20 bg-white/95 p-5 shadow-card backdrop-blur">
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h2 className="text-base font-semibold text-ink">用一句话找试卷</h2>
        <span className="text-[11px] text-slate-500">例如“我要复习高数”</span>
      </div>
      <p className="mt-1 text-xs leading-5 text-slate-600">
        系统只理解课程和期中/期末条件，再从本地真实目录查找；
        试卷信息与下载入口都来自已登记资料，不会生成虚构试卷。
      </p>

      <form onSubmit={handleSubmit} className="mt-3">
        <label htmlFor="natural-query" className="sr-only">
          复习需求
        </label>
        <textarea
          id="natural-query"
          value={value}
          onChange={(event) => onChange(event.target.value)}
          rows={2}
          maxLength={1000}
          placeholder="例如：我要复习高数"
          className="w-full resize-y rounded-xl border border-slate-300 bg-white px-3.5 py-3 text-sm leading-6 text-ink outline-none transition placeholder:text-slate-400 focus:border-river focus:ring-2 focus:ring-river/20"
        />

        <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
          <span className="text-[11px] text-slate-400">{value.length}/1000</span>
          <div className="flex items-center gap-2">
            {running && (
              <button
                type="button"
                onClick={onCancel}
                className="inline-flex items-center rounded-xl border border-slate-300 bg-white px-4 py-2 text-sm font-medium text-slate-700 transition hover:bg-slate-100 focus:outline-none focus-visible:ring-2 focus-visible:ring-river focus-visible:ring-offset-2"
              >
                停止
              </button>
            )}
            <button
              type="submit"
              disabled={running || !value.trim()}
              className="inline-flex items-center rounded-xl bg-river px-5 py-2 text-sm font-medium text-white transition hover:bg-river/90 focus:outline-none focus-visible:ring-2 focus-visible:ring-river focus-visible:ring-offset-2 disabled:cursor-not-allowed disabled:bg-slate-400"
            >
              {running ? "查询中…" : "查找试卷"}
            </button>
          </div>
        </div>
      </form>

      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
        <span>试试：</span>
        {EXAMPLES.map((example) => (
          <button
            key={example}
            type="button"
            onClick={() => onChange(example)}
            disabled={running}
            className="rounded-full border border-slate-200 bg-slate-50 px-2.5 py-1 text-slate-600 transition hover:border-river/40 hover:text-river focus:outline-none focus-visible:ring-2 focus-visible:ring-river disabled:cursor-not-allowed disabled:opacity-60"
          >
            {example}
          </button>
        ))}
      </div>
    </div>
  );
}
