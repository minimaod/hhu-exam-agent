/**
 * 展示层纯函数：内部动作名转中文、重复文案判定、时间兜底和建议按钮分类。
 *
 * 这里不读取会话数据，也不发请求；页面组件只调用这些函数决定“怎么显示”。
 * 去重只影响渲染，不修改、不删除任何已保存的历史。
 */

/** 内部动作名 → 学生能看懂的中文。未知动作不原样展示。 */
const ACTION_LABELS: Record<string, string> = {
  introduce: "介绍可用能力",
  query_papers: "查找试卷",
  explain_problem: "讲解题目",
  followup_problem: "回答追问",
  generate_exercise: "生成练习",
  evaluate_exercise: "评价作答",
  reveal_solution: "查看参考解答",
  record_mistake: "保存到错题本",
  record_feedback: "记录理解情况",
  clarify_or_reply: "说明与确认",
  search_questions: "查找真题",
  list_paper_questions: "浏览真题",
  select_question: "选择真题",
  view_question_answer: "查看真题答案",
  explain_question: "讲解真题",
  review_memory: "查看学习记忆",
  correct_memory: "纠正学习记忆",
  delete_memory: "移除学习记忆",
};

export type ActionStatus = "success" | "failed" | "clarification";

const ACTION_STATUS_LABELS: Record<ActionStatus, string> = {
  success: "已完成",
  failed: "未完成",
  clarification: "待补充",
};

export function actionLabel(action: string): string {
  return ACTION_LABELS[action] ?? "其他操作";
}

export function actionStatusLabel(status: ActionStatus): string {
  return ACTION_STATUS_LABELS[status] ?? "已处理";
}

export type AttemptKind = "initial" | "supplement" | "correction";

const ATTEMPT_KIND_LABELS: Record<AttemptKind, string> = {
  initial: "首次作答",
  supplement: "补充过程",
  correction: "订正",
};

export function attemptKindLabel(kind: string | null | undefined): string {
  return ATTEMPT_KIND_LABELS[(kind ?? "") as AttemptKind] ?? "本次作答";
}

/**
 * 归一化用于“是否同一段话”的比较：全半角、大小写、空白和常见标点忽略。
 * 只用于判断重复，不回写任何文本。
 */
