import { useCallback, useEffect, useRef, useState, type KeyboardEvent } from "react";
import hhuEmblem from "./assets/hhu-emblem.png";
import ConversationWorkspace from "./components/ConversationWorkspace";
import LearningMemory from "./components/LearningMemory";
import MaterialWorkspace, { type MaterialStudyTarget } from "./components/MaterialWorkspace";
import MistakeBook from "./components/MistakeBook";
import PaperCard from "./components/PaperCard";
import PaperFilters from "./components/PaperFilters";
import AuthPage, { type AuthMode } from "./auth/AuthPage";
import { AuthProvider, storageKeyFor, useAuth } from "./auth/AuthContext";
import UserMenu from "./auth/UserMenu";
import ContributeModal from "./components/ContributeModal";
import { fetchMemoryCourses } from "./lib/memoryApi";
import { listMistakeRecords } from "./lib/mistakesApi";
import { fetchHealth, HealthCheckError } from "./lib/healthApi";
import {
  downloadPaper,
  fetchPapers,
  PapersApiError,
  type ExamTypeFilter,
  type PaperItem,
} from "./lib/papersApi";
import type { DownloadState, DownloadStates } from "./lib/display";

type View = "conversations" | "materials" | "memory" | "mistakes" | "papers";
type PrivateView = Exclude<View, "papers">;

const GUEST_ENTRY_KEY = "hhu-exam-review.guest-entry";
const PRIVATE_VIEW_PROMPTS: Record<PrivateView, string> = {
  conversations: "登录后新建学习会话、上传题目并保存讲解。",
  materials: "登录后上传个人课程 PDF，生成带页码引用的章节复习清单。",
  memory: "登录后查看你的学习记忆和复习建议。",
  mistakes: "登录后查看你的错题本。",
};

const PAPERS_PAGE_SIZE = 30;

const VIEWS = [
  { key: "conversations", label: "学习会话", mobileBar: true },
  { key: "materials", label: "个人资料", mobileBar: false },
  { key: "papers", label: "试卷库", mobileBar: true },
  { key: "memory", label: "学习记忆", mobileBar: true },
  { key: "mistakes", label: "错题本", mobileBar: true },
] as const;

const ICON_PATHS = {
  conversations: "M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z",
  materials: "M4 19.5A2.5 2.5 0 0 1 6.5 17H20 M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z",
  papers: "M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z M14 2v6h6",
  memory: "M2 3h6a4 4 0 0 1 4 4v14a3 3 0 0 0-3-3H2z M22 3h-6a4 4 0 0 0-4 4v14a3 3 0 0 1 3-3h7z",
  mistakes: "M12 20h9 M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z",
  account: "M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2 M16 7a4 4 0 1 1-8 0 4 4 0 0 1 8 0z",
  menu: "M3 6h18M3 12h18M3 18h18",
} as const;

function NavIcon({ name }: { name: keyof typeof ICON_PATHS }) {
  return (
    <svg aria-hidden="true" className="nav-icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round">
      <path d={ICON_PATHS[name]} />
    </svg>
  );
}


type PapersState =
  | { phase: "loading" }
  | { phase: "ready"; items: PaperItem[]; total: number }
  | { phase: "error"; message: string };

type HealthState =
  | { phase: "checking" }
  | { phase: "ok" }
  | { phase: "error"; message: string };

type SubmittedQuery = {
  course: string;
  examType: ExamTypeFilter;
  seq: number;
};

const INITIAL_QUERY: SubmittedQuery = { course: "", examType: "all", seq: 0 };

export default function App() {
  return (
    <AuthProvider>
      <AppRoot />
    </AuthProvider>
  );
}

function AppRoot() {
  const { user } = useAuth();
  return <AppShell key={user?.id ?? "guest"} />;
}

function pathAuthMode(): AuthMode | null {
  if (window.location.pathname === "/register") {
    return "register";
  }
  if (window.location.pathname === "/login") {
    return "login";
  }
  return null;
}

function readAuthTarget(): PrivateView | null {
  const target: unknown = window.history.state?.hhuAuthTarget;
  return target === "conversations" || target === "materials" || target === "memory" || target === "mistakes" ? target : null;
}

