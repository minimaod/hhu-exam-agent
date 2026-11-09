import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { AuthProvider, storageKeyFor, useAuth } from "./AuthContext";

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

const user = { id: "a", username: "alice", created_at: "2026-01-01T00:00:00+00:00" };

function Probe() {
  const { logout: doLogout, user: current } = useAuth();
  return (
    <div>
      <span>{current?.username ?? "guest"}</span>
      <button type="button" onClick={() => void doLogout()}>
        logout
      </button>
    </div>
  );
}

test("退出登录清理当前用户的会话恢复键", async () => {
  fetchMe.mockResolvedValue(user);
  logout.mockResolvedValue(undefined);
  const key = storageKeyFor(user.id);
  expect(key).not.toBeNull();
  window.localStorage.setItem(key as string, "conv-a");
  window.history.replaceState(null, "", "/");

  render(
    <AuthProvider>
      <Probe />
    </AuthProvider>,
  );
  await waitFor(() => expect(screen.getByText("alice")).toBeTruthy());
  fireEvent.click(screen.getByRole("button", { name: "logout" }));
  await waitFor(() => expect(screen.getByText("guest")).toBeTruthy());
  expect(window.localStorage.getItem(key as string)).toBeNull();
});