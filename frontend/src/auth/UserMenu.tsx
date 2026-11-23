/** 顶栏用户区：已登录显示用户名和退出，访客显示登录/注册。 */

import { useState } from "react";
import { useAuth } from "./AuthContext";

export default function UserMenu({
  onLogin,
  onRegister,
}: {
  onLogin: () => void;
  onRegister: () => void;
}) {
  const { user, logout } = useAuth();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  if (!user) {
    return (
      <div className="flex items-center gap-2 text-xs">
        <button
          type="button"
          onClick={onLogin}
          className="quiet-button"
        >
          登录
        </button>
        <button
          type="button"
          onClick={onRegister}
          className="primary-button"
        >
          注册
        </button>
      </div>
    );
  }

  return (
    <div className="user-row">
      <span className="user-identity">
        <span className="user-avatar" aria-hidden="true">{Array.from(user.username)[0]?.toUpperCase()}</span>
        <span className="user-name" title={user.username}>{user.username}</span>
      </span>
      <button
        type="button"
        disabled={busy}
        onClick={async () => {
          setBusy(true);
          setError(null);
          try {
            await logout();
          } catch (err: unknown) {
            setError(err instanceof Error ? err.message : "退出失败，请重试。");
          } finally {
            setBusy(false);
          }
        }}
        className="quiet-button"
      >
        {busy ? "退出中…" : "退出登录"}
      </button>
      {error && (
        <span role="alert" className="text-rose-700">
          {error}
        </span>
      )}
    </div>
  );
}