import { useEffect, useMemo, useState } from "react";
import { RefreshCw } from "lucide-react";
import { deriveLogRows } from "../api/adapters";
import type { LogLevel } from "../api/types";
import { useRuns } from "../hooks/useRuns";
import { Button, ErrorState, JsonDisclosure, PageHeader, Skeleton } from "../components/ui";
import { formatTime } from "../components/trace/TraceEventView";

export default function LogsPage() {
  const { runs, activeRunId, activeDetail, loadRuns, selectRun, refreshActiveRun } = useRuns();
  const [query, setQuery] = useState(""); const [level, setLevel] = useState<"all" | LogLevel>("all");
  useEffect(() => { void loadRuns().then((items) => { const id = activeRunId || items[0]?.runId; if (id) void selectRun(id); }).catch(() => undefined); }, [activeRunId, loadRuns, selectRun]);
  const all = useMemo(() => activeDetail.data ? deriveLogRows(activeDetail.data) : [], [activeDetail.data]);
  const rows = useMemo(() => all.filter((row) => (level === "all" || row.level === level) && (!query.trim() || `${row.event} ${row.source} ${row.message}`.toLowerCase().includes(query.trim().toLowerCase()))), [all, level, query]);
  return <div className="page logs-page"><PageHeader eyebrow="Observability" title="系统日志" description="查看每次 Agent Run 的结构化 Trace 事件。" action={<Button className="primary" onClick={() => void refreshActiveRun()}><RefreshCw aria-hidden="true" size={14} />刷新日志</Button>} />
    <div className="page-body logs-body"><div className="metric-strip"><Metric label="总计" value={all.length} /><Metric label="错误" value={all.filter((row) => row.level === "error").length} tone="error" /><Metric label="警告" value={all.filter((row) => row.level === "warn").length} tone="warn" /></div>
      <div className="filter-bar"><label>Run<select value={activeRunId} onChange={(event) => void selectRun(event.target.value)}>{(runs.data || []).map((run) => <option value={run.runId} key={run.runId}>{run.runId} · {run.status}</option>)}</select></label><label className="grow">搜索<input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="筛选事件、来源或消息……" /></label><label>级别<select value={level} onChange={(event) => setLevel(event.target.value as "all" | LogLevel)}><option value="all">全部级别</option><option value="info">INFO</option><option value="warn">WARN</option><option value="error">ERROR</option></select></label></div>
      {(runs.status === "loading" || activeDetail.status === "loading") && <Skeleton lines={8} />}{(runs.status === "error" || activeDetail.status === "error") && <ErrorState message={runs.error || activeDetail.error} retry={() => void loadRuns(true)} />}
      {activeDetail.status === "success" && <div className="data-table-wrap"><table className="data-table"><thead><tr><th>时间</th><th>级别</th><th>来源</th><th>消息</th><th>详情</th></tr></thead><tbody>{rows.length === 0 ? <tr><td colSpan={5} className="table-empty">没有匹配的 Trace 事件</td></tr> : rows.map((row) => <tr key={row.id}><td className="nowrap">{formatTime(row.timestamp)}</td><td><span className={`log-level ${row.level}`}>{row.level}</span></td><td className="log-source">{row.source}</td><td>{row.message}</td><td><JsonDisclosure label={row.step ? `Step ${row.step}` : row.event} value={row.payload} /></td></tr>)}</tbody></table></div>}
    </div></div>;
}

function Metric({ label, value, tone = "" }: { label: string; value: number; tone?: string }) { return <div className={`metric-pill ${tone}`}><span>{label}</span><strong>{value}</strong></div>; }
