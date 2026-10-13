import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, getJson, postJson, subscribeNdjson } from "../api/client";
import type { ChatRunDto, ChatRunEvent, ChatRunRequest, ChatRunStatus, SessionDto, SessionResponse } from "../api/types";

export interface ActivityItem { id: string; event: string; label: string; step: number | null; receivedAt: number; raw: Record<string, unknown> }
export interface ActivitySnapshot { sessionId: string; items: ActivityItem[]; startedAt: number; finishedAt: number; status: "complete" | "error" }
export interface ChatRunView {
  run: ChatRunDto;
  cursor: number;
  text: string;
  thinking: string;
  activity: ActivityItem[];
  progressText: string;
  connectionError: string;
  startedAt: number;
  finishedAt: number;
  finalizing: boolean;
  hydrated: boolean;
  separateNextSegment: boolean;
  completedSession?: SessionDto;
}
interface PendingSubmission { request: ChatRunRequest & { request_id: string }; error: string; sending: boolean }
const ACTIVE_STATUSES = new Set<ChatRunStatus>(["queued", "running", "cancelling"]);
export const isActiveChatRun = (run?: ChatRunDto) => Boolean(run && ACTIVE_STATUSES.has(run.status));
export const chatRunStatusLabel = (status: ChatRunStatus) => ({ queued: "排队中", running: "运行中", cancelling: "停止中", completed: "已完成", stopped: "已停止", failed: "失败", interrupted: "已中断" })[status];

function newView(run: ChatRunDto, hydrated = false): ChatRunView {
  return { run, cursor: 0, text: "", thinking: "", activity: [], progressText: "", connectionError: "", startedAt: performance.now(), finishedAt: 0, finalizing: false, hydrated, separateNextSegment: false };
}
function messageOf(error: unknown) { return error instanceof Error ? error.message : String(error); }

