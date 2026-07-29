import { useCallback, useMemo, useState } from "react";
import { Bot } from "lucide-react";
import { getJson } from "../api/client";
import type { HealthResponse } from "../api/types";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useSessions } from "../hooks/useSessions";
import { AppContextProvider, SessionsContextProvider } from "./contexts";
import { AppShell } from "./AppShell";
import { ErrorState } from "../components/ui";

const WORKSPACE_KEY = "codingWorkspaceRoot";

export function AppBootstrap() {
  const loader = useCallback((signal: AbortSignal) => getJson<HealthResponse>("/api/health", { signal }), []);
  const health = useAsyncResource(loader);
  const sessions = useSessions();
  const [workspaceOverride, setWorkspaceOverride] = useState(() => localStorage.getItem(WORKSPACE_KEY) || "");
  const setCodingWorkspace = useCallback((value: string) => {
    const clean = value.trim(); setWorkspaceOverride(clean);
    if (clean) localStorage.setItem(WORKSPACE_KEY, clean); else localStorage.removeItem(WORKSPACE_KEY);
  }, []);
  const context = useMemo(() => health.data ? {
    user: health.data.user, health: health.data,
    codingWorkspace: workspaceOverride || health.data.coding_workspace || "",
    setCodingWorkspace,
  } : null, [health.data, setCodingWorkspace, workspaceOverride]);
  if (health.status === "error") return <main className="bootstrap-state"><ErrorState message={health.error} retry={() => void health.reload()} /></main>;
  if (!context) return <main className="app-loading"><div className="brand-loader"><Bot aria-hidden="true" /></div><strong>taleclaw</strong><small>正在加载 Agent Runtime…</small></main>;
  return <AppContextProvider value={context}><SessionsContextProvider value={sessions}><AppShell /></SessionsContextProvider></AppContextProvider>;
}
