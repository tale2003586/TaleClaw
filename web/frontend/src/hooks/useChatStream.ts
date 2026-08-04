import { useCallback, useRef, useState } from "react";
import { postJson, streamNdjson } from "../api/client";
import type { ChatStreamEvent, SessionDto } from "../api/types";

export interface ActivityItem { id: string; event: string; label: string; step: number | null; receivedAt: number; raw: Record<string, unknown> }
export interface ActivitySnapshot { sessionId: string; items: ActivityItem[]; startedAt: number; finishedAt: number; status: "complete" | "error" }

export function useChatStream(onComplete: (session: SessionDto | null, reply: string, activity: ActivitySnapshot) => void) {
  const [status, setStatus] = useState<"idle" | "streaming" | "stopping" | "error">("idle");
  const [text, setText] = useState(""); const [error, setError] = useState("");
  const [progressText, setProgressText] = useState("");
  const [activity, setActivity] = useState<ActivityItem[]>([]);
  const [startedAt, setStartedAt] = useState(0); const [finishedAt, setFinishedAt] = useState(0);
  const controller = useRef<AbortController | null>(null);
  const send = useCallback(async (sessionId: string, message: string, workspaceRoot = "", attachments: string[] = [], thinkingEnabled = false) => {
    controller.current?.abort(); const abort = new AbortController(); controller.current = abort;
    const requestStartedAt = performance.now();
    setStatus("streaming"); setText(""); setError(""); setProgressText(attachments.length ? "正在准备附件解析…" : ""); setActivity([]); setStartedAt(requestStartedAt); setFinishedAt(0);
    let complete: Record<string, unknown> | null = null;
    let accumulated = "";
    let activityItems: ActivityItem[] = [];
    try {
      await streamNdjson<ChatStreamEvent>("/api/chat/stream", { session_id: sessionId, message, attachments, thinking_enabled: thinkingEnabled, ...(workspaceRoot ? { workspace_root: workspaceRoot } : {}) }, (event) => {
        if (event.type === "delta") { accumulated += event.text || ""; setText(accumulated); setProgressText(""); }
        else if (event.type === "status") setProgressText(event.text || "");
        else if (event.type === "event") {
          const raw = event as Record<string, unknown>; const name = String(raw.event || "runtime.event");
          if (!name.startsWith("tool.call.")) return;
          const item = { id: `${String(raw.span_id || "")}:${name}:${activityItems.length}`, event: name, label: activityLabel(raw), step: numberOrNull(raw.step), receivedAt: performance.now(), raw };
          activityItems = [...activityItems, item]; setActivity(activityItems);
        } else if (event.type === "complete") complete = event as Record<string, unknown>;
        else if (event.type === "error") throw new Error(event.error || "流式请求失败");
      }, abort.signal);
      if (!complete) throw new Error("流式响应意外结束");
      const completed = complete as Record<string, unknown>;
      const endedAt = performance.now();
      onComplete((completed.session as SessionDto | undefined) || null, String(completed.reply || accumulated), { sessionId, items: activityItems, startedAt: requestStartedAt, finishedAt: endedAt, status: "complete" });
      // The completed session now contains the assistant message. Clear the
      // transient streaming copy so it is not rendered a second time.
      setText(""); setProgressText(""); setActivity([]); setFinishedAt(endedAt); setStatus("idle");
    } catch (reason) {
      if ((reason as Error).name === "AbortError") return;
      setFinishedAt(performance.now()); setProgressText(""); setError(reason instanceof Error ? reason.message : String(reason)); setStatus("error");
    }
  }, [onComplete]);
  const stop = useCallback(async (sessionId: string) => {
    setStatus("stopping");
    try { await postJson("/api/chat/stop", { session_id: sessionId }); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); setStatus("error"); }
  }, []);
  return { status, text, error, progressText, activity, startedAt, finishedAt, send, stop };
}

function numberOrNull(value: unknown) { const number = Number(value); return Number.isFinite(number) && number > 0 ? number : null; }
function activityLabel(event: Record<string, unknown>) {
  const preview = event.preview || event.args || event.output_preview || event.content_preview || event.description || event.tool || event.tool_name || event.model;
  return preview ? String(preview).replace(/\s+/g, " ").slice(0, 180) : String(event.event || "Runtime event");
}
