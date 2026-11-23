import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, test, vi } from "vitest";
import CoverAskPreview, {
  ALL_COVER_ASK_ITEMS,
  COVER_ASK_LEAD,
  COVER_ASK_POOL,
  buildCoverAskRound,
} from "./CoverAskPreview";

afterEach(() => {
  vi.useRealTimers();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  Object.defineProperty(document, "hidden", { value: false, configurable: true });
});

function advanceSteps(count: number, ms: number) {
  for (let i = 0; i < count; i += 1) {
    act(() => {
      vi.advanceTimersByTime(ms);
    });
  }
}

describe("CoverAskPreview", () => {
  test("12 句示例清单完整", () => {
    expect(ALL_COVER_ASK_ITEMS).toHaveLength(12);
    for (const item of ALL_COVER_ASK_ITEMS) {
      expect(item.text.trim().length).toBeGreaterThan(0);
      expect(item.text.length).toBeLessThanOrEqual(22);
    }

    expect(new Set(ALL_COVER_ASK_ITEMS.map((item) => item.text)).size).toBe(12);
  });

  test.each([0, 0.1, 0.49, 0.75, 0.99])("连续三轮均用完候选且相邻不同组，随机值=%s", (randomValue) => {
    let lastGroup = COVER_ASK_LEAD[2].group;
    for (let round = 0; round < 3; round += 1) {
      const order = buildCoverAskRound(lastGroup, () => randomValue);
      expect(order.map((item) => item.text).sort()).toEqual(COVER_ASK_POOL.map((item) => item.text).sort());
      for (const item of order) {
        expect(item.group).not.toBe(lastGroup);
        lastGroup = item.group;
      }
    }
  });

  test("按固定顺序轮播前 3 句，并可点击进入登录", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    const onEnterLogin = vi.fn();

    render(<CoverAskPreview onEnterLogin={onEnterLogin} />);

    const btn = screen.getByRole("button", { name: "登录后开始提问" });
    expect(screen.getByText("一句话说出你想复习什么")).toBeTruthy();

    // 第一句：“我要复习高数”（6 字），每次 110ms
    advanceSteps(6, 110);
    expect(btn.textContent).toContain("我要复习高数");

    // 停留 1600ms -> 删除 6 次 * 40ms -> 换句前空 400ms
    advanceSteps(1, 1600);
    advanceSteps(6, 40);
    advanceSteps(1, 400);

    // 第二句：“找一道 Python 循环题”（14 字符）
    advanceSteps(14, 110);
    expect(btn.textContent).toContain("找一道 Python 循环题");

    // 停留 1600ms -> 删除 14 次 * 40ms -> 换句前空 400ms
    advanceSteps(1, 1600);
    advanceSteps(14, 40);
    advanceSteps(1, 400);

    // 第三句：“这道题我选 A，帮我讲讲”（12 字）
    advanceSteps(12, 110);
    expect(btn.textContent).toContain("这道题我选 A，帮我讲讲");

    fireEvent.click(btn);
    expect(onEnterLogin).toHaveBeenCalledTimes(1);
  });

  test("动画按整轮显示候选，用完后才进入下一轮", () => {
    vi.useFakeTimers();
    const random = () => 0.99;
    vi.spyOn(Math, "random").mockImplementation(random);
    const firstRound = buildCoverAskRound(COVER_ASK_LEAD[2].group, random);
    const secondRound = buildCoverAskRound(firstRound[firstRound.length - 1].group, random);
    const { container } = render(<CoverAskPreview onEnterLogin={vi.fn()} />);
    const animated = container.querySelector(".cover-ask-text");

    for (const item of [...COVER_ASK_LEAD, ...firstRound, secondRound[0]]) {
      advanceSteps(item.text.length, 170);
      expect(animated?.textContent).toBe(item.text);
      advanceSteps(1, 1600);
      advanceSteps(item.text.length, 40);
      advanceSteps(1, 400);
    }
  });

  test("开启减少动态效果时固定展示第一句且标记静态", () => {
    vi.useFakeTimers();
    vi.stubGlobal("matchMedia", () => ({ matches: true }));

    const { container } = render(<CoverAskPreview onEnterLogin={vi.fn()} />);
    const wrap = container.querySelector(".cover-ask-wrap");
    expect(wrap?.getAttribute("data-reduced-motion")).toBe("true");
    expect(container.textContent).toContain("我要复习高数");

    act(() => { vi.advanceTimersByTime(10_000); });
    expect(container.textContent).toContain("我要复习高数");
  });

  test("页面切到后台时暂停轮播，回到前台后继续", () => {
    vi.useFakeTimers();
    vi.spyOn(Math, "random").mockReturnValue(0);
    const { container } = render(<CoverAskPreview onEnterLogin={vi.fn()} />);

    // 打出前 3 个字：“我要复”
    advanceSteps(3, 110);
    const textBeforeHide = container.querySelector(".cover-ask-text")?.textContent;
    expect(textBeforeHide).toBe("我要复");

    // 切到后台
    Object.defineProperty(document, "hidden", { value: true, configurable: true });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    advanceSteps(10, 500);
    expect(container.querySelector(".cover-ask-text")?.textContent).toBe("我要复");

    // 回到前台继续打完剩余 3 字
    Object.defineProperty(document, "hidden", { value: false, configurable: true });
    act(() => { document.dispatchEvent(new Event("visibilitychange")); });
    advanceSteps(3, 110);
    expect(container.querySelector(".cover-ask-text")?.textContent).toBe("我要复习高数");
  });
});
