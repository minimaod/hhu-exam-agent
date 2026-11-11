import { render, screen } from "@testing-library/react";
import { describe, expect, test } from "vitest";
import UsageNotice from "../components/UsageNotice";
import type { UsageSnapshot } from "../lib/usageApi";

const base: UsageSnapshot = {
  date: "2026-11-10",
  reset_at: "2026-11-11T00:00:00+08:00",
  requests: { limit: 2, used: 1, remaining: 1 },
  site_exhausted: false,
  images: { limit: 20, used: 3, remaining: 17 },
  active_turn: false,
};

describe("UsageNotice", () => {
  test("显示剩余次数并说明一轮可能消耗多次", () => {
    render(<UsageNotice usage={base} />);
    expect(screen.getByText(/今日剩余模型请求/)).toBeTruthy();
    expect(screen.getByText(/一轮复杂对话可能消耗/)).toBeTruthy();
  });

  test("个人额度用完显示原因和重置时间", () => {
    render(
      <UsageNotice
        usage={{
          ...base,
          requests: { limit: 2, used: 2, remaining: 0 },
        }}
      />,
    );
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("你今天的 2 次模型请求已用完");
    expect(alert.textContent).toContain("重置");
  });

  test("全站耗尽只显示布尔原因", () => {
    render(<UsageNotice usage={{ ...base, site_exhausted: true }} />);
    expect(screen.getByRole("alert").textContent).toContain(
      "全站今天的模型请求额度已用完",
    );
  });
});