import type {
  LogLevel, LogRow, RunDetail, RunDetailDto, RunSummary, RunSummaryDto,
  SubagentSummary, TraceEvent, TraceEventDto, TraceStep,
} from "./types";

export const asRecord = (value: unknown): Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value)
    ? value as Record<string, unknown>
    : {};

const asString = (value: unknown, fallback = "") =>
  value === null || value === undefined ? fallback : String(value);

const asNumber = (value: unknown, fallback = 0) => {
  const number = Number(value);
  return Number.isFinite(number) ? number : fallback;
};

export function adaptRunSummary(dto: RunSummaryDto): RunSummary {
  return {
    runId: asString(dto.run_id), sessionId: asString(dto.session_id),
    status: asString(dto.status, "unknown"), mode: asString(dto.mode, "-"),
    startedAt: asString(dto.started_at), finishedAt: asString(dto.finished_at),
    reasoningSteps: asNumber(dto.reasoning_steps), modelCalls: asNumber(dto.model_calls),
    toolCalls: asNumber(dto.tool_calls),
  };
}

export function adaptTraceEvent(dto: TraceEventDto): TraceEvent {
  const rawStep = dto.step;
  const step = rawStep === null || rawStep === undefined || rawStep === ""
    ? null : asNumber(rawStep);
  return {
    timestamp: asString(dto.timestamp), runId: asString(dto.run_id),
    event: asString(dto.event, "unknown.event"), sessionId: asString(dto.session_id),
    requestId: asString(dto.request_id), spanId: asString(dto.span_id),
    parentSpanId: asString(dto.parent_span_id), step, payload: asRecord(dto.payload),
  };
}

function adaptSubagent(value: unknown, runId: string): SubagentSummary {
  const raw = asRecord(value);
  const sessionId = asString(raw.session_id);
  const events = Array.isArray(raw.events) ? raw.events.map((value) => {
    const event = asRecord(value);
    return adaptTraceEvent({
      ...event,
      run_id: event.run_id || runId,
      session_id: event.session_id || sessionId,
    });
  }) : [];
  return {
    sessionId, description: asString(raw.description),
    agentType: asString(raw.agent_type), success: typeof raw.success === "boolean" ? raw.success : null,
    truncated: Boolean(raw.truncated), stopReason: asString(raw.stop_reason),
    startedAt: asString(raw.started_at), finishedAt: asString(raw.finished_at),
    promptPreview: asString(raw.prompt_preview), summaryPreview: asString(raw.summary_preview),
    errorPreview: asString(raw.error_preview), reasoningSteps: asNumber(raw.reasoning_steps),
    modelCalls: asNumber(raw.model_calls), toolCalls: asNumber(raw.tool_calls || raw.tool_count),
    toolFailures: asNumber(raw.tool_failures), toolDenials: asNumber(raw.tool_denials),
    eventCount: asNumber(raw.event_count, events.length),
    models: Array.isArray(raw.models) ? raw.models.map((item) => asString(item)).filter(Boolean) : [],
    tools: Array.isArray(raw.tools) ? raw.tools.map((item) => asString(item)).filter(Boolean) : [],
    events, raw,
  };
}

export function adaptRunDetail(dto: RunDetailDto): RunDetail {
  const runId = asString(dto.run_id);
  const events = Array.isArray(dto.events) ? dto.events.map((event) => adaptTraceEvent(asRecord(event))) : [];
  const subagents = Array.isArray(dto.subagents) ? dto.subagents.map((item) => adaptSubagent(item, runId)) : [];
  return {
    runId, runState: asRecord(dto.run_state), report: asRecord(dto.report),
    metrics: asRecord(dto.metrics), events, subagents,
  };
}

export function inferLogLevel(event: TraceEvent): LogLevel {
  const haystack = `${event.event} ${asString(event.payload.status)} ${asString(event.payload.stop_reason)}`.toLowerCase();
  if (/failed|error|denied|exception|fatal/.test(haystack)) return "error";
  if (/warn|retry|stopped|cancel|truncat|compress|guard|cooldown|fallback/.test(haystack)) return "warn";
  return "info";
}

export function summarizeEvent(event: TraceEvent): string {
  const p = event.payload;
  const preferred = [p.error_message, p.error_preview, p.output_preview, p.content_preview,
    p.summary_preview, p.reason, p.stop_reason, p.description, p.status]
    .find((value) => value !== undefined && value !== null && String(value).trim());
  let text = preferred ? String(preferred) : "";
  if (!text) {
    try { text = Object.keys(p).length ? JSON.stringify(p) : event.event; }
    catch { text = event.event; }
  }
  text = text.replace(/\s+/g, " ").trim();
  return text.length > 240 ? `${text.slice(0, 237)}…` : text;
}

export function deriveLogRows(detail: RunDetail): LogRow[] {
  return detail.events.map((event, index) => ({
    id: `${event.timestamp}:${event.spanId}:${event.event}:${index}`,
    timestamp: event.timestamp, level: inferLogLevel(event),
    source: asString(event.payload.tool_name || event.payload.model || event.event, "runtime"),
    message: summarizeEvent(event), event: event.event, step: event.step, payload: event.payload,
  }));
}

export function groupTrace(detail: RunDetail): { steps: TraceStep[]; runEvents: TraceEvent[] } {
  const groups = new Map<number, TraceEvent[]>();
  const runEvents: TraceEvent[] = [];
  for (const event of detail.events) {
    if (event.sessionId.startsWith("subtask:")) continue;
    const payloadStep = asNumber(event.payload.step, 0);
    const number = event.step || payloadStep;
    if (!number) { runEvents.push(event); continue; }
    groups.set(number, [...(groups.get(number) || []), event]);
  }
  return { steps: [...groups].sort(([a], [b]) => a - b).map(([number, events]) => ({ number, events })), runEvents };
}

export const displayValue = (value: unknown, fallback = "不适用") =>
  value === null || value === undefined || value === "" ? fallback : String(value);
