/** F012 登录状态：启动时读取 /api/me，401 时回到访客。 */

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import {
  fetchMe,
  login as apiLogin,
  logout as apiLogout,
  register as apiRegister,
  type AuthUser,
} from "../lib/authApi";
import { UNAUTHORIZED_EVENT } from "../lib/apiFetch";

export type AuthPhase = "checking" | "anonymous" | "authenticated" | "error";

type AuthContextValue = {
  phase: AuthPhase;
  user: AuthUser | null;
  error: string | null;
  login: (username: string, password: string) => Promise<void>;
  register: (inviteCode: string, username: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  retry: () => void;
};

const AuthContext = createContext<AuthContextValue | null>(null);

const STORAGE_PREFIX = "hhu-exam-review.current-conversation";

export function storageKeyFor(userId: string | null): string | null {
  return userId ? `${STORAGE_PREFIX}.${userId}` : null;
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [phase, setPhase] = useState<AuthPhase>("checking");
  const [user, setUser] = useState<AuthUser | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    const controller = new AbortController();
    setPhase("checking");
    setError(null);
    fetchMe(controller.signal)
      .then((result) => {
        setUser(result);
        setPhase(result ? "authenticated" : "anonymous");
      })
      .catch((err: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        setError(err instanceof Error ? err.message : "无法确认登录状态。");
        setPhase("error");
      });
    return () => controller.abort();
  }, [attempt]);

  useEffect(() => {
    const onUnauthorized = () => {
      setUser(null);
      setPhase("anonymous");
      if (window.location.pathname !== "/login") {
        window.history.replaceState(null, "", "/login");
      }
    };
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const login = useCallback(async (username: string, password: string) => {
    const next = await apiLogin(username, password);
    setUser(next);
    setPhase("authenticated");
    setError(null);
  }, []);

  const register = useCallback(
    async (inviteCode: string, username: string, password: string) => {
      const next = await apiRegister(inviteCode, username, password);
      setUser(next);
      setPhase("authenticated");
      setError(null);
    },
    [],
  );

  const logout = useCallback(async () => {
    try {
      await apiLogout();
    } finally {
      if (user) {
        try {
          window.localStorage.removeItem(`${STORAGE_PREFIX}.${user.id}`);
        } catch {
          // 隐私模式下无法清理，不影响内存状态。
        }
      }
      setUser(null);
      setPhase("anonymous");
      window.history.replaceState(null, "", "/login");
    }
  }, [user]);

  const retry = useCallback(() => setAttempt((value) => value + 1), []);

  const value = useMemo(
    () => ({ phase, user, error, login, register, logout, retry }),
    [phase, user, error, login, register, logout, retry],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const value = useContext(AuthContext);
  if (value === null) {
    throw new Error("useAuth 必须在 AuthProvider 内使用");
  }
  return value;
}