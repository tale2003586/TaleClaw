import { lazy, Suspense, useEffect, useState, type ComponentType, type LazyExoticComponent } from "react";
import { Activity, BarChart3, Bot, Brain, Folder, Gauge, List, LogOut, Menu, MessageCircle, PanelLeftClose, PanelLeftOpen, Plus, Settings, Trash2 } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { postJson } from "../api/client";
import { useAppContext, useSessionsContext } from "./contexts";
import { useAppView } from "../hooks/useAppView";
import { useTheme } from "../hooks/useTheme";
import { RunsProvider } from "../hooks/useRuns";
import { type AppView } from "./routing";
import { IconButton, Skeleton } from "../components/ui";
import { ThemeToggle } from "../components/ui/ThemeToggle";
import { PageErrorBoundary } from "../components/ui/PageErrorBoundary";

const pages: Record<AppView, LazyExoticComponent<ComponentType>> = {
  chat: lazy(() => import("../pages/ChatPage")), logs: lazy(() => import("../pages/LogsPage")),
  runs: lazy(() => import("../pages/RunsPage")), files: lazy(() => import("../pages/FilesPage")),
  analysis: lazy(() => import("../pages/AnalysisPage")), memory: lazy(() => import("../pages/MemoryPage")),
  status: lazy(() => import("../pages/StatusPage")), settings: lazy(() => import("../pages/SettingsPage")),
};

const navigation: Array<{ view: AppView; label: string; icon: LucideIcon; adminOnly?: boolean }> = [
  { view: "chat", label: "聊天", icon: MessageCircle },
  { view: "logs", label: "日志", icon: List, adminOnly: true },
  { view: "runs", label: "Runs", icon: Activity, adminOnly: true },
  { view: "files", label: "文件", icon: Folder },
  { view: "analysis", label: "分析", icon: BarChart3 },
  { view: "memory", label: "记忆", icon: Brain },
  { view: "status", label: "状态", icon: Gauge },
  { view: "settings", label: "设置", icon: Settings },
];

export function AppShell() {
  const { user } = useAppContext(); const sessions = useSessionsContext();
  const { view, navigate } = useAppView(user.role);
  const theme = useTheme();
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("sidebarCollapsed") === "1");
  const [drawer, setDrawer] = useState(false);
  useEffect(() => { const close = (event: KeyboardEvent) => { if (event.key === "Escape") setDrawer(false); }; window.addEventListener("keydown", close); return () => window.removeEventListener("keydown", close); }, []);
  const toggleCollapsed = () => setCollapsed((value) => { localStorage.setItem("sidebarCollapsed", value ? "0" : "1"); return !value; });
  const Page = pages[view];
  const logout = async () => { try { await postJson("/api/auth/logout", {}); } finally { window.location.assign("/login"); } };
  return <RunsProvider enabled={user.role === "admin"}>
    <main className={`app-shell ${collapsed ? "is-collapsed" : ""} ${drawer ? "is-drawer-open" : ""}`}>
      <aside className="sidebar">
        <header className="brand"><span className="brand-mark"><Bot aria-hidden="true" /></span><div><strong>taleclaw</strong><small>{user.id} · {user.role}</small></div><IconButton className="collapse-button" icon={collapsed ? PanelLeftOpen : PanelLeftClose} label={collapsed ? "展开侧栏" : "折叠侧栏"} onClick={toggleCollapsed} /></header>
        <nav className="primary-nav" aria-label="主导航">{navigation.map(({ view: item, label, icon: Icon, adminOnly }) => {
          if (adminOnly && user.role !== "admin") return null;
          return <button key={item} className={view === item ? "active" : ""} onClick={() => { navigate(item); setDrawer(false); }} aria-current={view === item ? "page" : undefined}><Icon aria-hidden="true" /><strong>{label}</strong></button>;
        })}</nav>
        <section className="conversation-panel">
          <header><span>会话</span><IconButton icon={Plus} label="新建会话" size="sm" onClick={() => { sessions.newSession(); navigate("chat"); }} /></header>
          <input value={sessions.filter} onChange={(event) => sessions.setFilter(event.target.value)} placeholder="搜索会话" aria-label="搜索会话" />
          <div className="conversation-list">{sessions.status === "loading" && <Skeleton lines={3} />}{sessions.visible.map((session) => {
            const id = session.channel === "web" ? String(session.chat_id || "") : String(session.id || "");
            return <div className={`conversation-item ${sessions.activeId === id ? "active" : ""}`} key={id}><button onClick={() => { void sessions.loadSession(id, session.channel !== "web"); navigate("chat"); }}><strong>{session.title || id}</strong><small>{session.current_mode || "hybrid"} · {formatDate(session.updated_at)}</small></button>{session.channel === "web" && <IconButton className="delete-session" icon={Trash2} size="sm" label={`删除 ${id}`} onClick={() => { if (window.confirm(`删除会话 ${id}？`)) void sessions.removeSession(id); }} />}</div>;
          })}{sessions.status === "success" && sessions.visible.length === 0 && <p className="muted-center">暂无会话</p>}</div>
        </section>
        <footer className="account-footer"><div><span>当前账号</span><strong>{user.id}</strong></div><div className="account-tools"><ThemeToggle {...theme} /><IconButton icon={LogOut} label="退出登录" onClick={logout} /></div></footer>
      </aside>
      <button className="drawer-backdrop" aria-label="关闭侧栏" onClick={() => setDrawer(false)} />
      <section className="workspace"><IconButton className="mobile-menu" icon={Menu} label="打开侧栏" onClick={() => setDrawer(true)} /><div className="view-transition" key={view}><PageErrorBoundary resetKey={view}><Suspense fallback={<div className="view-skeleton"><Skeleton lines={8} /></div>}><Page /></Suspense></PageErrorBoundary></div></section>
    </main>
  </RunsProvider>;
}

function formatDate(value?: string) {
  if (!value) return "未保存"; const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
