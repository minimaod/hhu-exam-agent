import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, expect, test, vi } from "vitest";
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

beforeEach(() => {
  window.history.replaceState(null, "", "/");
  window.sessionStorage.clear();
  fetchMe.mockResolvedValue(null);
  fetchHealth.mockResolvedValue({ status: "ok", service: "hhu-exam-review" });
  fetchPapers.mockResolvedValue({ items: [], total: 0 });
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

const coverHeading = /河海大学\s*考试复习助手/;
const archiveHeading = "河海大学历年试卷馆藏目录";
const filingNumber = "湘ICP备2026055716号-1";

test("首次访问停留在封面，点击登录才显示表单", async () => {
  vi.useFakeTimers();
  await act(async () => { render(<App />); });

  expect(screen.getByRole("heading", { name: coverHeading })).toBeTruthy();
  expect(screen.getByRole("link", { name: filingNumber }).getAttribute("href")).toBe("https://beian.miit.gov.cn/");
  expect(screen.getByRole("img", { name: "河海大学校徽" }).getAttribute("src")).toContain("hhu-emblem.png");
  await act(async () => { vi.advanceTimersByTime(10_000); });
  expect(screen.getByRole("heading", { name: coverHeading })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "登录学习工作区" })).toBeNull();
  expect(screen.getByRole("button", { name: "访客进入试卷库" })).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  expect(screen.getByRole("heading", { name: "登录学习工作区" })).toBeTruthy();
  expect(screen.getByRole("link", { name: filingNumber })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: coverHeading })).toBeNull();
  expect(screen.getByRole("button", { name: "访客进入试卷库" })).toBeTruthy();
});

test("开启减少动态效果时也由用户从封面进入登录", async () => {
  vi.stubGlobal("matchMedia", () => ({ matches: true }));
  render(<App />);

  expect(await screen.findByRole("heading", { name: coverHeading })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: "登录学习工作区" })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  expect(screen.getByRole("heading", { name: "登录学习工作区" })).toBeTruthy();
});

test("登录页可进入试卷库，访客点击私有入口时在馆藏内提示登录", async () => {
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
  const protectedRequest = vi.fn(() => new Response("", { status: 401 }));
  vi.stubGlobal("fetch", protectedRequest);
  render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "登录" }));
  const loginHeading = screen.getByRole("heading", { name: "登录学习工作区" });
  expect(document.activeElement).toBe(loginHeading);
  fireEvent.click(screen.getByRole("button", { name: "访客进入试卷库" }));

  expect(await screen.findByText("共 1 份试卷")).toBeTruthy();
  expect(screen.getByRole("img", { name: "河海大学校徽" })).toBeTruthy();
  expect(screen.getByText("已登记的试卷")).toBeTruthy();
  expect(screen.getByRole("button", { name: "下载原始 PDF" })).toBeTruthy();

  fireEvent.click(within(screen.getByRole("navigation", { name: "主导航" })).getByRole("button", { name: "学习会话" }));
  expect(screen.getByRole("heading", { name: "登录后新建学习会话、上传题目并保存讲解。" })).toBeTruthy();
  expect(screen.getByText("共 1 份试卷")).toBeTruthy();
  expect(window.location.pathname).toBe("/");

  fireEvent.click(screen.getByRole("button", { name: "登录后继续" }));
  expect(screen.getByRole("heading", { name: "登录学习工作区" })).toBeTruthy();
  expect(screen.getByText("登录后新建学习会话、上传题目并保存讲解。")).toBeTruthy();
  expect(window.location.pathname).toBe("/login");

  fireEvent.click(screen.getByRole("button", { name: "访客进入试卷库" }));
  expect(screen.getByRole("heading", { name: archiveHeading })).toBeTruthy();
  expect(window.location.pathname).toBe("/");
  expect(protectedRequest).not.toHaveBeenCalled();
});

