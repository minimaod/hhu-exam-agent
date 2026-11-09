import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { AuthProvider } from "./AuthContext";
import AuthPage from "./AuthPage";

const { fetchMe, login, register, logout } = vi.hoisted(() => ({
  fetchMe: vi.fn(),
  login: vi.fn(),
  register: vi.fn(),
  logout: vi.fn(),
}));

vi.mock("../lib/authApi", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../lib/authApi")>()),
  fetchMe,
  login,
  register,
  logout,
}));

const user = {
  id: "u-1",
  username: "alice",
  created_at: "2026-01-01T00:00:00+00:00",
};

test("登录成功后进入应用，失败显示统一错误", async () => {
  fetchMe.mockResolvedValue(null);
  login.mockRejectedValueOnce(new Error("用户名或密码错误。"));
  login.mockResolvedValueOnce(user);
  const onSuccess = vi.fn();

  render(
    <AuthProvider>
      <AuthPage mode="login" onSwitch={() => undefined} onSuccess={onSuccess} />
    </AuthProvider>,
  );

  fireEvent.change(screen.getByLabelText("用户名"), {
    target: { value: "alice" },
  });
  fireEvent.change(screen.getByLabelText("密码"), {
    target: { value: "password123" },
  });
  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toContain("用户名或密码错误"),
  );

  fireEvent.click(screen.getByRole("button", { name: "登录" }));
  await waitFor(() => expect(onSuccess).toHaveBeenCalledTimes(1));
});

test("注册页要求两次密码一致", async () => {
  fetchMe.mockResolvedValue(null);
  render(
    <AuthProvider>
      <AuthPage mode="register" onSwitch={() => undefined} onSuccess={() => undefined} />
    </AuthProvider>,
  );
  fireEvent.change(screen.getByLabelText("邀请码"), {
    target: { value: "invite-code" },
  });
  fireEvent.change(screen.getByLabelText("用户名"), {
    target: { value: "alice" },
  });
  fireEvent.change(screen.getByLabelText("密码"), {
    target: { value: "password123" },
  });
  fireEvent.change(screen.getByLabelText("确认密码"), {
    target: { value: "different" },
  });
  fireEvent.click(screen.getByRole("button", { name: "注册并进入" }));
  await waitFor(() =>
    expect(screen.getByRole("alert").textContent).toContain("两次输入的密码不一致"),
  );
  expect(register).not.toHaveBeenCalled();
});