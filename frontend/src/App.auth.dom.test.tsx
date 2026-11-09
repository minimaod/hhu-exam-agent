import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import App from "./App";

const { fetchMe, login, register, logout } = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
}));
const { fetchPapers, downloadPaper } = vi.hoisted(() => ({
  fetchPapers: vi.fn(),
  downloadPaper: vi.fn(),
}));
const { fetchHealth } = vi.hoisted(() => ({ fetchHealth: vi.fn() }));

vi.mock("./lib/authApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./lib/authApi")>()),
  fetchMe,
  login,
  register,
  logout,
}));
vi.mock("./lib/papersApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./lib/papersApi")>()),
  fetchPapers,
  downloadPaper,
}));
vi.mock("./lib/healthApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("./lib/healthApi")>()),
  fetchHealth,
}));

test("访客可以浏览试卷库，进入个人工作区时要求登录", async () => {
  fetchMe.mockResolvedValue(null);
  fetchHealth.mockResolvedValue({ status: "ok", service: "hhu-exam-review" });
  fetchPapers.mockResolvedValue({
    items: [
      {
        id: "p-1",
        title: "高数期中",
        course: "高等数学BⅠ",
        academic_year: "2022-2023",
        exam_type: "midterm",
        exam_date: "2022-11-08",
        download_url: "/api/papers/p-1/download",
      },
    ],
    total: 1,
  });

  render(<App />);

  await waitFor(() => expect(screen.getByText("共 1 份试卷")).toBeTruthy());
  expect(screen.getByText("已登记的试卷")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "学习会话" }));
  await waitFor(() => expect(screen.getByText("登录学习工作区")).toBeTruthy());
});

test("直接打开 /register 显示注册页（刷新路径）", async () => {
  window.history.replaceState(null, "", "/register");
  fetchMe.mockResolvedValue(null);
  fetchHealth.mockResolvedValue({ status: "ok", service: "hhu-exam-review" });
  fetchPapers.mockResolvedValue({ items: [], total: 0 });

  render(<App />);
  await waitFor(() => expect(screen.getByText("用邀请码注册")).toBeTruthy());
  window.history.replaceState(null, "", "/");
});