test("可从封面直接进入试卷库，访客刷新后留在馆藏", async () => {
  const first = render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "访客进入试卷库" }));
  expect(screen.getByRole("heading", { name: archiveHeading })).toBeTruthy();
  await screen.findByText("没有符合条件的试卷");
  first.unmount();

  render(<App />);
  expect(await screen.findByRole("heading", { name: archiveHeading })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: coverHeading })).toBeNull();
});

test("直接打开 /register 跳过封面，且可以进入试卷库", async () => {
  window.history.replaceState(null, "", "/register");
  render(<App />);

  expect(await screen.findByRole("heading", { name: "用邀请码注册" })).toBeTruthy();
  expect(screen.getByRole("link", { name: filingNumber })).toBeTruthy();
  expect(screen.queryByRole("heading", { name: coverHeading })).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: "访客进入试卷库" }));
  expect(screen.queryByRole("heading", { name: "用邀请码注册" })).toBeNull();
  expect(screen.getByRole("link", { name: filingNumber })).toBeTruthy();
  expect(screen.getByText("已登记的试卷")).toBeTruthy();
  expect(window.location.pathname).toBe("/");
});

test("从访客提示登录后进入原先选择的错题本", async () => {
  login.mockResolvedValue({ id: "test-user", username: "chen", created_at: "2026-11-11T00:00:00+08:00" });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const path = url.split("?")[0];
    if (path === "/api/conversations" || path === "/api/mistake-records") {
      return Response.json({ items: [], total: 0 });
    }
    if (path === "/api/learning-memory/courses") return Response.json({ items: [], pending: 0 });
    if (path === "/api/usage/today") {
      return Response.json({
        date: "2026-11-11", reset_at: "2099-01-01T00:00:00+08:00",
        requests: { limit: 30, used: 0, remaining: 30 },
        images: { limit: 20, used: 0, remaining: 20 },
        site_exhausted: false, active_turn: false,
      });
    }
    throw new Error("Unexpected request: " + url);
  }));

  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: "访客进入试卷库" }));
  fireEvent.click(within(screen.getByRole("navigation", { name: "主导航" })).getByRole("button", { name: "错题本" }));
  expect(screen.getByRole("heading", { name: "登录后查看你的错题本。" })).toBeTruthy();
  fireEvent.click(screen.getByRole("button", { name: "登录后继续" }));
  fireEvent.change(screen.getByLabelText("用户名"), { target: { value: "chen" } });
  fireEvent.change(screen.getByLabelText("密码"), { target: { value: "password123" } });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));

  expect(await screen.findByRole("heading", { name: "错题本" })).toBeTruthy();
  await waitFor(() => expect(window.location.pathname).toBe("/"));
  expect(window.history.state).toBeNull();
});