export function normalizeText(value: string): string {
  return value
    .normalize("NFKC")
    .toLowerCase()
    .replace(/\s+/g, "")
    .replace(/[，。！？；：、,.!?;:·\-—–…"'“”‘’（）()【】\[\]《》<>]/g, "");
}

export function splitParagraphs(value: string | null | undefined): string[] {
  if (!value) {
    return [];
  }
  return value
    .split(/\n+/)
    .map((line) => line.trim())
    .filter(Boolean);
}

/**
 * 按优先级依次展示自然语言回复、结果消息和动作消息，后出现且归一化相同的段落不再重复。
 * 返回“需要显示”的文本片段，调用方据此渲染。
 */
export function dedupeTexts(
  candidates: Array<string | null | undefined>,
): { shown: string[]; duplicate: boolean[] } {
  const seen = new Set<string>();
  const shown: string[] = [];
  const duplicate: boolean[] = [];
  for (const candidate of candidates) {
    const paragraphs = splitParagraphs(candidate);
    let allDuplicate = paragraphs.length > 0;
    const kept: string[] = [];
    for (const paragraph of paragraphs) {
      const normalized = normalizeText(paragraph);
      if (!normalized) {
        continue;
      }
      if (seen.has(normalized)) {
        continue;
      }
      seen.add(normalized);
      kept.push(paragraph);
      allDuplicate = false;
    }
    shown.push(kept.join("\n"));
    duplicate.push(allDuplicate);
  }
  return { shown, duplicate };
}

/** 单段文本是否已在已展示文本中出现（归一化后相同）。 */
export function isDuplicateText(
  candidate: string | null | undefined,
  shown: Array<string | null | undefined>,
): boolean {
  const normalized = normalizeText(candidate ?? "");
  if (!normalized) {
    return false;
  }
  return shown.some((item) => normalizeText(item ?? "") === normalized);
}

/**
 * 这段文字是否已经被上方叙事完整包含。
 * 用于动作结果消息：叙事已经原样说了同样的话时，不再单独重复一遍；
 * 只做“整段包含”判断，避免吞掉叙事里没有的真实结果。
 */
export function isCoveredText(
  candidate: string | null | undefined,
  shown: Array<string | null | undefined>,
): boolean {
  const normalized = normalizeText(candidate ?? "");
  if (!normalized) {
    return false;
  }
  return shown.some((item) => {
    const other = normalizeText(item ?? "");
    return other.length > 0 && (other === normalized || other.includes(normalized));
  });
}

/** 会话时间：缺失或无法解析时如实说明，不回退到空字符串或编造时间。 */
export function formatDateTime(
  value: string | null | undefined,
  fallback = "时间未知",
): string {
  if (!value) {
    return fallback;
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return fallback;
  }
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/** 考试日期：目录没有登记时明确标出来，不用空白或猜测日期填充。 */
export function formatExamDate(value: string | null | undefined): {
  text: string;
  known: boolean;
} {
  const trimmed = value?.trim();
  if (!trimmed) {
    return { text: "时间待补充", known: false };
  }
  return { text: trimmed, known: true };
}

/** 文件大小：小于 1 MB 按 KB 显示，否则换算为保留一位小数的 MB。 */
export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) {
    return "大小未知";
  }
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KB`;
  }
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

// 与 MathText 的公式定界规则一致：$$…$$、\[…\]、\(…\)、$…$
const MATH_TOKEN = /(?<!\\)\$\$[\s\S]+?\$\$|(?<!\\)\\\[[\s\S]+?\\\]|(?<!\\)\\\([\s\S]+?\\\)|(?<![\\$])\$(?!\$)(?:\\.|[^\\$\n])+?\$|(?<opener>(?<!\\)(?:\$\$|\\\[|\\\(|(?<!\$)\$(?!\$)))/g;

/**
 * 按长度截断文本，但不把公式从中间切开：截断点落在公式内部时退到公式之前，
 * 公式位于开头时保留整段公式；被截断时补省略号。与后端 app/text_excerpt.py 规则一致。
 */
export function mathSafeExcerpt(text: string, limit: number, ellipsis = "…"): string {
  const compact = text.trim();
  if (compact.length <= limit) {
    return compact;
  }
  let cut = Math.max(limit - ellipsis.length, 1);
  for (const match of compact.matchAll(MATH_TOKEN)) {
    const start = match.index ?? 0;
    const end = start + match[0].length;
    if (start >= cut) {
      break;
    }
    if (match.groups?.opener) {
      cut = start;
      break;
    }
    if (cut < end) {
      cut = start > 0 ? start : end;
      break;
    }
  }
  const head = compact.slice(0, cut).trimEnd();
  if (head.length >= compact.length) {
    return compact;
  }
  return `${head}${ellipsis}`;
}

export type SuggestionIntent = "answer" | "reveal" | "save" | "practice" | "chat";

/**
 * 建议按钮按文字归到有限几类操作，便于绑定到该轮的具体对象。
 * 匹配不到的仍按自然语言发送，不强行解释成按钮流程。
 */
export function classifySuggestion(label: string): SuggestionIntent {
  const text = label.trim();
  if (/(看|查看|直接看|展开|显示).{0,6}(解答|解析|答案)/.test(text)) {
    return "reveal";
  }
  if (/(提交|我的).{0,6}(答案|作答)/.test(text)) {
    return "answer";
  }
  if (/(保存|存起来|存到|记录到|加入).{0,8}(错题|错题本)/.test(text)) {
    return "save";
  }
  if (/(练|做题|来一道|类似题|出题|再练)/.test(text)) {
    return "practice";
  }
  return "chat";
}

/** 学习记忆的证据等级：只描述这一次记录，不换算成掌握率。 */
const VERDICT_LABELS: Record<string, string> = {
  wrong_independent: "独立作答错误",
  correct_independent: "独立作答正确",
  after_reveal: "看答案后作答",
  insufficient: "记录不足",
  unclear: "独立性未确认",
  self_report: "学生自述",
  no_attempt: "尚未作答",
};

export function memoryVerdictLabel(verdict: string | null | undefined): string {
  return VERDICT_LABELS[verdict ?? ""] ?? "记录待核对";
}

const VALIDITY_LABELS: Record<string, string> = {
  ok: "有效",
  superseded: "已被订正",
  unverified: "无法核对",
  unavailable: "暂时读不到",
};

export function memoryValidityLabel(validity: string | null | undefined): string {
  return VALIDITY_LABELS[validity ?? ""] ?? "待核对";
}

export type DownloadPhase = "downloading" | "triggered" | "error";

export type DownloadState = {
  phase: DownloadPhase;
  message?: string;
};

export type DownloadStates = Record<string, DownloadState>;

type LessonContent = {
  knowledge_points: { name: string; explanation: string }[];
  steps: { index: number; title: string; content: string }[];
  final_answer: string;
  error_analysis: string | null;
};

export function explanationBody(lesson: LessonContent): string {
  const sections: string[] = [];
  if (lesson.knowledge_points.length) {
    sections.push("## 知识点\n\n" + lesson.knowledge_points.map(
      ({ name, explanation }) => "- **" + name + "**：" + explanation,
    ).join("\n"));
  }
  if (lesson.error_analysis) {
    sections.push("## 你解法中的具体问题\n\n" + lesson.error_analysis);
  }
  if (lesson.steps.length) {
    sections.push("## 解题步骤\n\n" + lesson.steps.map(
      ({ index, title, content }) => "### " + index + ". " + title + "\n\n" + content,
    ).join("\n\n"));
  }
  if (lesson.final_answer) sections.push("## 最终答案\n\n" + lesson.final_answer);
  return sections.join("\n\n");
}

function paperFields(value: string) {
  const text = value.normalize("NFKC");
  const year = text.match(/\d{4}\s*[-—–]\s*\d{4}|\d{4}\s*级/)?.[0];
  const term = text.match(/第([一二两12])学期/)?.[1];
  const type = text.match(/期[中末]/)?.[0];
  const core = normalizeText(text
    .replace(/\d{4}\s*[-—–]\s*\d{4}|\d{4}\s*级/g, "")
    .replace(/学年|第[一二两12]学期/g, "")
    .replace(/(?:期末|期中)?(?:考试)?(?:试题及答案|试题|试卷|真题)/g, ""));
  return {
    year: year ? normalizeText(year) : "",
    term: term ? (term === "一" || term === "1" ? "1" : "2") : "",
    type,
    core: core.replace(/^高数/, "高等数学"),
  };
}

const PAPER_METADATA_HEADINGS = /^(?:序号|学年|学期|学年学期|年份|科目|课程|试卷|卷名|试卷名称|试卷标题|考试|考试类型|考试日期|考试时间|类型|时间|日期|下载|原卷|原始pdf|pdf|下载链接|试卷下载)$/i;

function isAcademicYear(value: string): boolean {
  return /^\d{4}\s*[-—–]\s*\d{4}(?:\s*学年)?$/.test(value.trim());
}

function isPaperMetadata(value: string): boolean {
  return isAcademicYear(value) || /^(?:\d{1,2}|[-—–]+|\d{4}[-—–]\d{1,2}[-—–]\d{1,2}|期[中末]|高等数学|高数|时间待补充|(?:下载)?(?:原始)?pdf|下载(?:原卷)?|原卷)$/i.test(value.normalize("NFKC").replace(/\s/g, ""));
}

function tableCells(line: string): string[] {
  return line.trim().replace(/^\||\|$/g, "").split("|").map((cell) => cell.trim());
}

/** 已有结果卡时移除能核实的试卷清单，保留每一条额外分析和比较。 */
export function stripDuplicatePaperList(
  assistantText: string,
  items: ReadonlyArray<{ id: string; title: string }>,
): string {
  if (!assistantText.trim() || !items.length) return assistantText;

  const entries = items.map(({ title }) => paperFields(title));
  const matchesEntry = (value: string) => {
    const content = value
      .replace(/^\s*(?:[-*•]|\d+[.)、])\s+/, "")
      .replace(/\[([^\]]+)\]\(\/api\/papers\/([^/)]+)\/download\)/g, "$1")
      .replace(/[（(]\s*(?:下载(?:原卷)?|(?:原始)?PDF)\s*[)）]\s*$/i, "")
      .replace(/^【|】$/g, "")
      .replace(/[*_]/g, "");
    const fields = paperFields(content);
    return entries.some((entry) =>
      fields.core.length >= 3 &&
      fields.core === entry.core &&
      fields.year === entry.year &&
      (!fields.term || !entry.term || fields.term === entry.term) &&
      (!fields.type || !entry.type || fields.type === entry.type),
    );
  };
  const matchesRow = (row: string) => {
    const cells = tableCells(row);
    // 年份和题名可分列。只忽略已知目录字段，保留包含额外说明的行。
    return matchesEntry(cells.filter((cell) => !isPaperMetadata(cell) || isAcademicYear(cell)).join(" ")) ||
      (cells.some(matchesEntry) && cells.every((cell) => isPaperMetadata(cell) || matchesEntry(cell)));
  };

  const lines = assistantText.split(/\r?\n/);
  const kept: string[] = [];
  for (let index = 0; index < lines.length; index += 1) {
    const line = lines[index];
    const separator = lines[index + 1] ?? "";
    if (line.includes("|") && /^\s*\|?[\s:|-]+\|[\s:|-]*$/.test(separator)) {
      let next = index + 2;
      while (next < lines.length && lines[next].includes("|") && lines[next].trim()) next += 1;
      const rows = lines.slice(index + 2, next);
      const metadataTable = tableCells(line).every((cell) => PAPER_METADATA_HEADINGS.test(normalizeText(cell).replace(/[／/]/g, "")));
      if (!metadataTable || !rows.length || !rows.every(matchesRow)) {
        kept.push(...lines.slice(index, next));
      }
      index = next - 1;
    } else if (!matchesEntry(line)) {
      kept.push(line);
    }
  }
  return kept.join("\n").trim();
}
