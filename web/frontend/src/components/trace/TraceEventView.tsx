import { inferLogLevel, summarizeEvent } from "../../api/adapters";
import { Braces } from "lucide-react";
import type { TraceEvent } from "../../api/types";
import { JsonDisclosure } from "../ui";

export function TraceEventView({ event }: { event: TraceEvent }) {
  return <div className={`trace-event trace-${inferLogLevel(event)}`}><div><strong><Braces aria-hidden="true" size={12} />{event.event}</strong><span>{summarizeEvent(event)}</span><small>{duration(event) || formatTime(event.timestamp)}</small></div><JsonDisclosure value={event.payload} /></div>;
}

export function formatTime(value: string) {
  if (!value) return "-"; const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value : date.toLocaleString("zh-CN", { hour12: false });
}

function duration(event: TraceEvent) {
  const value = Number(event.payload.duration_ms); if (!Number.isFinite(value)) return "";
  return value < 1000 ? `${Math.round(value)}ms` : `${(value / 1000).toFixed(1)}s`;
}
