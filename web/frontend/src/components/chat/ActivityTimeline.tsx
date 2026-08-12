import { useEffect, useState } from "react";
import { Bot, CheckCircle2, CircleDot, Timer, Wrench, XCircle } from "lucide-react";
import type { ActivityItem, ActivitySnapshot } from "../../hooks/useChatStream";

type ActivityStatus = ActivitySnapshot["status"] | "running";

interface ActivityTimelineProps {
  items: ActivityItem[];
  status: ActivityStatus;
  startedAt: number;
  finishedAt?: number;
}

export function ActivityTimeline({ items, status, startedAt, finishedAt = 0 }: ActivityTimelineProps) {
  const now = useActivityClock(status === "running");
  const effectiveNow = finishedAt || now;
  const tools = groupTools(items);
  const subagents = groupSubagents(items);
  const elapsed = Math.max(0, effectiveNow - startedAt);

  if (status === "running") {
    return <section className="activity-live" aria-live="polite">
      <div className="activity-status-line">
        <span className="activity-running-label"><CircleDot aria-hidden="true" size={14} /><strong>执行中</strong></span>
        <span className="activity-metric"><Wrench aria-hidden="true" size={13} />{tools.length} 次工具调用</span>
        {subagents.length > 0 && <span className="activity-metric"><Bot aria-hidden="true" size={13} />{subagents.length} 个子 Agent</span>}
        <time className="activity-metric"><Timer aria-hidden="true" size={13} />{formatDuration(elapsed)}</time>
      </div>
      {(tools.length > 0 || subagents.length > 0) && <ActivityDisclosure tools={tools} subagents={subagents} now={effectiveNow} label={`查看执行活动 · ${tools.length + subagents.length}`} />}
    </section>;
  }

  if (!tools.length && !subagents.length) return null;
  const failed = tools.filter((tool) => tool.status === "error").length;
  const failedSubagents = subagents.filter((subagent) => subagent.status === "error").length;
  const parts = [tools.length ? `${tools.length} 次工具调用` : "", subagents.length ? `${subagents.length} 个子 Agent` : ""].filter(Boolean).join(" · ");
  const totalFailed = failed + failedSubagents;
  const label = `${status === "error" ? "执行中断" : "执行完成"} · ${parts} · ${formatDuration(elapsed)}${totalFailed ? ` · ${totalFailed} 失败` : ""}`;
  return <ActivityDisclosure tools={tools} subagents={subagents} now={effectiveNow} label={label} className={status === "error" ? "error" : "complete"} />;
}

type ToolActivity = {
  key: string;
  tool: string;
  status: "running" | "success" | "error";
  args: string;
  preview: string;
  durationMs: number;
  startedAt: number;
  raw: Record<string, unknown>;
};

type SubagentActivity = {
  key: string;
  agentType: string;
  description: string;
  status: "running" | "success" | "error";
  startedAt: number;
  finishedAt: number;
};

function ActivityDisclosure({ tools, subagents, now, label, className = "" }: { tools: ToolActivity[]; subagents: SubagentActivity[]; now: number; label: string; className?: string }) {
  return <details className={`activity-panel ${className}`.trim()}>
    <summary>{label}</summary>
    <div className="activity-tool-list">{subagents.map((subagent) => <SubagentRow key={subagent.key} subagent={subagent} now={now} />)}{tools.map((tool) => <ToolRow key={tool.key} tool={tool} now={now} />)}</div>
  </details>;
}

function SubagentRow({ subagent, now }: { subagent: SubagentActivity; now: number }) {
  const Icon = subagent.status === "running" ? CircleDot : subagent.status === "error" ? XCircle : CheckCircle2;
  const duration = (subagent.finishedAt || now) - subagent.startedAt;
  return <div className={`activity-tool-row ${subagent.status}`}>
    <Icon className="activity-tool-icon" aria-hidden="true" size={15} />
    <div className="activity-tool-copy"><strong>{subagent.agentType}</strong><small>{subagent.description}</small></div>
    <span className="activity-tool-meta">{subagent.status === "running" ? "运行中" : subagent.status === "error" ? "失败" : "完成"} · {formatDuration(duration)}</span>
  </div>;
}

