import { useEffect, useState } from "react";
import { CheckCircle2, CircleDot, Timer, Wrench, XCircle } from "lucide-react";
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
  const elapsed = Math.max(0, effectiveNow - startedAt);

  if (status === "running") {
    return <section className="activity-live" aria-live="polite">
      <div className="activity-status-line">
        <span className="activity-running-label"><CircleDot aria-hidden="true" size={14} /><strong>执行中</strong></span>
        <span className="activity-metric"><Wrench aria-hidden="true" size={13} />{tools.length} 次工具调用</span>
        <time className="activity-metric"><Timer aria-hidden="true" size={13} />{formatDuration(elapsed)}</time>
      </div>
      {tools.length > 0 && <ToolDisclosure tools={tools} now={effectiveNow} label={`查看工具调用 · ${tools.length}`} />}
    </section>;
  }

  if (!tools.length) return null;
  const failed = tools.filter((tool) => tool.status === "error").length;
  const label = `${status === "error" ? "执行中断" : "执行完成"} · ${tools.length} 次工具调用 · ${formatDuration(elapsed)}${failed ? ` · ${failed} 失败` : ""}`;
  return <ToolDisclosure tools={tools} now={effectiveNow} label={label} className={status === "error" ? "error" : "complete"} />;
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

function ToolDisclosure({ tools, now, label, className = "" }: { tools: ToolActivity[]; now: number; label: string; className?: string }) {
  return <details className={`activity-panel ${className}`.trim()}>
    <summary>{label}</summary>
    <div className="activity-tool-list">{tools.map((tool) => <ToolRow key={tool.key} tool={tool} now={now} />)}</div>
  </details>;
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
