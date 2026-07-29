import { useCallback, useEffect } from "react";
import { getJson } from "../api/client";
import type { RuntimeHealthResponse } from "../api/types";
import { useAppContext, useSessionsContext } from "../app/contexts";
import { useAsyncResource } from "../hooks/useAsyncResource";
import { useMemory } from "../hooks/useMemory";
import { useRuns } from "../hooks/useRuns";
import { RotateCcw, RefreshCw } from "lucide-react";
import { Button, PageHeader } from "../components/ui";

export default function StatusPage() {
  const app = useAppContext(); const sessions = useSessionsContext(); const memory = useMemory(); const runs = useRuns();
  const { loadRuns } = runs;
  const loader = useCallback((signal: AbortSignal) => getJson<RuntimeHealthResponse>("/api/runtime-health", { signal }), []);
  const runtime = useAsyncResource(loader);
  useEffect(() => { if (app.user.role === "admin") void loadRuns().catch(() => undefined); }, [app.user.role, loadRuns]);
  const status = runtime.status === "loading" ? "检查中" : runtime.status === "error" || runtime.data?.ok === false ? "异常" : runtime.data?.ok ? "就绪" : "未知";
  return <div className="page"><PageHeader eyebrow="Runtime Health" title="系统状态" description="会话、工作区和 Agent Runtime 的当前状态。" action={<Button className="primary" onClick={() => void runtime.reload()}><RefreshCw aria-hidden="true" size={14} />刷新状态</Button>} /><div className="page-body status-body"><section className={`runtime-hero ${status === "异常" ? "error" : ""}`}><span className="runtime-orb" /><div><small>Agent Runtime</small><strong>{status}</strong></div><p>{runtime.error || runtime.data?.error || (status === "就绪" ? "Runtime 已完成初始化并可接受请求。" : "正在检查 Runtime。")}</p></section><div className="status-grid"><StatusCard label="当前会话" value={sessions.activeId} /><StatusCard label="运行模式" value={String(sessions.active?.current_mode || "hybrid")} /><StatusCard label="会话数" value={sessions.data?.length || 0} /><StatusCard label="记忆文件" value={memory.data?.length || 0} /><StatusCard label="当前账号" value={`${app.user.id} · ${app.user.role}`} /><StatusCard label="最近 Runs" value={app.user.role === "admin" ? runs.runs.data?.length || 0 : "无权限"} /></div><div className="status-columns"><section className="card"><span>App Root</span><p>{app.health.workspace}</p></section><section className="card"><span>Coding Workspace</span><input value={app.codingWorkspace} onChange={(event) => app.setCodingWorkspace(event.target.value)} /><Button onClick={() => app.setCodingWorkspace("")}><RotateCcw aria-hidden="true" size={14} />恢复默认</Button></section></div></div></div>;
}

function StatusCard({ label, value }: { label: string; value: string | number }) { return <section className="status-card"><span>{label}</span><strong>{value}</strong></section>; }