function ToolRow({ tool, now }: { tool: ToolActivity; now: number }) {
  const Icon = tool.status === "running" ? CircleDot : tool.status === "error" ? XCircle : CheckCircle2;
  const duration = tool.status === "running" ? Math.max(0, now - tool.startedAt) : tool.durationMs;
  return <div className={`activity-tool-row ${tool.status}`}>
    <Icon className="activity-tool-icon" aria-hidden="true" size={15} />
    <div className="activity-tool-copy"><strong>{tool.tool}</strong><small>{toolDetail(tool)}</small></div>
    <span className="activity-tool-meta">{tool.status === "running" ? "运行中" : tool.status === "error" ? "失败" : "完成"} · {formatDuration(duration)}</span>
  </div>;
}

function groupTools(items: ActivityItem[]): ToolActivity[] {
  const grouped = new Map<string, ToolActivity>();
  items.filter((item) => item.event.startsWith("tool.call.")).forEach((item, index) => {
    const raw = item.raw;
    const key = String(raw.span_id || raw.tool_call_id || `tool:${index}`);
    const existing: ToolActivity = grouped.get(key) || {
      key,
      tool: String(raw.tool || raw.tool_name || "tool"),
      status: "running",
      args: "",
      preview: "",
      durationMs: 0,
      startedAt: item.receivedAt,
      raw,
    };
    existing.tool = String(raw.tool || raw.tool_name || existing.tool);
    existing.args = String(raw.args || existing.args || "");
    existing.preview = String(raw.preview || existing.preview || item.label || "");
    existing.durationMs = numberOrZero(raw.duration_ms) || existing.durationMs;
    existing.raw = { ...existing.raw, ...raw };
    if (item.event === "tool.call.completed") existing.status = "success";
    if (item.event === "tool.call.failed") existing.status = "error";
    grouped.set(key, existing);
  });
  return [...grouped.values()];
}

function groupSubagents(items: ActivityItem[]): SubagentActivity[] {
  const grouped = new Map<string, SubagentActivity>();
  items.filter((item) => item.event === "subagent.started" || item.event === "subagent.completed").forEach((item, index) => {
    const raw = item.raw;
    const key = String(raw.span_id || `subagent:${index}`);
    const existing = grouped.get(key) || {
      key,
      agentType: String(raw.agent_type || "subagent"),
      description: String(raw.description || ""),
      status: "running" as const,
      startedAt: item.receivedAt,
      finishedAt: 0,
    };
    existing.agentType = String(raw.agent_type || existing.agentType);
    existing.description = String(raw.description || existing.description);
    if (item.event === "subagent.completed") {
      existing.status = raw.success === false ? "error" : "success";
      existing.finishedAt = item.receivedAt;
    }
    grouped.set(key, existing);
  });
  return [...grouped.values()];
}

function toolDetail(tool: ToolActivity) {
  const args = parseArgs(tool.args);
  const preferred = args.path || args.file || args.command || args.cmd;
  return squash(String(preferred || tool.preview || tool.args || ""));
}

function parseArgs(value: string): Record<string, string> {
  try {
    const parsed = JSON.parse(value.replace(/\.\.\.\[truncated\]$/, ""));
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as Record<string, string> : {};
  } catch { return {}; }
}

function numberOrZero(value: unknown) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? number : 0;
}

function formatDuration(value: number) {
  if (value < 1000) return `${Math.max(1, Math.round(value))}ms`;
  return `${(value / 1000).toFixed(1)}s`;
}

function squash(value: string) {
  const clean = value.replace(/\s+/g, " ").trim();
  return clean.length > 160 ? `${clean.slice(0, 157)}...` : clean;
}

function useActivityClock(running: boolean) {
  const [now, setNow] = useState(() => performance.now());
  useEffect(() => {
    if (!running) return;
    const timer = window.setInterval(() => setNow(performance.now()), 100);
    return () => window.clearInterval(timer);
  }, [running]);
  return now;
}
