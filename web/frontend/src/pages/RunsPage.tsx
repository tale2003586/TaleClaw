import { useEffect, useMemo, useState, type ReactNode } from "react";
import { RefreshCw } from "lucide-react";
import { displayValue, groupTrace } from "../api/adapters";
import { useRuns } from "../hooks/useRuns";
import { Badge, Button, EmptyState, ErrorState, JsonDisclosure, PageHeader, Skeleton } from "../components/ui";
import { formatTime, TraceEventView } from "../components/trace/TraceEventView";

export default function RunsPage() {
  const { runs, activeRunId, activeDetail, loadRuns, selectRun } = useRuns(); const [status, setStatus] = useState("all");
  useEffect(() => { void loadRuns().then((items) => { const id = activeRunId || items[0]?.runId; if (id) void selectRun(id); }).catch(() => undefined); }, [activeRunId, loadRuns, selectRun]);
  const refresh = async () => {
    const items = await loadRuns(true);
    const id = items.some((run) => run.runId === activeRunId) ? activeRunId : items[0]?.runId;
    if (id) await selectRun(id, true);
  };
  const visible = useMemo(() => (runs.data || []).filter((run) => status === "all" || run.status === status), [runs.data, status]);
  const detail = activeDetail.data; const grouped = useMemo(() => detail ? groupTrace(detail) : { steps: [], runEvents: [] }, [detail]);
  const state = detail?.runState || {}; const metrics = detail?.metrics || {};
  const request = detail?.events.find((event) => event.event === "inbound_received")?.payload.content_preview;
  const metricEntries: Array<[string, unknown]> = [["步骤", state.reasoning_steps ?? metrics.reasoning_steps], ["模型调用", metrics.model_calls], ["工具调用", metrics.tool_calls], ["Tokens", metrics.total_tokens], ["耗时", formatDuration(metrics.run_duration_ms)], ["Subagents", detail?.subagents.length || 0]];
  return <div className="page runs-page"><PageHeader eyebrow="Agent Runtime" title="Runs / Trace" description="复盘执行步骤、模型调用、工具活动与停止原因。" action={<><select aria-label="状态筛选" value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">全部状态</option><option value="completed">Completed</option><option value="running">Running</option><option value="stopped">Stopped</option><option value="failed">Failed</option></select><Button className="primary" onClick={() => void refresh()}><RefreshCw aria-hidden="true" size={14} />刷新 Runs</Button></>} />
    <div className="runs-layout"><aside className="run-index"><header><span>最近执行</span><strong>{runs.data?.length || 0}</strong></header>{runs.status === "loading" && <Skeleton lines={5} />}{runs.status === "error" && <ErrorState message={runs.error} retry={() => void loadRuns(true)} />}{runs.status === "success" && visible.length === 0 && <p className="run-index-empty">{(runs.data?.length || 0) === 0 ? "暂无 Run" : "没有匹配状态"}</p>}{visible.map((run) => <button key={run.runId} className={run.runId === activeRunId ? "active" : ""} onClick={() => void selectRun(run.runId)}><div><strong>{run.runId}</strong><Badge tone={tone(run.status)}>{run.status}</Badge></div><span>{run.mode} · {formatTime(run.startedAt)}</span><small>{run.reasoningSteps} steps · {run.modelCalls} models · {run.toolCalls} tools</small></button>)}</aside>
      <article className="run-detail">{runs.status === "success" && (runs.data?.length || 0) === 0 && <EmptyState title="还没有 Run" message="发起一次聊天任务后，可在这里查看完整执行时间线。" />}{activeDetail.status === "loading" && <Skeleton lines={10} />}{activeDetail.status === "error" && <ErrorState message={activeDetail.error} retry={() => activeRunId ? void selectRun(activeRunId, true) : undefined} />}{activeDetail.status === "success" && detail && <><header className="run-detail-header"><div><h3>{detail.runId}</h3><p>{displayValue(state.mode, "-")} · {formatTime(String(state.started_at || ""))}</p></div><Badge tone={tone(String(state.status || "unknown"))}>{displayValue(state.status, "unknown")}</Badge></header><div className="trace-metrics">{metricEntries.map(([label, value]) => <div key={label}><span>{label}</span><strong>{displayValue(value)}</strong></div>)}</div>
        <TraceSection title="用户请求"><p className="request-preview">{displayValue(request, "未记录请求摘要")}</p></TraceSection>
        <TraceSection title="执行时间线"><div className="trace-timeline">{grouped.steps.length === 0 && <EmptyState title="没有 Step 事件" message="独立 Run 事件仍在下方保留。" />}{grouped.steps.map((step) => <section className="trace-step" key={step.number}><span className="step-marker">{step.number}</span><div className="step-card"><h5>Step {step.number} · Context / Model / Tools</h5>{step.events.map((event, index) => <TraceEventView event={event} key={`${event.event}-${index}`} />)}</div></section>)}</div></TraceSection>
        <TraceSection title="Subagents">{detail.subagents.length === 0 ? <EmptyState title="没有 Subagent" message="此 Run 未记录子任务。" /> : detail.subagents.map((item) => <details className="subagent-card" key={item.sessionId}><summary><strong>{item.description || item.agentType || item.sessionId}</strong><Badge tone={item.success ? "success" : item.success === false ? "error" : "warn"}>{item.success ? "success" : item.success === false ? "failed" : "incomplete"}</Badge></summary><pre>{JSON.stringify(item.raw, null, 2)}</pre></details>)}</TraceSection>
        <TraceSection title="Run 事件"><div className="trace-event-list">{grouped.runEvents.map((event, index) => <TraceEventView event={event} key={`${event.event}-${index}`} />)}</div></TraceSection>
        <TraceSection title="Run State"><JsonDisclosure label="查看完整状态 JSON" value={state} /></TraceSection>
        <TraceSection title="最终结果"><pre className="final-answer">{displayValue(state.final_answer || state.error || state.stop_reason)}</pre></TraceSection></>}</article>
    </div></div>;
}

function TraceSection({ title, children }: { title: string; children: ReactNode }) { return <section className="trace-section"><h4>{title}</h4>{children}</section>; }
function tone(status: string) { const clean = status.toLowerCase(); return ["completed", "success", "ready"].includes(clean) ? "success" : ["failed", "error"].includes(clean) ? "error" : ["stopped", "incomplete"].includes(clean) ? "warn" : clean === "running" ? "info" : "neutral"; }
function formatDuration(value: unknown) { const number = Number(value); if (!Number.isFinite(number)) return displayValue(value); return number < 1000 ? `${Math.round(number)}ms` : `${(number / 1000).toFixed(1)}s`; }
