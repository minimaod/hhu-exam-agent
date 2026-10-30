import { useId, useState } from "react";
import type { QuestionCard, QuestionCodeBlock } from "../lib/conversationsApi";

const TYPE_LABELS: Record<QuestionCard["question_type"], string> = {
  choice: "选择题",
  fill: "填空题",
  code: "编程题",
};

const STATE_LABELS: Record<QuestionCard["answer"]["state"], string> = {
  original: "原卷答案",
  missing: "原卷未提供答案",
  pending_review: "答案待核对",
  conflict: "答案存在冲突",
};

const KNOWLEDGE_STATUS_LABELS: Record<string, string> = {
  candidate: "候选",
  verified: "已确认",
  rejected: "已拒绝",
};

export type QuestionAction = "select" | "explain" | "view" | "exercise" | "record" | "answer";

type QuestionCardProps = {
  card: QuestionCard;
  busy?: boolean;
  compact?: boolean;
  onAction?: (action: QuestionAction, card: QuestionCard, answer?: string) => void;
};

function sourceTitle(card: QuestionCard): string {
  return card.source.paper_title ?? card.source.paper_id;
}

function isCodePlaceholder(text: string | null | undefined): boolean {
  const value = (text ?? "").trim();
  return !value || /^\[?参考代码\]?$/.test(value) || value === "见代码块";
}

function pageLabel(pages: number[] | undefined): string | null {
  return pages?.length ? pages.map((page) => `第 ${page} 页`).join("、") : null;
}

function AssetImage({
  card,
  asset,
}: {
  card: QuestionCard;
  asset: QuestionCard["assets"][number];
}) {
  const [failed, setFailed] = useState(false);
  if (failed) {
    return (
      <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
        题图加载失败，可打开原卷
        {asset.page != null ? `第 ${asset.page} 页` : ""}核对。
      </p>
    );
  }
  return (
    <img
      src={asset.url}
      alt={asset.description ?? `第 ${card.number_label} 题题面图`}
      className="max-h-80 max-w-full rounded-lg border border-slate-200 object-contain"
      loading="lazy"
      onError={() => setFailed(true)}
    />
  );
}

