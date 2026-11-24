/** 登录/注册页；注册成功后服务端已设 Cookie，直接进入应用。 */

import { useEffect, useRef, useState, type FormEvent } from "react";
import hhuEmblem from "../assets/hhu-emblem.png";
import { useAuth } from "./AuthContext";
import CoverAskPreview from "./CoverAskPreview";

export type AuthMode = "login" | "register";

type Props = {
  mode: AuthMode;
  onSwitch: (mode: AuthMode) => void;
  onSuccess?: () => void;
  onGuest?: () => void;
  startWithCover?: boolean;
  reason?: string;
};

export default function AuthPage({
  mode,
  onSwitch,
  onSuccess,
  onGuest,
  startWithCover = false,
  reason,
}: Props) {
  const { login, register } = useAuth();
  const [showingCover, setShowingCover] = useState(startWithCover);
  const authHeading = useRef<HTMLHeadingElement>(null);
  const focusForm = useRef(Boolean(onGuest && !startWithCover));
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [inviteCode, setInviteCode] = useState("");
  const [confirm, setConfirm] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const isRegister = mode === "register";
  const PosterTitle = showingCover ? "h1" : "p";

  function enterLogin() {
    focusForm.current = true;
    setShowingCover(false);
  }

  useEffect(() => {
    if (!showingCover && focusForm.current) {
      focusForm.current = false;
      authHeading.current?.focus();
    }
  }, [showingCover]);

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
      onSuccess?.();
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : "请求失败，请稍后再试。");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="auth-split" data-cover={showingCover}>
      <div className="auth-poster">
        <div className="auth-poster-content">
          <div className="auth-poster-intro">
            <img className="brand-emblem auth-emblem" src={hhuEmblem} alt="河海大学校徽" />
            <p className="brand-subtitle">HOHAI UNIVERSITY · EXAM REVIEW</p>
            <PosterTitle className="auth-poster-title">河海大学<br className="auth-line-break" />考试复习助手</PosterTitle>
            <p className="auth-poster-description">查阅历年真题，练习解题推导，整理自己的错题。</p>
            {showingCover && (
              <div className="cover-actions">
                <button
                  type="button"
                  className="cover-continue"
                  onClick={enterLogin}
                >
                  登录
                </button>
                {onGuest && <button type="button" className="cover-guest" onClick={onGuest}>访客进入试卷库</button>}
              </div>
            )}
          </div>
          {showingCover && <CoverAskPreview onEnterLogin={enterLogin} />}
        </div>
      </div>
      <div className="auth-form-stage" inert={showingCover} aria-hidden={showingCover || undefined}>
        <div className="auth-box">
          <h1 ref={authHeading} tabIndex={-1} className="auth-title">
            {isRegister ? "用邀请码注册" : "登录学习工作区"}
          </h1>
          <p className="auth-description">
            {isRegister
              ? "邀请码由管理员发放，注册成功后会自动登录。"
              : reason ?? "登录后可以保存学习会话、查看学习记忆和整理错题。"}
          </p>

          <form className="mt-6 space-y-4" onSubmit={handleSubmit}>
            {isRegister && (
              <label className="block text-sm text-slate-700">
                邀请码
                <input
                  className="field-control"
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
                className="field-control"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                autoComplete="username"
                required
              />
            </label>
            <label className="block text-sm text-slate-700">
              密码
              <input
                className="field-control"
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
                  className="field-control"
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
              className="primary-button w-full"
            >
              {submitting ? "提交中…" : isRegister ? "注册并进入" : "登录"}
            </button>
          </form>

          <div className="auth-options">
            {onGuest && (
              <button type="button" className="quiet-button auth-guest" onClick={onGuest} disabled={submitting}>
                访客进入试卷库
              </button>
            )}
            <button
              type="button"
              className="auth-switch"
              onClick={() => onSwitch(isRegister ? "login" : "register")}
              disabled={submitting}
            >
              {isRegister ? "已有账号，去登录" : "没有账号？用邀请码注册"}
            </button>
          </div>
        </div>
      </div>
    </section>
  );
}