function SiteFilingLink() {
  return (
    <a className="site-filing-link" href="https://beian.miit.gov.cn/" target="_blank" rel="noreferrer">
      湘ICP备2026055716号-1
    </a>
  );
}

function AppShell() {
  const { phase, user, error: authError, retry } = useAuth();
  const [view, setView] = useState<View>(() => {
    if (!user) return "papers";
    return (pathAuthMode() && readAuthTarget()) || "conversations";
  });
  const [authMode, setAuthMode] = useState<AuthMode | null>(pathAuthMode);
  const [guestEntry, setGuestEntry] = useState(() => {
    try {
      return sessionStorage.getItem(GUEST_ENTRY_KEY) === "yes";
    } catch {
      return false;
    }
  });
  const [guestTarget, setGuestTarget] = useState<PrivateView | null>(null);
  const [prefillDraft, setPrefillDraft] = useState<{
    conversationId: string;
    text: string;
    materialGuideId?: string;
    materialItemId?: string;
    materialArtifactId?: string;
    materialQuestionId?: string;
    materialKcId?: string;
    materialStudyMode?: "explain" | "exercise" | "evaluate";
  } | null>(null);
  const guestPromptRef = useRef<HTMLElement>(null);
  const paperTitleRef = useRef<HTMLHeadingElement>(null);
  const papersScrollRef = useRef<HTMLDivElement>(null);
  const [visiblePaperCount, setVisiblePaperCount] = useState(PAPERS_PAGE_SIZE);
  const focusArchive = useRef(false);
  const [drawerOpen, setDrawerOpen] = useState(false);
  const drawerTrigger = useRef<HTMLButtonElement | null>(null);
  const drawerClose = useRef<HTMLButtonElement>(null);
  const [historyHost, setHistoryHost] = useState<HTMLDivElement | null>(null);
  const [createHost, setCreateHost] = useState<HTMLDivElement | null>(null);
  const [usageHost, setUsageHost] = useState<HTMLDivElement | null>(null);
  const [counts, setCounts] = useState<Partial<Record<View, number>>>({});
  const [learningRevision, setLearningRevision] = useState(0);
  const [swiss, setSwiss] = useState(() => {
    try {
      return localStorage.getItem("hhu-exam-review.theme") === "swiss";
    } catch {
      return false;
    }
  });
  const [selectedRecordId, setSelectedRecordId] = useState<string | null>(null);
  const [openConversationId, setOpenConversationId] = useState<string | null>(
    null,
  );
  const [contributeOpen, setContributeOpen] = useState(false);

  const openAuth = useCallback((mode: AuthMode, nextView: PrivateView | null = null) => {
    setAuthMode(mode);
    setDrawerOpen(false);
    window.history.pushState(
      nextView ? { hhuAuthTarget: nextView } : null,
      "",
      mode === "register" ? "/register" : "/login",
    );
  }, []);

  const continueAsGuest = () => {
    try {
      sessionStorage.setItem(GUEST_ENTRY_KEY, "yes");
    } catch {
      // 浏览器禁用会话存储时，仍可在当前页面浏览试卷。
    }
    focusArchive.current = true;
    setGuestEntry(true);
    setGuestTarget(null);
    setAuthMode(null);
    setView("papers");
    window.history.replaceState(null, "", "/");
  };

  useEffect(() => {
    const onPop = () => setAuthMode(pathAuthMode());
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  useEffect(() => {
    if (phase === "authenticated" && authMode) {
      // 账号切换会重挂 AppShell。新工作区先从登录历史读取目标，再清理 URL。
      window.history.replaceState(null, "", "/");
      setAuthMode(null);
    }
  }, [authMode, phase]);

  useEffect(() => {
    if (focusArchive.current && paperTitleRef.current) {
      focusArchive.current = false;
      paperTitleRef.current.focus();
    }
  }, [authMode, guestEntry, view]);

  const showDrawer = (trigger: HTMLButtonElement) => {
    drawerTrigger.current = trigger;
    setDrawerOpen(true);
  };

  const closeDrawer = useCallback(() => {
    setDrawerOpen(false);
  }, []);

  useEffect(() => {
    if (!drawerOpen) {
      drawerTrigger.current?.focus();
      drawerTrigger.current = null;
      return;
    }
    drawerClose.current?.focus();
    const desktop = window.matchMedia?.("(min-width: 860px)");
    const resize = () => {
      if (desktop?.matches) setDrawerOpen(false);
    };
    desktop?.addEventListener("change", resize);
    return () => desktop?.removeEventListener("change", resize);
  }, [drawerOpen]);

  const handleDrawerKeys = (event: KeyboardEvent<HTMLElement>) => {
    if (!drawerOpen) return;
    if (event.key === "Escape") {
      event.preventDefault();
      closeDrawer();
    } else if (event.key === "Tab") {
      const focusable = event.currentTarget.querySelectorAll<HTMLElement>("button:not(:disabled), a[href]");
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last?.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first?.focus();
      }
    }
  };

  const toggleTheme = () => {
    setSwiss(!swiss);
    try {
      localStorage.setItem("hhu-exam-review.theme", swiss ? "archival" : "swiss");
    } catch {
      // 浏览器禁用本地存储时，当前页面内仍可切换主题。
    }
  };

  const handleNav = useCallback(
    (key: View) => {
      closeDrawer();
      if (key !== "papers" && !user) {
        setGuestTarget(key);
        return;
      }
      setGuestTarget(null);
      if (key === "mistakes") {
        setSelectedRecordId(null);
      }
      setView(key);
    },
    [closeDrawer, user],
  );

  useEffect(() => {
    if (guestTarget && !authMode) guestPromptRef.current?.focus();
  }, [authMode, guestTarget]);

  const [courseInput, setCourseInput] = useState("");
  const [examTypeInput, setExamTypeInput] = useState<ExamTypeFilter>("all");
  const [submitted, setSubmitted] = useState<SubmittedQuery>(INITIAL_QUERY);

  const [papersState, setPapersState] = useState<PapersState>({
    phase: "loading",
  });
  const [catalogCourses, setCatalogCourses] = useState<[string, number][]>([]);
  const [downloads, setDownloads] = useState<DownloadStates>({});
  const downloadTimersRef = useRef<Record<string, number>>({});
  const downloadInFlightRef = useRef<Set<string>>(new Set());

  const [healthState, setHealthState] = useState<HealthState>({
    phase: "checking",
  });
  const [healthAttempt, setHealthAttempt] = useState(0);

  const setNavCount = useCallback((key: View, count: number) => {
    setCounts((current) => current[key] === count ? current : { ...current, [key]: count });
  }, []);
  const reportConversations = useCallback((count: number) => setNavCount("conversations", count), [setNavCount]);
  const reportMemory = useCallback((count: number) => setNavCount("memory", count), [setNavCount]);
  const reportMistakes = useCallback((count: number) => setNavCount("mistakes", count), [setNavCount]);
  const refreshLearningCounts = useCallback(() => setLearningRevision((value) => value + 1), []);

  useEffect(() => {
    if (!user) return;
    const controller = new AbortController();
    fetchMemoryCourses(controller.signal)
      .then((payload) => {
        if (!controller.signal.aborted) reportMemory(payload.items.reduce((total, item) => total + item.evidence, 0) + payload.pending);
      })
      .catch(() => {});
    listMistakeRecords({ limit: 1, offset: 0 }, controller.signal)
      .then((payload) => {
        if (!controller.signal.aborted) reportMistakes(payload.total);
      })
      .catch(() => {});
    return () => controller.abort();
  }, [user, learningRevision, reportMemory, reportMistakes]);

  useEffect(() => {
    const controller = new AbortController();
    setPapersState({ phase: "loading" });

    fetchPapers(
      { course: submitted.course, examType: submitted.examType },
      controller.signal,
    )
      .then((payload) => {
        if (controller.signal.aborted) return;
        if (!submitted.course && submitted.examType === "all") {
          const courses = new Map<string, number>();
          for (const paper of payload.items) courses.set(paper.course, (courses.get(paper.course) ?? 0) + 1);
          setCatalogCourses([...courses].sort((a, b) => b[1] - a[1]));
          setNavCount("papers", payload.total);
        }
        setPapersState({
          phase: "ready",
          items: payload.items,
          total: payload.total,
        });
      })
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }
        if (error instanceof DOMException && error.name === "AbortError") {
          return;
        }

        setPapersState({
          phase: "error",
          message:
            error instanceof PapersApiError
              ? error.message
              : "查询试卷时发生未知错误，请重试。",
        });
      });

    return () => controller.abort();
  }, [submitted, setNavCount]);

  useEffect(() => {
    const controller = new AbortController();
    setHealthState({ phase: "checking" });

    fetchHealth(controller.signal)
      .then(() => setHealthState({ phase: "ok" }))
      .catch((error: unknown) => {
        if (controller.signal.aborted) {
          return;
        }

        setHealthState({
          phase: "error",
          message:
            error instanceof HealthCheckError
              ? error.message
              : "检查时发生未知错误，请重试。",
        });
      });

    return () => controller.abort();
  }, [healthAttempt]);

  const submitQuery = useCallback(() => {
    setVisiblePaperCount(PAPERS_PAGE_SIZE);
    setSubmitted((current) => ({
      course: courseInput.trim(),
      examType: examTypeInput,
      seq: current.seq + 1,
    }));
  }, [courseInput, examTypeInput]);

  const selectCatalogCourse = (course: string) => {
    setCourseInput(course);
    setVisiblePaperCount(PAPERS_PAGE_SIZE);
    setSubmitted((current) => ({ course, examType: examTypeInput, seq: current.seq + 1 }));
  };

  const retryQuery = useCallback(() => {
    setSubmitted((current) => ({ ...current, seq: current.seq + 1 }));
  }, []);

  const setDownloadState = useCallback(
    (id: string, state: DownloadState | null) => {
      setDownloads((current) => {
        const next = { ...current };
        if (state === null) {
          delete next[id];
        } else {
          next[id] = state;
        }
        return next;
      });
    },
    [],
  );

  const handleDownload = useCallback(
    async (paper: PaperItem) => {
      // 同一份文件正在请求时忽略重复点击；不靠按钮禁用兜底。
      if (downloadInFlightRef.current.has(paper.id)) {
        return;
      }
      downloadInFlightRef.current.add(paper.id);
      window.clearTimeout(downloadTimersRef.current[paper.id]);
      setDownloadState(paper.id, { phase: "downloading" });

      try {
        await downloadPaper(paper);
        setDownloadState(paper.id, {
          phase: "triggered",
          message: "已开始下载，请在浏览器下载栏确认是否保存成功。",
        });
        downloadTimersRef.current[paper.id] = window.setTimeout(() => {
          setDownloadState(paper.id, null);
        }, 10_000);
      } catch (error: unknown) {
        setDownloadState(paper.id, {
          phase: "error",
          message:
            error instanceof PapersApiError
              ? error.message
              : "下载时发生未知错误，请重试。",
        });
      } finally {
        downloadInFlightRef.current.delete(paper.id);
      }
    },
    [setDownloadState],
  );

  useEffect(
    () => () => {
      Object.values(downloadTimersRef.current).forEach((timer) =>
        window.clearTimeout(timer),
      );
    },
    [],
  );

  const openRecord = useCallback((recordId: string) => {
    setSelectedRecordId(recordId);
    setView("mistakes");
  }, []);

  const openConversationFromMemory = useCallback((conversationId: string) => {
    setOpenConversationId(conversationId);
    setView("conversations");
  }, []);

  const consumeConversationRequest = useCallback((id: string) => {
    setOpenConversationId((current) => (current === id ? null : current));
  }, []);

  const theme = swiss ? "swiss" : "archival";
  const firstVisit = !user && phase === "anonymous" && !guestEntry && authMode === null;

  if (phase === "checking" && !user) {
    return (
      <main className="entry-check" aria-busy="true">
        <p role="status">正在确认登录状态…</p>
      </main>
    );
  }

  if (!user && (authMode || firstVisit)) {
    const target = authMode ? readAuthTarget() : null;
    return (
      <main className="entry-screen" data-theme={theme}>
        <AuthPage
          mode={authMode ?? "login"}
          startWithCover={firstVisit}
          reason={target ? PRIVATE_VIEW_PROMPTS[target] : undefined}
          onGuest={continueAsGuest}
          onSwitch={(mode) => {
            setAuthMode(mode);
            window.history.replaceState(window.history.state, "", mode === "register" ? "/register" : "/login");
          }}
        />
        <footer className="entry-filing"><SiteFilingLink /></footer>
      </main>
    );
  }

  const isLoading = papersState.phase === "loading";
  const currentView = view;
  const healthLabel = { checking: "检查服务…", ok: "服务正常", error: "服务异常" }[healthState.phase];

  return (
    <div className="workbench" data-theme={theme}>
      <header className="mobile-topbar" inert={drawerOpen}>
        <button type="button" className="icon-button" aria-label="打开会话与额度抽屉" aria-expanded={drawerOpen} aria-controls="workspace-rail" onClick={(event) => showDrawer(event.currentTarget)}>
          <NavIcon name="menu" />
        </button>
        <p>{healthState.phase === "error" && <span className="health-dot" data-phase={healthState.phase} aria-hidden="true" />}河海考试复习 · {VIEWS.find(({ key }) => key === currentView)?.label}</p>
      </header>

      {drawerOpen && <div className="drawer-mask" aria-hidden="true" onClick={closeDrawer} />}
      <aside
        id="workspace-rail"
        className="workspace-rail"
        data-open={drawerOpen}
        aria-label="学习工作台侧栏"
        role={drawerOpen ? "dialog" : undefined}
        aria-modal={drawerOpen || undefined}
        onKeyDown={handleDrawerKeys}
      >
        <div className="rail-brand">
          <img className="brand-emblem" src={hhuEmblem} alt="河海大学校徽" />
          <div>
            <p className="brand-title">河海考试复习</p>
            <p className="brand-subtitle">HHU EXAM AGENT</p>
          </div>
          {healthState.phase === "error" && (
            <span className="health-dot rail-health" data-phase={healthState.phase} title={healthLabel} aria-label={healthLabel} />
          )}
          <button ref={drawerClose} type="button" className="drawer-close icon-button" aria-label="关闭抽屉" onClick={closeDrawer}>×</button>
        </div>

        {user && <div ref={setCreateHost} />}
        {!user && (
          <button type="button" className="rail-create" aria-describedby="guest-access-note" onClick={() => handleNav("conversations")}>
            <span aria-hidden="true">＋ </span>新建学习会话
            <span className="guest-required" aria-hidden="true">需登录</span>
          </button>
        )}
        {!user && <span id="guest-access-note" className="sr-only">此功能需要登录</span>}
        <nav className="rail-nav" aria-label="主导航">
          {VIEWS.map(({ key, label }) => (
            <button
              key={key}
              type="button"
              onClick={() => handleNav(key)}
              aria-current={currentView === key ? "page" : undefined}
              aria-describedby={!user && key !== "papers" ? "guest-access-note" : undefined}
            >
              <NavIcon name={key} />
              <span>{label}</span>
              {!user && key !== "papers" && <span className="guest-required" aria-hidden="true">需登录</span>}
              {counts[key] !== undefined && <span className="rail-count" aria-hidden="true">{counts[key]}</span>}
            </button>
          ))}
        </nav>

        <div className="rail-history" ref={setHistoryHost} />
        <div className="rail-footer">
          {user && <div ref={setUsageHost} />}
          {user && (
            <button
              type="button"
              className="quiet-button w-full justify-center"
              onClick={() => {
                closeDrawer();
                setContributeOpen(true);
              }}
            >
              自愿贡献资料
            </button>
          )}
          {healthState.phase === "error" && (
            <div className="rail-service-error">
              <span title={healthState.message}>服务异常</span>
              <button type="button" className="quiet-button" onClick={() => setHealthAttempt((value) => value + 1)}>重试</button>
            </div>
          )}
          <button
            type="button"
            className="theme-switch"
            aria-pressed={swiss}
            title="点击在学术典藏与瑞士极简主题之间切换"
            onClick={toggleTheme}
          >
            <span>显示主题</span>
            <span className="theme-switch-pill">
              {swiss ? "瑞士极简" : "学术典藏"}
              <span className="theme-switch-arrow" aria-hidden="true">⇄</span>
            </span>
          </button>
          <UserMenu onLogin={() => openAuth("login")} onRegister={() => openAuth("register")} />
          <SiteFilingLink />
        </div>
      </aside>

      <main className="main-stage" inert={drawerOpen}>
        {user && (
          <div className={currentView === "conversations" && phase !== "error" ? "h-full" : "hidden"} hidden={currentView !== "conversations" || phase === "error"}>
            <ConversationWorkspace
              downloads={downloads}
              onDownload={handleDownload}
              onOpenRecord={openRecord}
              openConversationId={openConversationId}
              onConversationOpened={consumeConversationRequest}
              storageKey={storageKeyFor(user.id)}
              rail={{ history: historyHost, create: createHost, usage: usageHost }}
              onNavigateToConversation={() => handleNav("conversations")}
              onCountChange={reportConversations}
              onLearningChanged={refreshLearningCounts}
              prefillDraft={prefillDraft}
              onPrefillConsumed={() => setPrefillDraft(null)}
            />
          </div>
        )}

        {phase === "error" ? (
          <div className="page-frame">
            <div className="inline-alert">
              <p className="font-semibold">无法确认登录状态</p>
              <p>{authError ?? "请稍后再试。"}</p>
              <button type="button" className="quiet-button" onClick={retry}>重试</button>
            </div>
          </div>
        ) : (
          <>
            {view === "materials" && user && (
              <div className="stage-scroll">
                <MaterialWorkspace
                  onStartStudy={(conversationId, prompt, target: MaterialStudyTarget) => {
                    setPrefillDraft({
                      conversationId,
                      text: prompt,
                      materialGuideId: target.guideId,
                      materialItemId: target.itemId,
                      materialArtifactId: target.artifactId,
                      materialQuestionId: target.questionId,
                      materialKcId: target.kcId,
                      materialStudyMode: target.mode,
                    });
                    openConversationFromMemory(conversationId);
                  }}
                />
              </div>
            )}
            {view === "memory" && user && (
              <div className="stage-scroll">
                <LearningMemory onOpenConversation={openConversationFromMemory} onCountChange={reportMemory} />
              </div>
            )}
            {view === "mistakes" && user && (
              <div className="stage-scroll">
                <MistakeBook
                  selectedId={selectedRecordId}
                  onSelectRecord={setSelectedRecordId}
                  onCountChange={reportMistakes}
                  onOpenConversation={openConversationFromMemory}
                />
              </div>
            )}
            {view === "papers" && (
              <div className="stage-scroll" ref={papersScrollRef}>
                <div className="page-frame archive-page">
                  <header className="page-heading">
                    <p className="eyebrow">历年真题馆藏 · 公开检索</p>
                    <h1 ref={paperTitleRef} tabIndex={-1}>河海大学历年试卷馆藏目录</h1>
                    <p>按课程和考试类型检索，下载馆藏原卷 PDF。</p>
                  </header>

                  {!user && guestTarget && (
                    <section
                      ref={guestPromptRef}
                      tabIndex={-1}
                      aria-labelledby="guest-prompt-heading"
                      className="guest-prompt"
                    >
                      <div>
                        <h2 id="guest-prompt-heading">{PRIVATE_VIEW_PROMPTS[guestTarget]}</h2>
                        <p>你仍可用访客身份查找和下载试卷。</p>
                      </div>
                      <div className="guest-prompt-actions">
                        <button type="button" className="primary-button" onClick={() => openAuth("login", guestTarget)}>登录后继续</button>
                        <button type="button" className="quiet-button" onClick={() => {
                          setGuestTarget(null);
                          paperTitleRef.current?.focus();
                        }}>继续浏览试卷库</button>
                      </div>
                    </section>
                  )}

                  <section aria-labelledby="manual-heading">
                    <h2 id="manual-heading" className="sr-only">已登记的试卷</h2>
                    <PaperFilters
                      course={courseInput}
                      examType={examTypeInput}
                      loading={isLoading}
                      onCourseChange={setCourseInput}
                      onExamTypeChange={setExamTypeInput}
                      onSubmit={submitQuery}
                    />
                    {catalogCourses.length > 0 && (
                      <div className="course-pills" aria-label="按馆藏课程筛选">
                        <button type="button" aria-pressed={!submitted.course} onClick={() => selectCatalogCourse("")}>全部课程</button>
                        {catalogCourses.map(([course, count]) => (
                          <button key={course} type="button" aria-pressed={submitted.course === course} onClick={() => selectCatalogCourse(course)}>
                            {course} <span aria-hidden="true">{count}</span>
                          </button>
                        ))}
                      </div>
                    )}

                    <div className="archive-results" aria-live="polite">
                      {papersState.phase === "loading" && <p className="page-message">正在读取试卷目录…</p>}
                      {papersState.phase === "error" && (
                        <div className="inline-alert">
                          <p className="font-semibold">没能读取试卷列表</p>
                          <p>{papersState.message}</p>
                          <button type="button" className="quiet-button" onClick={retryQuery}>重试</button>
                        </div>
                      )}
                      {papersState.phase === "ready" && papersState.total === 0 && (
                        <div className="page-message">
                          <p className="font-semibold">没有符合条件的试卷</p>
                          <p>可以换一个课程关键词，或把考试类型改回“全部类型”。</p>
                        </div>
                      )}
                      {papersState.phase === "ready" && papersState.total > 0 && (
                        <>
                          <div className="archive-result-meta">
                            <h3>共 {papersState.total} 份试卷</h3>
                            <p>按学年、考试日期从近到远排列</p>
                          </div>
                          <div className="paper-index-head" aria-hidden="true">
                            <span>学年</span><span>试卷题名 / 课程</span><span>考试类型</span><span>考试日期</span><span>获取试卷</span>
                          </div>
                          <ul className="paper-index">
                            {papersState.items.slice(0, visiblePaperCount).map((paper) => (
                              <PaperCard key={paper.id} paper={paper} download={downloads[paper.id]} onDownload={handleDownload} />
                            ))}
                          </ul>
                          {(papersState.items.length > visiblePaperCount || visiblePaperCount > PAPERS_PAGE_SIZE) && (
                            <div className="archive-load-more">
                              <span className="archive-load-status">
                                已显示 {Math.min(visiblePaperCount, papersState.items.length)} / {papersState.items.length} 份
                              </span>
                              {papersState.items.length > visiblePaperCount && (
                                <button
                                  type="button"
                                  className="quiet-button"
                                  onClick={() => setVisiblePaperCount((count) => count + PAPERS_PAGE_SIZE)}
                                >
                                  加载更多（剩余 {papersState.items.length - visiblePaperCount} 份）
                                </button>
                              )}
                              {visiblePaperCount > PAPERS_PAGE_SIZE && (
                                <button
                                  type="button"
                                  className="quiet-button"
                                  onClick={() => papersScrollRef.current?.scrollTo({ top: 0, behavior: "smooth" })}
                                >
                                  回到顶部
                                </button>
                              )}
                            </div>
                          )}
                        </>
                      )}
                    </div>
                  </section>
                </div>
              </div>
            )}
          </>
        )}
      </main>

      <nav className="mobile-tabs" aria-label="移动端导航" inert={drawerOpen}>
        {VIEWS.filter(({ mobileBar }) => mobileBar).map(({ key, label }) => (
          <button
            key={key}
            type="button"
            aria-current={currentView === key ? "page" : undefined}
            aria-describedby={!user && key !== "papers" ? "guest-access-note" : undefined}
            onClick={() => handleNav(key)}
          >
            <NavIcon name={key} /><span>{label}</span>
          </button>
        ))}
        <button
          type="button"
          aria-current={currentView === "materials" ? "page" : undefined}
          aria-expanded={user ? drawerOpen : undefined}
          aria-controls={user ? "workspace-rail" : undefined}
          onClick={(event) => user ? showDrawer(event.currentTarget) : openAuth("login")}
        >
          <NavIcon name="account" /><span>我的</span>
        </button>
      </nav>
      {user && contributeOpen && (
        <ContributeModal onClose={() => setContributeOpen(false)} />
      )}
    </div>
  );
}
