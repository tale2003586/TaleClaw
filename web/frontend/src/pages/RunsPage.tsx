import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Bot, CheckCircle2, CircleDot, RefreshCw, TriangleAlert } from "lucide-react";
import { displayValue, groupTrace } from "../api/adapters";
import type { SubagentSummary, TraceEvent } from "../api/types";
import { useRuns } from "../hooks/useRuns";
import { Badge, Button, EmptyState, ErrorState, JsonDisclosure, PageHeader, Skeleton } from "../components/ui";
import { formatTime, TraceEventView } from "../components/trace/TraceEventView";

export default function RunsPage() {
  const { runs, activeRunId, activeDetail, live, pollIntervalMs, loadRuns, selectRun } = useRuns(); const [status, setStatus] = useState("all");
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
  return <div className="page runs-page"><PageHeader eyebrow="Agent Runtime" title="Runs / Trace" description="复盘执行步骤、模型调用、工具活动与停止原因。" action={<>{live && <span className="live-refresh" role="status"><span aria-hidden="true" />实时更新 · {pollIntervalMs / 1000}s</span>}<select aria-label="状态筛选" value={status} onChange={(event) => setStatus(event.target.value)}><option value="all">全部状态</option><option value="completed">Completed</option><option value="running">Running</option><option value="stopped">Stopped</option><option value="failed">Failed</option></select><Button className="primary" onClick={() => void refresh()}><RefreshCw aria-hidden="true" size={14} />刷新 Runs</Button></>} />
    <div className="runs-layout"><aside className="run-index"><header><span>最近执行</span><strong>{runs.data?.length || 0}</strong></header>{runs.status === "loading" && <Skeleton lines={5} />}{runs.status === "error" && <ErrorState message={runs.error} retry={() => void loadRuns(true)} />}{runs.status === "success" && visible.length === 0 && <p className="run-index-empty">{(runs.data?.length || 0) === 0 ? "暂无 Run" : "没有匹配状态"}</p>}{visible.map((run) => <button key={run.runId} className={run.runId === activeRunId ? "active" : ""} onClick={() => void selectRun(run.runId)}><div><strong>{run.runId}</strong><Badge tone={tone(run.status)}>{run.status}</Badge></div><span>{run.mode} · {formatTime(run.startedAt)}</span><small>{run.reasoningSteps} steps · {run.modelCalls} models · {run.toolCalls} tools</small></button>)}</aside>
      <article className="run-detail">{runs.status === "success" && (runs.data?.length || 0) === 0 && <EmptyState title="还没有 Run" message="发起一次聊天任务后，可在这里查看完整执行时间线。" />}{activeDetail.status === "loading" && <Skeleton lines={10} />}{activeDetail.status === "error" && <ErrorState message={activeDetail.error} retry={() => activeRunId ? void selectRun(activeRunId, true) : undefined} />}{activeDetail.status === "success" && detail && <><header className="run-detail-header"><div><h3>{detail.runId}</h3><p>{displayValue(state.mode, "-")} · {formatTime(String(state.started_at || ""))}</p></div><Badge tone={tone(String(state.status || "unknown"))}>{displayValue(state.status, "unknown")}</Badge></header><div className="trace-metrics">{metricEntries.map(([label, value]) => <div key={label}><span>{label}</span><strong>{displayValue(value)}</strong></div>)}</div>
        <TraceSection title="用户请求"><p className="request-preview">{displayValue(request, "未记录请求摘要")}</p></TraceSection>
        <TraceSection title="执行时间线"><div className="trace-timeline">{grouped.steps.length === 0 && <EmptyState title="没有 Step 事件" message="独立 Run 事件仍在下方保留。" />}{grouped.steps.map((step) => <section className="trace-step" key={step.number}><span className="step-marker">{step.number}</span><div className="step-card"><h5>Step {step.number} · Context / Model / Tools</h5>{step.events.map((event, index) => <TraceEventView event={event} key={`${event.event}-${index}`} />)}</div></section>)}</div></TraceSection>
        <TraceSection title="Subagents">{detail.subagents.length === 0 ? <EmptyState title="没有 Subagent" message="此 Run 未记录子任务。" /> : <SubagentPanel items={detail.subagents} />}</TraceSection>
        <TraceSection title="Run 事件"><div className="trace-event-list">{grouped.runEvents.map((event, index) => <TraceEventView event={event} key={`${event.event}-${index}`} />)}</div></TraceSection>
        <TraceSection title="Run State"><JsonDisclosure label="查看完整状态 JSON" value={state} /></TraceSection>
        <TraceSection title="最终结果"><pre className="final-answer">{displayValue(state.final_answer || state.error || state.stop_reason)}</pre></TraceSection></>}</article>
    </div></div>;
}

function TraceSection({ title, children }: { title: string; children: ReactNode }) { return <section className="trace-section"><h4>{title}</h4>{children}</section>; }
function tone(status: string) { const clean = status.toLowerCase(); return ["completed", "success", "ready"].includes(clean) ? "success" : ["failed", "error"].includes(clean) ? "error" : ["stopped", "incomplete"].includes(clean) ? "warn" : clean === "running" ? "info" : "neutral"; }
function formatDuration(value: unknown) { const number = Number(value); if (!Number.isFinite(number)) return displayValue(value); return number < 1000 ? `${Math.round(number)}ms` : `${(number / 1000).toFixed(1)}s`; }