export function useChatRuns(onComplete: (sessionId: string, session: SessionDto) => void, enabled = true) {
  const [views, setViews] = useState<Record<string, ChatRunView>>({});
  const [pending, setPending] = useState<Record<string, PendingSubmission>>({});
  const [error, setError] = useState("");
  const viewsRef = useRef<Record<string, ChatRunView>>({});
  const pendingRef = useRef<Record<string, PendingSubmission>>({});
  const callback = useRef(onComplete);
  const mounted = useRef(false);
  const subscriptions = useRef(new Map<string, AbortController>());
  const listController = useRef<AbortController | null>(null);
  useEffect(() => { callback.current = onComplete; }, [onComplete]);

  const update = useCallback((runId: string, transform: (current: ChatRunView) => ChatRunView) => {
    const current = viewsRef.current[runId];
    if (!current || !mounted.current) return;
    const next = transform(current);
    viewsRef.current = { ...viewsRef.current, [runId]: next };
    setViews(viewsRef.current);
  }, []);
  const remember = useCallback((run: ChatRunDto, hydrateHistory = false) => {
    if (!mounted.current) return;
    const current = viewsRef.current[run.run_id];
    // A slower list request must not move an observed run back to an older state.
    if (current && (run.last_seq < current.run.last_seq || (!isActiveChatRun(current.run) && isActiveChatRun(run)))) return;
    viewsRef.current = { ...viewsRef.current, [run.run_id]: current
      ? { ...current, run: { ...current.run, ...run } }
      : newView(run, hydrateHistory && !isActiveChatRun(run)) };
    setViews(viewsRef.current);
  }, []);
  const setSubmission = useCallback((sessionId: string, value?: PendingSubmission) => {
    const next = { ...pendingRef.current };
    if (value) next[sessionId] = value; else delete next[sessionId];
    pendingRef.current = next;
    if (mounted.current) setPending(next);
  }, []);

  const reload = useCallback(async () => {
    if (!enabled) return;
    listController.current?.abort();
    const controller = new AbortController();
    listController.current = controller;
    try {
      const result = await getJson<{ runs: ChatRunDto[] }>("/api/chat/runs", { signal: controller.signal });
      if (controller.signal.aborted || !mounted.current) return;
      for (const run of result.runs || []) remember(run, true);
      setError("");
    } catch (reason) {
      if (!controller.signal.aborted && mounted.current) setError(messageOf(reason));
    }
  }, [enabled, remember]);

  const submitRequest = useCallback(async (submission: PendingSubmission) => {
    const { session_id: sessionId } = submission.request;
    setSubmission(sessionId, { ...submission, sending: true, error: "" });
    try {
      const response = await postJson<{ run: ChatRunDto }>("/api/chat/runs", submission.request);
      if (!response.run?.run_id || response.run.session_id !== sessionId) throw new Error("运行响应的会话身份不一致");
      remember(response.run);
      setSubmission(sessionId);
      return response.run;
    } catch (reason) {
      // Definitive client rejection can be edited. A lost response must retain
      // the same idempotency key: retrying it may discover an already-started run.
      if (reason instanceof ApiError && reason.status >= 400 && reason.status < 500 && reason.status !== 408) {
        setSubmission(sessionId);
        void reload();
      } else setSubmission(sessionId, { ...submission, sending: false, error: messageOf(reason) });
      throw reason;
    }
  }, [reload, remember, setSubmission]);
  const submit = useCallback(async (request: ChatRunRequest) => {
    if (pendingRef.current[request.session_id]) throw new Error("该会话的提交结果尚待确认，请重试原请求");
    if (Object.values(viewsRef.current).some(({ run }) => run.session_id === request.session_id && isActiveChatRun(run))) throw new Error("该会话已有运行中的任务");
    const requestId = typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
    return submitRequest({ request: { ...request, request_id: requestId }, sending: true, error: "" });
  }, [submitRequest]);
  const retrySubmission = useCallback(async (sessionId: string) => {
    const submission = pendingRef.current[sessionId];
    if (submission && !submission.sending) return submitRequest(submission);
  }, [submitRequest]);
  const cancel = useCallback(async (runId: string) => {
    update(runId, (view) => ({ ...view, connectionError: "" }));
    try {
      const response = await postJson<{ run?: ChatRunDto }>(`/api/chat/runs/${encodeURIComponent(runId)}/cancel`, {});
      if (response.run) remember(response.run);
      else update(runId, (view) => ({ ...view, run: { ...view.run, status: "cancelling" } }));
    } catch (reason) {
      update(runId, (view) => ({ ...view, connectionError: `停止失败：${messageOf(reason)}` }));
    }
  }, [remember, update]);

  useEffect(() => {
    mounted.current = true;
    const ownedSubscriptions = subscriptions.current;
    if (enabled) void reload();
    const timer = enabled ? window.setInterval(() => void reload(), 10000) : undefined;
    const refresh = () => { if (enabled) void reload(); };
    window.addEventListener("online", refresh);
    return () => {
      mounted.current = false;
      if (timer !== undefined) window.clearInterval(timer);
      window.removeEventListener("online", refresh);
      listController.current?.abort();
      for (const controller of ownedSubscriptions.values()) controller.abort();
      ownedSubscriptions.clear();
      // Unsubscribing never cancels the server run.
    };
  }, [enabled, reload]);

  useEffect(() => {
    if (!enabled) return;
    const consume = (runId: string, event: ChatRunEvent) => {
      const current = viewsRef.current[runId];
      if (!current || event.run_id !== runId || event.session_id !== current.run.session_id
        || !Number.isSafeInteger(event.seq) || event.seq <= current.cursor) return;
      update(runId, (view) => {
        const next = { ...view, cursor: event.seq, connectionError: "", run: { ...view.run, last_seq: Math.max(view.run.last_seq, event.seq) } };
        if (event.type === "delta") {
          const delta = event.text || "";
          next.text += delta && next.separateNextSegment && next.text ? "\n\n" + delta : delta;
          next.separateNextSegment = false;
          next.progressText = "";
        } else if (event.type === "thinking") next.thinking += event.text || "";
        else if (event.type === "assistant_segment" && event.has_content && !event.final) next.separateNextSegment = true;
        else if (event.type === "assistant_completed") { next.finalizing = true; next.progressText = "回答已完成，正在保存…"; }
        else if (event.type === "status") {
          next.progressText = event.text || "";
          if (event.status && ["queued", "running", "cancelling", "completed", "stopped", "failed", "interrupted"].includes(event.status)) next.run.status = event.status as ChatRunStatus;
        } else if (event.type === "event") {
          const raw = event as Record<string, unknown>, name = String(raw.event || "");
          if (name.startsWith("tool.call.") || name.startsWith("subagent.")) {
            const preview = raw.preview || raw.args || raw.output_preview || raw.description || raw.tool || raw.tool_name || name;
            next.activity = [...next.activity, { id: `${runId}:${event.seq}`, event: name, label: String(preview).replace(/\s+/g, " ").slice(0, 180), step: Number(raw.step) > 0 ? Number(raw.step) : null, receivedAt: performance.now(), raw }];
          }
        } else if (event.type === "complete") {
          next.completedSession = event.session as SessionDto | undefined;
          next.run.reply = String(event.reply ?? next.text);
          next.finalizing = true;
          // The detail response supplies the authoritative terminal status,
          // including stopped rather than completed.
        } else if (event.type === "error") next.run.error = event.error || "执行失败";
        return next;
      });
    };
    const finish = async (runId: string, signal: AbortSignal) => {
      const view = viewsRef.current[runId];
      if (!view || view.hydrated) return;
      const session = view.completedSession || (await getJson<SessionResponse>(`/api/session?session_id=${encodeURIComponent(view.run.session_id)}&limit=40`, { signal })).session;
      if (signal.aborted || !mounted.current) return;
      if (!session || (session.chat_id && session.chat_id !== view.run.session_id)) throw new Error("完成响应的会话身份不一致");
      callback.current(view.run.session_id, session);
      update(runId, (current) => ({ ...current, hydrated: true, finishedAt: performance.now(), finalizing: false, connectionError: "", progressText: "" }));
    };
    const subscribe = async (runId: string, controller: AbortController) => {
      let retryMs = 500;
      while (!controller.signal.aborted && mounted.current) {
        try {
          const view = viewsRef.current[runId];
          if (!view || view.hydrated) break;
          if (isActiveChatRun(view.run) || view.cursor < view.run.last_seq) {
            await subscribeNdjson<ChatRunEvent>(`/api/chat/runs/${encodeURIComponent(runId)}/events?after=${view.cursor}`, (event) => consume(runId, event), controller.signal);
          }
          if (controller.signal.aborted) break;
          const response = await getJson<{ run: ChatRunDto }>(`/api/chat/runs/${encodeURIComponent(runId)}`, { signal: controller.signal });
          if (controller.signal.aborted) break;
          if (response.run?.run_id !== runId || response.run.session_id !== view.run.session_id) throw new Error("运行状态的会话身份不一致");
          remember(response.run);
          const current = viewsRef.current[runId];
          if (!isActiveChatRun(current.run) && current.cursor >= current.run.last_seq) {
            await finish(runId, controller.signal);
            break;
          }
          retryMs = 500;
        } catch (reason) {
          if (controller.signal.aborted) break;
          update(runId, (view) => ({ ...view, connectionError: `连接中断，正在重连：${messageOf(reason)}` }));
          retryMs = Math.min(retryMs * 2, 10000);
          if (reason instanceof ApiError && [401, 403, 404].includes(reason.status)) break;
        }
        await abortableDelay(retryMs, controller.signal);
      }
      // Keep finished/denied subscriptions registered until the owning effect
      // unmounts; this prevents an error render from creating a tight retry loop.
    };
    for (const [runId, view] of Object.entries(views)) {
      if (view.hydrated || subscriptions.current.has(runId)) continue;
      const controller = new AbortController();
      subscriptions.current.set(runId, controller);
      void subscribe(runId, controller);
    }
  }, [enabled, remember, update, views]);

  return { views, pending, error, reload, submit, retrySubmission, cancel };
}
export type ChatRunsController = ReturnType<typeof useChatRuns>;

function abortableDelay(ms: number, signal: AbortSignal) {
  return new Promise<void>((resolve) => {
    const done = () => { window.clearTimeout(timer); signal.removeEventListener("abort", done); resolve(); };
    const timer = window.setTimeout(done, ms);
    signal.addEventListener("abort", done, { once: true });
    if (signal.aborted) done();
  });
}
