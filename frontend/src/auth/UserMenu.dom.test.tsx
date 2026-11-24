import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { AuthProvider } from "./AuthContext";
import UserMenu from "./UserMenu";

const { fetchMe, logout } = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("../lib/authApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/authApi")>()),
  fetchMe,
  logout,
  login: vi.fn(),
  register: vi.fn(),
}));

test("退出失败显示提示，重试成功后才回到访客", async () => {
  fetchMe.mockResolvedValue({
    id: "u-1",
    username: "alice",
    created_at: "2026-01-01T00:00:00+00:00",
  });
  logout.mockRejectedValueOnce(new Error("网络错误，请重试。"));
  logout.mockResolvedValueOnce(undefined);
  window.history.replaceState(null, "", "/");

  render(
    <AuthProvider>
      <UserMenu onLogin={() => undefined} onRegister={() => undefined} />
    </AuthProvider>,
  );
  await waitFor(() => expect(screen.getByText("alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toContain("网络错误"),
  );
  expect(screen.getByText("alice")).toBeTruthy();

  fireEvent.click(screen.getByRole("button", { name: "退出登录" }));
  await waitFor(() => expect(screen.getByRole("button", { name: "登录" })).toBeTruthy());
});