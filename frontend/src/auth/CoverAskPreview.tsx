/** 封面底部的动态提问预览框；点击或按 Enter/空格等同于点击“登录”。 */

import { useEffect, useState } from "react";

export type CoverAskGroup = "papers" | "practice" | "explain" | "mistakes";

export type CoverAskItem = {
  text: string;
  group: CoverAskGroup;
};

/** 固定开场 3 句，依次展示找试卷、刷真题、讲题三类核心能力。 */
export const COVER_ASK_LEAD: readonly CoverAskItem[] = [
  { text: "我要复习高数", group: "papers" },
  { text: "找一道 Python 循环题", group: "practice" },
  { text: "这道题我选 A，帮我讲讲", group: "explain" },
];

/** 其余 9 句示例，与开场 3 句共同构成 12 句完整清单（每句不超过 16 字）。 */
export const COVER_ASK_POOL: readonly CoverAskItem[] = [
  { text: "高数期末卷有哪些？", group: "papers" },
  { text: "有没有线代的期中试卷", group: "papers" },
  { text: "2024 年 Python 期末卷发我一下", group: "papers" },
  { text: "来一道列表的选择题", group: "practice" },
  { text: "给我出一道类似的变式题", group: "practice" },
  { text: "为什么 while 循环执行了 10 次？", group: "explain" },
  { text: "帮我看看这道题哪里做错了", group: "explain" },
  { text: "把这道题加入错题本", group: "mistakes" },
  { text: "我最近哪些知识点总出错？", group: "mistakes" },
];

export const ALL_COVER_ASK_ITEMS: readonly CoverAskItem[] = [
  ...COVER_ASK_LEAD,
  ...COVER_ASK_POOL,
];

function arrangeAskItems(items: readonly CoverAskItem[], lastGroup: CoverAskGroup): CoverAskItem[] | null {
  if (items.length === 0) return [];
  for (const item of items) {
    if (item.group === lastGroup) continue;
    const rest = arrangeAskItems(items.filter((candidate) => candidate !== item), item.group);
    if (rest) return [item, ...rest];
  }
  return null;
}

/** 每轮打乱候选，再排成相邻不同组的完整顺序，避免到最后只剩同组句子。 */
export function buildCoverAskRound(lastGroup: CoverAskGroup, random: () => number = Math.random): CoverAskItem[] {
  const shuffled = [...COVER_ASK_POOL];
  for (let index = shuffled.length - 1; index > 0; index -= 1) {
    const other = Math.floor(random() * (index + 1));
    [shuffled[index], shuffled[other]] = [shuffled[other], shuffled[index]];
  }
  const order = arrangeAskItems(shuffled, lastGroup);
  if (!order) throw new Error("封面提问示例需要能按不同组交替排列");
  return order;
}

function prefersReducedMotion(): boolean {
  if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
    return false;
  }
  return Boolean(window.matchMedia("(prefers-reduced-motion: reduce)")?.matches);
}

type Phase = "typing" | "holding" | "deleting" | "pausing";

type Props = {
  onEnterLogin: () => void;
};

export default function CoverAskPreview({ onEnterLogin }: Props) {
  const [reducedMotion, setReducedMotion] = useState<boolean>(prefersReducedMotion);
  const [hidden, setHidden] = useState<boolean>(() =>
    typeof document !== "undefined" ? Boolean(document.hidden) : false,
  );

  useEffect(() => {
    if (typeof window === "undefined" || typeof window.matchMedia !== "function") {
      return;
    }
    const mq = window.matchMedia("(prefers-reduced-motion: reduce)");
    if (!mq) {
      return;
    }
    const onChange = () => {
      setReducedMotion(Boolean(mq.matches));
    };
    mq.addEventListener?.("change", onChange);
    return () => {
      mq.removeEventListener?.("change", onChange);
    };
  }, []);

  useEffect(() => {
    if (typeof document === "undefined") {
      return;
    }
    const onVisibility = () => {
      setHidden(Boolean(document.hidden));
    };
    document.addEventListener("visibilitychange", onVisibility);
    return () => {
      document.removeEventListener("visibilitychange", onVisibility);
    };
  }, []);

  // 动画进度保存在组件内部，后台暂停恢复时从当前句子与进度继续
  const [animState, setAnimState] = useState<{
    leadIndex: number;
    currentItem: CoverAskItem;
    remaining: CoverAskItem[];
    charCount: number;
    phase: Phase;
  }>(() => ({
    leadIndex: 0,
    currentItem: COVER_ASK_LEAD[0],
    remaining: [],
    charCount: 0,
    phase: "typing",
  }));

  useEffect(() => {
    if (reducedMotion || hidden) {
      return;
    }

    const { phase } = animState;

    let delay = 400;
    if (phase === "typing") {
      delay = 110 + Math.floor(Math.random() * 61);
    } else if (phase === "holding") {
      delay = 1600;
    } else if (phase === "deleting") {
      delay = 40;
    } else if (phase === "pausing") {
      delay = 400;
    }

    const timer = window.setTimeout(() => {
      const { currentItem, leadIndex, remaining } = animState;
      const round = phase === "pausing" && leadIndex + 1 >= COVER_ASK_LEAD.length && remaining.length === 0
        ? buildCoverAskRound(currentItem.group)
        : remaining;
      setAnimState((prev) => {
        if (prev.phase === "typing") {
          const nextCount = prev.charCount + 1;
          if (nextCount >= prev.currentItem.text.length) {
            return { ...prev, charCount: nextCount, phase: "holding" };
          }
          return { ...prev, charCount: nextCount };
        }

        if (prev.phase === "holding") {
          return { ...prev, phase: "deleting" };
        }

        if (prev.phase === "deleting") {
          const nextCount = Math.max(0, prev.charCount - 1);
          if (nextCount === 0) {
            return { ...prev, charCount: 0, phase: "pausing" };
          }
          return { ...prev, charCount: nextCount };
        }

        // phase === "pausing": 切换到下一句
        const nextLeadIndex = prev.leadIndex + 1;
        if (nextLeadIndex < COVER_ASK_LEAD.length) {
          const nextItem = COVER_ASK_LEAD[nextLeadIndex];
          return {
            ...prev,
            leadIndex: nextLeadIndex,
            currentItem: nextItem,
            charCount: 0,
            phase: "typing",
          };
        }

        const [next, ...rest] = round;
        return {
          ...prev,
          leadIndex: nextLeadIndex,
          currentItem: next,
          remaining: rest,
          charCount: 0,
          phase: "typing",
        };
      });
    }, delay);

    return () => {
      window.clearTimeout(timer);
    };
  }, [reducedMotion, hidden, animState]);

  const displayText = reducedMotion
    ? COVER_ASK_LEAD[0].text
    : animState.currentItem.text.slice(0, animState.charCount);

  return (
    <div className="cover-ask-wrap" data-reduced-motion={reducedMotion ? "true" : undefined}>
      <button
        type="button"
        className="cover-ask"
        aria-label="登录后开始提问"
        onClick={onEnterLogin}
      >
        <svg
          className="cover-ask-icon"
          width="18"
          height="18"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          strokeWidth="2"
          aria-hidden="true"
        >
          <circle cx="11" cy="11" r="7" />
          <path d="M20 20l-3.5-3.5" />
        </svg>
        <span className="cover-ask-text" aria-hidden="true">
          <span>{displayText}</span>
          <span className="cover-ask-caret" />
        </span>
        <span className="cover-ask-hint" aria-hidden="true">↵</span>
      </button>
      <p className="cover-ask-caption">一句话说出你想复习什么</p>
    </div>
  );
}