function CodeBlocks({
  blocks,
  annotations,
}: {
  blocks: QuestionCodeBlock[];
  annotations: QuestionCard["code_annotations"];
}) {
  const groups = new Map<string, QuestionCodeBlock[]>();
  blocks.forEach((block, index) => {
    const key = block.method_label
      || (block.method != null ? `方法 ${block.method}` : "参考代码");
    const entry = groups.get(key) ?? [];
    entry.push({ ...block, block_id: block.block_id ?? `block-${index}` });
    groups.set(key, entry);
  });
  return (
    <div className="mt-2 space-y-3">
      {Array.from(groups.entries()).map(([label, items]) => (
        <section key={label} className="space-y-2">
          <h5 className="text-[11px] font-medium text-emerald-800">{label}</h5>
          {items.map((block) => (
            <div key={block.block_id ?? block.text.slice(0, 40)}>
              <div className="mb-1 flex flex-wrap gap-2 text-[11px] text-emerald-700">
                {block.name ? <span>{block.name}</span> : null}
                {block.kind ? <span>类型：{block.kind}</span> : null}
                {block.column != null ? <span>第 {block.column} 列</span> : null}
                {block.pages?.length || block.page != null ? (
                  <span>{pageLabel(block.pages) ?? `第 ${block.page} 页`}</span>
                ) : null}
                {block.depends_on?.length ? (
                  <span>依赖：{block.depends_on.join("、")}</span>
                ) : null}
              </div>
              <pre className="overflow-x-auto rounded-lg bg-slate-900 px-3 py-2 text-xs leading-6 text-slate-100">
                {block.text}
              </pre>
            </div>
          ))}
        </section>
      ))}
      {annotations?.length ? (
        <ul className="space-y-1 text-[11px] leading-5 text-emerald-800">
          {annotations.map((item, index) => (
            <li key={`${item.role ?? "annotation"}-${index}`}>
              {item.role ? `${item.role}：` : ""}
              {item.text}
              {item.page != null ? `（第 ${item.page} 页）` : ""}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}

export function QuestionCardView({ card, busy, compact, onAction }: QuestionCardProps) {
  const groupId = useId();
  const identity = `${card.question_id}:${card.build_id}:${card.content_sha256}`;
  const [choiceDraft, setChoiceDraft] = useState({ identity, answer: "" });
  const picked = choiceDraft.identity === identity ? choiceDraft.answer : "";
  const canAnswer = card.question_type === "choice" && Boolean(onAction) && !card.review.stale;
  const statementAssets = card.assets.filter(
    (asset) => asset.page == null || card.pages.statement.includes(asset.page),
  );
  const revealed = Boolean(!compact && card.answer_revealed && card.answer_detail);
  const answerText = card.answer_detail?.text ?? "";
  const blocks = card.code_blocks ?? [];
  const examples = card.code_examples ?? [];
  const solutions = card.independent_solutions ?? [];
  return (
    <article id={`question-${identity}`} className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <header className="mb-2 flex flex-wrap items-center gap-2">
        <span className="rounded-full border border-river/30 bg-river/10 px-2 py-0.5 text-[11px] text-river">
          第 {card.number_label} 题 · {TYPE_LABELS[card.question_type]}
        </span>
        <span className="text-[11px] text-slate-500">
          {sourceTitle(card)}
          {card.source.academic_year ? ` · ${card.source.academic_year}` : ""}
        </span>
        {card.source.download_url ? (
          <a
            className="text-[11px] text-river underline-offset-2 hover:underline"
            href={card.source_url && card.pages.statement.length
              ? `${card.source_url}&kind=statement&page=${card.pages.statement[0]}#page=${card.pages.statement[0]}`
              : card.source.download_url}
            target="_blank"
            rel="noreferrer"
          >
            查看原卷
          </a>
        ) : null}
        {pageLabel(card.pages.statement) ? (
          <span className="text-[11px] text-slate-400">
            题面{pageLabel(card.pages.statement)}
          </span>
        ) : null}
        {pageLabel(card.pages.answer) ? (
          <span className="text-[11px] text-slate-400">
            答案{pageLabel(card.pages.answer)}
          </span>
        ) : null}
      </header>

      {card.source_url ? (
        <nav className="mb-2 flex flex-wrap gap-3 text-xs text-river" aria-label="原始来源页">
          {card.pages.statement.length ? (
            <a href={`${card.source_url}&kind=statement&page=${card.pages.statement[0]}&download=true`}
              className="underline underline-offset-2">下载原卷</a>
          ) : null}
          {(["statement", "answer"] as const).flatMap((kind) =>
            card.pages[kind].map((page) => (
              <a key={`${kind}-${page}`} target="_blank" rel="noreferrer"
                className="underline underline-offset-2"
                href={`${card.source_url}&kind=${kind}&page=${page}#page=${page}`}>
                查看{kind === "statement" ? "题面" : "答案"}原页 · 第 {page} 页
              </a>
            )),
          )}
        </nav>
      ) : null}
      {card.source_views?.length ? (
        <p className="mb-2 text-[11px] text-slate-500">
          已查看原卷来源：{card.source_views.map((view) =>
            `${view.kind === "statement" ? "题面" : "答案"}第 ${view.page} 页（${new Date(view.viewed_at).toLocaleString("zh-CN")}）`,
          ).join("；")}
        </p>
      ) : null}

      {card.source.may_show_answer ? (
        <p className="mb-2 rounded-lg border border-amber-200 bg-amber-50 px-2 py-1.5 text-[11px] text-amber-800">
          原卷可能包含答案页；打开原页会记录本题已查看含答案原卷。建议先自己作答。
        </p>
      ) : null}

      {card.knowledge_points?.length ? (
        <ul className="mb-2 flex flex-wrap gap-1.5">
          {card.knowledge_points.map((point) => (
            <li
              key={point.slug}
              className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5 text-[11px] text-slate-600"
            >
              {point.name}
              {point.status && point.status !== "approved" ? `（${KNOWLEDGE_STATUS_LABELS[point.status] ?? point.status}）` : ""}
            </li>
          ))}
        </ul>
      ) : null}

      <p className="whitespace-pre-wrap text-sm leading-7 text-slate-800">{card.statement}</p>

      {card.formulas.length ? (
        <ul className="mt-2 space-y-1 text-sm text-slate-700">
          {card.formulas.map((formula, index) => (
            <li key={`${formula.text}-${index}`} className="rounded bg-slate-50 px-2 py-1 font-mono text-xs">
              {formula.text}
            </li>
          ))}
        </ul>
      ) : null}

      {card.options.length ? (
        <ul className="mt-3 space-y-1.5">
          {card.options.map((option) => (
            <li key={option.label} className="text-sm text-slate-700">
              <label className={`flex gap-2 rounded-lg px-2 py-1.5 ${canAnswer ? "cursor-pointer hover:bg-slate-50" : ""} ${picked === option.label ? "bg-river/10 ring-1 ring-river/40" : ""}`}>
                {canAnswer ? (
                  <input type="radio" name={groupId} value={option.label}
                    aria-label={`${option.label}. ${option.text}`}
                    checked={picked === option.label} disabled={busy}
                    className="mt-1 accent-river"
                    onChange={() => setChoiceDraft({ identity, answer: option.label })} />
                ) : null}
                <span className="font-medium text-slate-500">{option.label}.</span>
                <span className="whitespace-pre-wrap">{option.text}</span>
              </label>
            </li>
          ))}
        </ul>
      ) : null}

      {canAnswer ? (
        <div className="mt-3 flex items-center gap-3">
          <button type="button" disabled={busy || !picked}
            className="rounded-lg bg-river px-3 py-1.5 text-xs text-white disabled:opacity-50"
            onClick={() => { if (picked) onAction?.("answer", card, picked); }}>
            提交并讲解
          </button>
          <span className="text-xs text-slate-500">{picked ? `已选 ${picked}` : "请选择一个选项"}</span>
        </div>
      ) : null}

      {statementAssets.length ? (
        <div className="mt-3 space-y-2">
          {statementAssets.map((asset) => (
            <AssetImage key={asset.asset_id} card={card} asset={asset} />
          ))}
        </div>
      ) : null}

      {card.code_lines.length ? (
        <pre className="mt-3 overflow-x-auto rounded-lg bg-slate-900 px-3 py-2 text-xs leading-6 text-slate-100">
          {card.code_lines.join("\n")}
        </pre>
      ) : null}

      <div className="mt-3 flex flex-wrap items-center gap-2 text-[11px] text-slate-500">
        <span className="rounded-full border border-slate-200 bg-slate-50 px-2 py-0.5">
          {STATE_LABELS[card.answer.state]}
        </span>
        {card.review.stale ? (
          <span className="rounded-full border border-amber-200 bg-amber-50 px-2 py-0.5 text-amber-700">
            题目内容已变化
          </span>
        ) : null}
      </div>

      {revealed ? (
        <details data-question-answer className="mt-3 rounded-lg border border-emerald-200 bg-emerald-50 p-3">
          <summary className="cursor-pointer text-xs font-medium text-emerald-800">参考答案（已查看，点击展开）</summary>
          <div className="flex flex-wrap items-center justify-between gap-2">
            {card.answer_revealed ? (
              <span className="text-[11px] text-emerald-700">本题答案已在本会话开放
              {card.answer_revealed_at
                ? ` · ${new Date(card.answer_revealed_at).toLocaleString("zh-CN")}`
                : ""}</span>
            ) : null}
          </div>
          {!isCodePlaceholder(answerText) ? (
            <p className="mt-1 whitespace-pre-wrap text-sm leading-6 text-emerald-900">
              {answerText}
            </p>
          ) : null}
          {card.reference_code?.trim() && !blocks.length ? (
            <pre className="mt-2 overflow-x-auto rounded-lg bg-slate-900 px-3 py-2 text-xs leading-6 text-slate-100">
              {card.reference_code}
            </pre>
          ) : null}
          {blocks.length ? <CodeBlocks blocks={blocks} annotations={card.code_annotations} /> : null}
          {blocks.length && card.reference_code?.trim() ? (
            <details className="mt-2 text-xs text-emerald-800">
              <summary className="cursor-pointer">完整参考代码</summary>
              <pre className="mt-2 overflow-x-auto rounded-lg bg-slate-900 px-3 py-2 text-xs leading-6 text-slate-100">
                {card.reference_code}
              </pre>
            </details>
          ) : null}
          {examples.length ? (
            <div className="mt-2 space-y-2">
              <h5 className="text-[11px] font-medium text-emerald-800">示例</h5>
              {examples.map((example, index) => (
                <pre
                  key={`${example.slice(0, 30)}-${index}`}
                  className="overflow-x-auto rounded-lg bg-slate-900 px-3 py-2 text-xs leading-6 text-slate-100"
                >
                  {example}
                </pre>
              ))}
            </div>
          ) : null}
          {solutions.length ? (
            <div className="mt-2 space-y-2">
              <h5 className="text-[11px] font-medium text-emerald-800">其他解法</h5>
              {solutions.map((solution, index) => (
                <div key={`${solution.author ?? "solution"}-${index}`} className="text-xs text-emerald-900">
                  {solution.author ? <p className="font-medium">{solution.author}</p> : null}
                  <p className="whitespace-pre-wrap leading-6">{solution.text}</p>
                  {solution.note ? <p className="text-emerald-700">{solution.note}</p> : null}
                </div>
              ))}
            </div>
          ) : null}
          {card.answer_detail?.note ? (
            <p className="mt-2 text-[11px] text-emerald-700">{card.answer_detail.note}</p>
          ) : null}
        </details>
      ) : card.answer.state === "missing" ? (
        <p className="mt-3 rounded-lg bg-slate-50 px-3 py-2 text-xs text-slate-500">
          这份原卷没有提供参考答案，可以让我先讲解，再一起核对。
        </p>
      ) : null}

      {!compact && onAction ? (
        <div className="mt-3 flex flex-wrap gap-2">
          {!card.selected ? (
            <button
              type="button"
              className="rounded-lg bg-river px-3 py-1.5 text-xs text-white disabled:opacity-50"
              disabled={busy}
              onClick={() => onAction("select", card)}
            >
              选这道题
            </button>
          ) : null}
          <button
            type="button"
            className="rounded-lg border border-river/40 px-3 py-1.5 text-xs text-river disabled:opacity-50"
            disabled={busy}
            onClick={() => onAction("explain", card)}
          >
            讲解这道题
          </button>
          {card.answer.available && !revealed ? (
            <button
              type="button"
              className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-700 disabled:opacity-50"
              disabled={busy}
              onClick={() => onAction("view", card)}
            >
              查看参考答案
            </button>
          ) : null}
          <button
            type="button"
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-700 disabled:opacity-50"
            disabled={busy}
            onClick={() => onAction("exercise", card)}
          >
            生成一道变式
          </button>
          <button
            type="button"
            className="rounded-lg border border-slate-300 px-3 py-1.5 text-xs text-slate-700 disabled:opacity-50"
            disabled={busy}
            onClick={() => onAction("record", card)}
          >
            保存到错题本
          </button>
        </div>
      ) : null}
    </article>
  );
}

export function QuestionResultList({
  cards,
  status,
  message,
  busy,
  onAction,
}: {
  cards: QuestionCard[];
  status?: string | null;
  message?: string | null;
  busy?: boolean;
  onAction?: (action: QuestionAction, card: QuestionCard, answer?: string) => void;
}) {
  if (!cards.length) {
    return message ? (
      <p className="rounded-lg border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-800">
        {message}
      </p>
    ) : null;
  }
  return (
    <section className="space-y-2">
      <h4 className="text-xs font-medium text-slate-500">
        找到 {cards.length} 道已审核真题{status === "ok" ? "" : `（${message ?? "部分结果"}）`}
      </h4>
      {cards.map((card, index) => (
        <div key={`${card.question_id}:${card.build_id}:${card.content_sha256}`} className="flex items-start gap-2">
          <span className="mt-2 w-10 shrink-0 text-center text-xs text-slate-400">
            第 {index + 1} 道
          </span>
          <div className="min-w-0 flex-1">
            <QuestionCardView card={card} compact={!card.selected} busy={busy} onAction={onAction} />
            {!card.selected ? <div className="mt-1 flex flex-wrap gap-2">
              <button
                type="button"
                className="rounded-lg bg-river px-3 py-1.5 text-xs text-white disabled:opacity-50"
                disabled={busy}
                onClick={() => onAction?.("select", card)}
              >
                选第 {index + 1} 道题
              </button>
              <button
                type="button"
                className="rounded-lg border border-river/40 px-3 py-1.5 text-xs text-river disabled:opacity-50"
                disabled={busy}
                onClick={() => onAction?.("explain", card)}
              >
                直接讲解
              </button>
            </div> : null}
          </div>
        </div>
      ))}
    </section>
  );
}

export default QuestionCardView;
