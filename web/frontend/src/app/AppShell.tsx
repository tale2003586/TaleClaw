import { lazy, Suspense, useEffect, useRef, useState, type ComponentType, type LazyExoticComponent } from "react";
import { Activity, BarChart3, Bot, Brain, ChevronUp, Folder, Gauge, List, LogOut, Menu, MessageCircle, Moon, PanelLeftClose, PanelLeftOpen, Plus, Settings, Sun, Trash2, UserRound } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { postJson } from "../api/client";
import { useAppContext, useSessionsContext } from "./contexts";
import { useAppView } from "../hooks/useAppView";
import { useTheme } from "../hooks/useTheme";
import { RunsProvider } from "../hooks/useRuns";
import { type AppView } from "./routing";
import { IconButton, Skeleton } from "../components/ui";
import { PageErrorBoundary } from "../components/ui/PageErrorBoundary";

const pages: Record<AppView, LazyExoticComponent<ComponentType>> = {
  chat: lazy(() => import("../pages/ChatPage")), logs: lazy(() => import("../pages/LogsPage")),
  runs: lazy(() => import("../pages/RunsPage")), files: lazy(() => import("../pages/FilesPage")),
  analysis: lazy(() => import("../pages/AnalysisPage")), memory: lazy(() => import("../pages/MemoryPage")),
  status: lazy(() => import("../pages/StatusPage")), settings: lazy(() => import("../pages/SettingsPage")),
};

const primaryNavigation: Array<{ view: AppView; label: string; icon: LucideIcon; adminOnly?: boolean }> = [
  { view: "chat", label: "聊天", icon: MessageCircle },
  { view: "logs", label: "日志", icon: List, adminOnly: true },
  { view: "runs", label: "Runs", icon: Activity, adminOnly: true },
];

const accountNavigation: Array<{ view: AppView; label: string; description: string; icon: LucideIcon }> = [
  { view: "files", label: "文件", description: "工作区文件", icon: Folder },
  { view: "analysis", label: "分析", description: "文本分析工具", icon: BarChart3 },
  { view: "memory", label: "记忆", description: "Agent 记忆", icon: Brain },
  { view: "status", label: "状态", description: "运行时状态", icon: Gauge },
  { view: "settings", label: "设置", description: "界面与工作区", icon: Settings },
];

