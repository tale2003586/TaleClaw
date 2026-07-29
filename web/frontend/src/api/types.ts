export type UserRole = "admin" | "user";

export interface CurrentUser {
  id: string;
  role: UserRole;
}

export interface HealthResponse {
  ok: boolean;
  workspace: string;
  coding_workspace: string;
  user: CurrentUser;
  runtime: string;
}

export interface RuntimeHealthResponse {
  ok: boolean;
  runtime: string;
  error?: string;
}

export interface MessageDto {
  seq?: number;
  role: string;
  content?: string;
  name?: string;
  tool_calls?: unknown[];
  metadata?: Record<string, unknown>;
}

export interface SessionDto {
  id?: string;
  chat_id?: string;
  channel?: string;
  title?: string;
  current_mode?: string;
  updated_at?: string;
  messages?: MessageDto[];
  message_page?: {
    has_more: boolean;
    next_before?: number | null;
  };
}

export interface SessionsResponse {
  sessions: SessionDto[];
  has_more?: boolean;
  next_offset?: number | null;
}
export interface SessionResponse { session: SessionDto }

export interface MemoryFile { name: string; content: string }
export interface MemoryResponse { files: MemoryFile[] }

export interface FileEntry {
  name: string;
  path: string;
  type?: "file" | "directory" | string;
  is_dir: boolean;
  previewable?: boolean;
  mime?: string;
  size?: number;
  modified?: string;
}

export interface FilesResponse {
  path: string;
  parent: string;
  entries: FileEntry[];
  record_path?: string;
}

export interface FilePreviewResponse {
  path: string;
  name?: string;
  previewable?: boolean;
  content?: string;
  download_url?: string;
}

export interface AnalysisResponse {
  reply?: string;
  analysis?: string;
  record_path?: string;
  record_download_url?: string;
}

export interface RunSummaryDto {
  run_id?: unknown;
  session_id?: unknown;
  status?: unknown;
  mode?: unknown;
  started_at?: unknown;
  finished_at?: unknown;
  reasoning_steps?: unknown;
  model_calls?: unknown;
  tool_calls?: unknown;
}

export interface TraceEventDto {
  timestamp?: unknown;
  run_id?: unknown;
  event?: unknown;
  session_id?: unknown;
  request_id?: unknown;
  span_id?: unknown;
  parent_span_id?: unknown;
  step?: unknown;
  payload?: unknown;
}

export interface RunDetailDto {
  run_id?: unknown;
  run_state?: unknown;
  report?: unknown;
  metrics?: unknown;
  events?: unknown;
  subagents?: unknown;
}

export interface RunSummary {
  runId: string;
  sessionId: string;
  status: string;
  mode: string;
  startedAt: string;
  finishedAt: string;
  reasoningSteps: number;
  modelCalls: number;
  toolCalls: number;
}

export interface TraceEvent {
  timestamp: string;
  runId: string;
  event: string;
  sessionId: string;
  requestId: string;
  spanId: string;
  parentSpanId: string;
  step: number | null;
  payload: Record<string, unknown>;
}

export interface SubagentSummary {
  sessionId: string;
  description: string;
  agentType: string;
  success: boolean | null;
  stopReason: string;
  summaryPreview: string;
  raw: Record<string, unknown>;
}

export interface RunDetail {
  runId: string;
  runState: Record<string, unknown>;
  report: Record<string, unknown>;
  metrics: Record<string, unknown>;
  events: TraceEvent[];
  subagents: SubagentSummary[];
}

export type LogLevel = "info" | "warn" | "error";

export interface LogRow {
  id: string;
  timestamp: string;
  level: LogLevel;
  source: string;
  message: string;
  event: string;
  step: number | null;
  payload: Record<string, unknown>;
}

export interface TraceStep { number: number; events: TraceEvent[] }

export type ChatStreamEvent =
  | { type: "delta"; text?: string }
  | ({ type: "event" } & Record<string, unknown>)
  | ({ type: "complete" } & Record<string, unknown>)
  | { type: "error"; error?: string };

export type AsyncStatus = "idle" | "loading" | "success" | "error";
export interface AsyncState<T> {
  status: AsyncStatus;
  data: T | null;
  error: string;
  updatedAt: number | null;
}