function SubagentPanel({ items }: { items: SubagentSummary[] }) {
  const statuses = items.map(subagentStatus);
  const completed = statuses.filter((status) => status === "success").length;
  const running = statuses.filter((status) => status === "running").length;
  const failed = statuses.filter((status) => status === "failed" || status === "incomplete").length;
  const events = items.reduce((total, item) => total + item.eventCount, 0);
  return <div className="subagent-panel">
    <section className="subagent-parent-summary">
      <div className="subagent-parent-title"><span><Bot aria-hidden="true" /></span><div><strong>Subagent 执行汇总</strong><small>{items.length} 个子任务 · {events} 条事件</small></div></div>
      <div className="subagent-parent-metrics"><SummaryMetric label="总计" value={items.length} /><SummaryMetric label="运行中" value={running} tone="info" /><SummaryMetric label="成功" value={completed} tone="success" /><SummaryMetric label="异常" value={failed} tone={failed ? "error" : ""} /></div>
    </section>
    <div className="subagent-children">{items.map((item, index) => <SubagentCard item={item} index={index} key={item.sessionId || `${item.agentType}-${index}`} />)}</div>
  </div>;
}

function SummaryMetric({ label, value, tone: metricTone = "" }: { label: string; value: number; tone?: string }) {
  return <div className={metricTone}><span>{label}</span><strong>{value}</strong></div>;
}

function SubagentCard({ item, index }: { item: SubagentSummary; index: number }) {
  const [open, setOpen] = useState(false);
  const [eventLimit, setEventLimit] = useState(80);
  const status = subagentStatus(item);
  const visibleEvents = item.events.slice(Math.max(0, item.events.length - eventLimit));
  const hiddenEvents = item.events.length - visibleEvents.length;
  const title = item.description || item.agentType || item.sessionId || `Subagent ${index + 1}`;
  return <details className={`subagent-card subagent-${status}`} open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>
      <div className="subagent-title"><span className="subagent-node">{status === "success" ? <CheckCircle2 aria-hidden="true" /> : status === "running" ? <CircleDot aria-hidden="true" /> : <TriangleAlert aria-hidden="true" />}</span><span><strong>{title}</strong><small>{item.sessionId}</small></span></div>
      <div className="subagent-card-meta"><span>{item.reasoningSteps} steps · {item.modelCalls} models · {item.toolCalls} tools · {item.eventCount} events</span><Badge tone={tone(status)}>{status}</Badge></div>
    </summary>
    {open && <div className="subagent-body">
      <div className="subagent-facts"><div><span>类型</span><strong>{item.agentType || "unknown"}</strong></div><div><span>开始</span><strong>{formatTime(item.startedAt)}</strong></div><div><span>工具异常</span><strong>{item.toolFailures + item.toolDenials}</strong></div><div><span>停止原因</span><strong>{item.stopReason || (status === "running" ? "运行中" : "-")}</strong></div></div>
      {(item.promptPreview || item.summaryPreview || item.errorPreview) && <div className="subagent-copy-grid">{item.promptPreview && <details><summary>任务说明</summary><pre>{item.promptPreview}</pre></details>}{item.summaryPreview && <details><summary>执行总结</summary><pre>{item.summaryPreview}</pre></details>}{item.errorPreview && <details className="error"><summary>异常信息</summary><pre>{item.errorPreview}</pre></details>}</div>}
      <div className="subagent-events-header"><div><strong>事件记录</strong><small>与主 Agent Trace 使用相同的事件与 Payload 折叠视图</small></div><span>{item.events.length}</span></div>
      {hiddenEvents > 0 && <Button className="subagent-load-events" onClick={() => setEventLimit((value) => value + 80)}>加载更早事件（剩余 {hiddenEvents}）</Button>}
      {visibleEvents.length === 0 ? <EmptyState title="暂无事件记录" message="Subagent 已登记，但还没有写入具体事件。" /> : <SubagentEventGroups events={visibleEvents} />}
    </div>}
  </details>;
}

function SubagentEventGroups({ events }: { events: TraceEvent[] }) {
  const groups = useMemo(() => {
    const result: Array<{ step: number | null; events: TraceEvent[] }> = [];
    for (const event of events) {
      const payloadStep = Number(event.payload.step);
      const step = event.step || (Number.isFinite(payloadStep) && payloadStep > 0 ? payloadStep : null);
      const previous = result.at(-1);
      if (!previous || previous.step !== step) result.push({ step, events: [event] });
      else previous.events.push(event);
    }
    return result;
  }, [events]);
  return <div className="subagent-event-groups">{groups.map((group, groupIndex) => <section key={`${group.step ?? "run"}-${groupIndex}`}><header>{group.step ? `Step ${group.step}` : "Lifecycle"}<span>{group.events.length} events</span></header><div className="trace-event-list">{group.events.map((event, index) => <TraceEventView event={event} key={`${event.timestamp}-${event.spanId}-${event.event}-${index}`} />)}</div></section>)}</div>;
}

function subagentStatus(item: SubagentSummary) {
  if (item.success === true) return "success";
  if (item.success === false) return "failed";
  return item.events.some((event) => event.event === "subagent.completed") ? "incomplete" : "running";
}