export function AppShell() {
  const { user } = useAppContext(); const sessions = useSessionsContext();
  const { view, navigate } = useAppView(user.role);
  const theme = useTheme();
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("sidebarCollapsed") === "1");
  const [drawer, setDrawer] = useState(false);
  const [accountMenu, setAccountMenu] = useState(false);
  const accountRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const close = (event: KeyboardEvent) => { if (event.key === "Escape") { setDrawer(false); setAccountMenu(false); } };
    window.addEventListener("keydown", close);
    return () => window.removeEventListener("keydown", close);
  }, []);
  useEffect(() => {
    if (!accountMenu) return;
    const close = (event: PointerEvent) => { if (!accountRef.current?.contains(event.target as Node)) setAccountMenu(false); };
    window.addEventListener("pointerdown", close);
    return () => window.removeEventListener("pointerdown", close);
  }, [accountMenu]);
  const toggleCollapsed = () => setCollapsed((value) => { localStorage.setItem("sidebarCollapsed", value ? "0" : "1"); return !value; });
  const openView = (nextView: AppView) => { navigate(nextView); setDrawer(false); setAccountMenu(false); };
  const Page = pages[view];
  const logout = async () => { try { await postJson("/api/auth/logout", {}); } finally { window.location.assign("/login"); } };
  return <RunsProvider enabled={user.role === "admin"}>
    <main className={`app-shell ${collapsed ? "is-collapsed" : ""} ${drawer ? "is-drawer-open" : ""}`}>
      <aside className="sidebar">
        <header className="brand"><span className="brand-mark"><Bot aria-hidden="true" /></span><div><strong>taleclaw</strong><small>{user.id} · {user.role}</small></div><IconButton className="collapse-button" icon={collapsed ? PanelLeftOpen : PanelLeftClose} label={collapsed ? "展开侧栏" : "折叠侧栏"} onClick={toggleCollapsed} /></header>
        <nav className="primary-nav" aria-label="主导航">{primaryNavigation.map(({ view: item, label, icon: Icon, adminOnly }) => {
          if (adminOnly && user.role !== "admin") return null;
          return <button key={item} className={view === item ? "active" : ""} onClick={() => openView(item)} aria-current={view === item ? "page" : undefined}><Icon aria-hidden="true" /><strong>{label}</strong></button>;
        })}</nav>
        <section className="conversation-panel">
          <header><span>会话</span><IconButton icon={Plus} label="新建会话" size="sm" onClick={() => { sessions.newSession(); openView("chat"); }} /></header>
          <input value={sessions.filter} onChange={(event) => sessions.setFilter(event.target.value)} placeholder="搜索会话" aria-label="搜索会话" />
          <div className="conversation-list" onScroll={(event) => { const node = event.currentTarget; if (node.scrollHeight - node.scrollTop - node.clientHeight < 80) void sessions.loadMoreSessions(); }}>{sessions.status === "loading" && <Skeleton lines={3} />}{sessions.visible.map((session) => {
            const id = session.channel === "web" ? String(session.chat_id || "") : String(session.id || "");
            return <div className={`conversation-item ${sessions.activeId === id ? "active" : ""}`} key={id}><button onClick={() => { void sessions.loadSession(id, session.channel !== "web"); openView("chat"); }}><strong>{session.title || id}</strong><small>{session.current_mode || "hybrid"} · {formatDate(session.updated_at)}</small></button>{session.channel === "web" && <IconButton className="delete-session" icon={Trash2} size="sm" label={`删除 ${id}`} onClick={() => { if (window.confirm(`删除会话 ${id}？`)) void sessions.removeSession(id); }} />}</div>;
          })}{sessions.hasMore && <button className="conversation-load-more" disabled={sessions.loadingMore} onClick={() => void sessions.loadMoreSessions()}>{sessions.loadingMore ? "加载中…" : "加载更早会话"}</button>}{sessions.status === "success" && sessions.visible.length === 0 && <p className="muted-center">暂无会话</p>}</div>
        </section>
        <footer className="account-footer" ref={accountRef}>
          {accountMenu && <div className="account-menu" role="menu" aria-label="账号与更多功能">
            <header><span className="account-avatar"><UserRound aria-hidden="true" /></span><div><strong>{user.id}</strong><small>{user.role === "admin" ? "管理员" : "用户"}</small></div></header>
            <nav aria-label="更多功能">{accountNavigation.map(({ view: item, label, description, icon: Icon }) => <button key={item} role="menuitem" className={view === item ? "active" : ""} onClick={() => openView(item)}><Icon aria-hidden="true" /><span><strong>{label}</strong><small>{description}</small></span></button>)}</nav>
            <div className="account-menu-actions"><button role="menuitem" onClick={theme.toggleTheme}>{theme.theme === "dark" ? <Sun aria-hidden="true" /> : <Moon aria-hidden="true" />}<span><strong>{theme.theme === "dark" ? "切换到亮色" : "切换到暗色"}</strong><small>当前为{theme.theme === "dark" ? "暗色" : "亮色"}主题</small></span></button><button role="menuitem" className="danger" onClick={logout}><LogOut aria-hidden="true" /><span><strong>退出登录</strong><small>结束当前账号会话</small></span></button></div>
          </div>}
          <button className={`account-trigger ${accountNavigation.some((item) => item.view === view) ? "active" : ""}`} aria-haspopup="menu" aria-expanded={accountMenu} onClick={() => setAccountMenu((open) => !open)}><span className="account-avatar"><UserRound aria-hidden="true" /></span><span className="account-copy"><small>当前账号</small><strong>{user.id}</strong></span><ChevronUp className={accountMenu ? "rotated" : ""} aria-hidden="true" /></button>
        </footer>
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
