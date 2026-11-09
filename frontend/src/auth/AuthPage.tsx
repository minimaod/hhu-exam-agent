/** 登录/注册页；注册成功后服务端已设 Cookie，直接进入应用。 */

import { useState, type FormEvent } from "react";
import { useAuth } from "./AuthContext";

export type AuthMode = "login" | "register";

type Props = {
  mode: AuthMode;
  onSwitch: (mode: AuthMode) => void;
  onSuccess: () => void;
};

export default function AuthPage({ mode, onSwitch, onSuccess }: Props) {
  const { login, register } = useAuth();
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRegister = mode === "register";

  async function handleSubmit(event: FormEvent) {
    event.preventDefault();
    setError(null);
    if (isRegister && password !== confirm) {
      setError("两次输入的密码不一致。");
      return;
    }
    setSubmitting(true);
    try {
      if (isRegister) {
        await register(inviteCode.trim(), username.trim(), password);
      } else {
        await login(username.trim(), password);
      }
      onSuccess();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "请求失败，请稍后再试。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="mx-auto mt-10 w-full max-w-md rounded-3xl border border-slate-200 bg-white/90 p-6 shadow-sm">
      <p className="text-xs font-medium tracking-[0.2em] text-river">
        HHU EXAM REVIEW
      </p>
      <h1 className="mt-2 text-xl font-semibold text-ink">
        {isRegister ? "用邀请码注册" : "登录学习工作区"}
      </h1>
      <p className="mt-2 text-sm leading-6 text-slate-600">
        {isRegister
          ? "邀请码由管理员一次性发放；注册成功会自动登录。"
          : "登录后才能查看学习会话、错题本和学习记忆。试卷库仍可先浏览。"}
      </p>

      <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
        {isRegister && (
          <label className="block text-sm text-slate-700">
            邀请码
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
              value={inviteCode}
              onChange={(event) => setInviteCode(event.target.value)}
              autoComplete="one-time-code"
              required
            />
          </label>
        )}
        <label className="block text-sm text-slate-700">
          用户名
          <input
            className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            autoComplete="username"
            required
          />
        </label>
        <label className="block text-sm text-slate-700">
          密码
          <input
            className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
            type="password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            autoComplete={isRegister ? "new-password" : "current-password"}
            required
          />
        </label>
        {isRegister && (
          <label className="block text-sm text-slate-700">
            确认密码
            <input
              className="mt-1 w-full rounded-xl border border-slate-300 px-3 py-2 text-sm"
              type="password"
              value={confirm}
              onChange={(event) => setConfirm(event.target.value)}
              autoComplete="new-password"
              required
            />
          </label>
        )}

        {error && (
          <p role="alert" className="text-sm text-rose-700">
            {error}
          </p>
        )}

        <button
          type="submit"
          disabled={submitting}
          className="w-full rounded-xl bg-river px-4 py-2 text-sm font-medium text-white disabled:opacity-60"
        >
          {submitting ? "提交中…" : isRegister ? "注册并进入" : "登录"}
        </button>
      </form>

      <button
        type="button"
        className="mt-4 text-sm text-river underline-offset-2 hover:underline"
        onClick={() => onSwitch(isRegister ? "login" : "register")}
      >
        {isRegister ? "已有账号，去登录" : "没有账号？用邀请码注册"}
      </button>
    </section>
  );
}