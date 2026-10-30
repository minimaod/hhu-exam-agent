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
  select_question: "选择真题",
  view_question_answer: "查看真题答案",
  explain_question: "讲解真题",
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
    return { text: "日期未登记", known: false };
  }
  return { text: trimmed, known: true };
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

export type DownloadPhase = "downloading" | "triggered" | "error";

export type DownloadState = {
  phase: DownloadPhase;
  message?: string;
};

export type DownloadStates = Record<string, DownloadState>;