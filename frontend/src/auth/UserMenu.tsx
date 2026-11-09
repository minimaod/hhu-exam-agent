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
      <div className="flex items-center gap-2 text-[11px]">
        <button
          type="button"
          onClick={onLogin}
          className="rounded-lg border border-slate-300 bg-white px-2 py-1 font-medium text-slate-700 hover:bg-slate-100"
        >
          登录
        </button>
        <button
          type="button"
          onClick={onRegister}
          className="rounded-lg bg-river px-2 py-1 font-medium text-white"
        >
          注册
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2 text-[11px] text-slate-600">
      <span className="max-w-[8rem] truncate" title={user.username}>
        {user.username}
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
        className="rounded-lg border border-slate-300 bg-white px-2 py-1 font-medium text-slate-700 hover:bg-slate-100 disabled:opacity-60"
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