test("切换主视图保留会话和草稿，原会话跳转可消费并再次触发", async () => {
  const date = "2026-11-11T00:00:00+08:00";
  const sessions = [
    { id: "origin", title: "来源会话", created_at: date, updated_at: date },
    { id: "other", title: "另一会话", created_at: date, updated_at: date },
  ];
  fetchMe.mockResolvedValue({ id: "test-user", username: "chen", created_at: date });
  fetchHealth.mockResolvedValue({ status: "ok", service: "hhu-exam-review" });
  fetchPapers.mockResolvedValue({ items: [], total: 0 });
  vi.stubGlobal("fetch", vi.fn(async (url: string) => {
    const path = url.split("?")[0];
    if (path === "/api/conversations") return Response.json({ items: sessions, total: 2 });
    const conversation = sessions.find(({ id }) => path === "/api/conversations/" + id);
    if (conversation) return Response.json({ conversation, turns: [], active_problem: null, record: null });
    if (path === "/api/mistake-records") return Response.json({ items: [], total: 0 });
    if (path === "/api/usage/today") {
      return Response.json({
        date: "2026-11-11", reset_at: "2099-01-01T00:00:00+08:00",
        requests: { limit: 30, used: 0, remaining: 30 },
        images: { limit: 20, used: 0, remaining: 20 },
        site_exhausted: false, active_turn: false,
      });
    }
    if (path === "/api/learning-memory/courses") {
      return Response.json({ items: [{ course: "高等数学", label: "高等数学", evidence: 1 }], pending: 0 });
    }
    if (path === "/api/learning-memory") {
      return Response.json({ entries: [{ entry_id: "exercise:1", source: { conversation_id: "origin" } }] });
    }
    throw new Error("Unexpected request: " + url);
  }));

  render(<App />);
  fireEvent.click(await screen.findByRole("button", { name: /另一会话/ }));
  const composer = await screen.findByRole("textbox", { name: "学习指令" }) as HTMLTextAreaElement;
  fireEvent.change(composer, { target: { value: "尚未发送的草稿" } });
  const mainNav = within(screen.getByRole("navigation", { name: "主导航" }));
  fireEvent.click(mainNav.getByRole("button", { name: "试卷库" }));
  expect(screen.queryByRole("textbox", { name: "学习指令" })).toBeNull();
  fireEvent.click(mainNav.getByRole("button", { name: "学习会话" }));
  expect(screen.getByRole("textbox", { name: "学习指令" })).toBe(composer);
  expect(composer.value).toBe("尚未发送的草稿");

  const openOrigin = async () => {
    fireEvent.click(mainNav.getByRole("button", { name: "学习记忆" }));
    fireEvent.click(await screen.findByRole("button", { name: "回到原会话" }));
    await screen.findByRole("heading", { name: "来源会话" });
  };
  await openOrigin();
  fireEvent.click(screen.getByRole("button", { name: /另一会话/ }));
  fireEvent.click(mainNav.getByRole("button", { name: "试卷库" }));
  fireEvent.click(mainNav.getByRole("button", { name: "学习会话" }));
  expect(await screen.findByRole("heading", { name: "另一会话" })).toBeTruthy();
  await openOrigin();
});

test("封面动态提问框点击后进入登录表单并隐藏自身", async () => {
  render(<App />);

  const askButton = await screen.findByRole("button", { name: "登录后开始提问" });
  expect(screen.queryByText("2021–2022 学年期中试卷 · 第 11 题 · 填空题")).toBeNull();
  expect(screen.getByText("一句话说出你想复习什么")).toBeTruthy();

  fireEvent.click(askButton);
  const loginHeading = screen.getByRole("heading", { name: "登录学习工作区" });
  expect(document.activeElement).toBe(loginHeading);
  expect(screen.queryByRole("button", { name: "登录后开始提问" })).toBeNull();
});

test("移动端底部导航保持 5 项且命名统一，试卷库分批展示并支持加载更多", async () => {
  const manyPapers = Array.from({ length: 35 }, (_, i) => ({
    id: `p-${i + 1}`,
    title: `高等数学试卷 ${i + 1}`,
    course: "高等数学",
    academic_year: "2024-2025",
    exam_type: "final" as const,
    exam_date: "2025-01-10",
    download_url: `/api/papers/p-${i + 1}/download`,
  }));
  fetchPapers.mockResolvedValue({ items: manyPapers, total: manyPapers.length });
  render(<App />);

  fireEvent.click(await screen.findByRole("button", { name: "访客进入试卷库" }));
  expect(await screen.findByText("共 35 份试卷")).toBeTruthy();

  const mobileNav = within(screen.getByRole("navigation", { name: "移动端导航" }));
  const mobileButtons = mobileNav.getAllByRole("button").map((btn) => btn.textContent);
  expect(mobileButtons).toEqual(["学习会话", "试卷库", "学习记忆", "错题本", "我的"]);

  expect(screen.getByText("高等数学试卷 30")).toBeTruthy();
  expect(screen.queryByText("高等数学试卷 31")).toBeNull();
  fireEvent.click(screen.getByRole("button", { name: /加载更多（剩余 5 份）/ }));
  expect(screen.getByText("高等数学试卷 35")).toBeTruthy();
});
