import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { getJson } from "../api/client";
import { adaptRunDetail, adaptRunSummary } from "../api/adapters";
import type { AsyncState, RunDetail, RunDetailDto, RunSummary, RunSummaryDto } from "../api/types";
import { idleState } from "./useAsyncResource";

interface RunsContextValue {
  runs: AsyncState<RunSummary[]>;
  activeRunId: string;
  activeDetail: AsyncState<RunDetail>;
  live: boolean;
  pollIntervalMs: number;
  loadRuns(force?: boolean, silent?: boolean): Promise<RunSummary[]>;
  selectRun(runId: string, force?: boolean, silent?: boolean): Promise<RunDetail>;
  refreshActiveRun(): Promise<void>;
}

const RunsContext = createContext<RunsContextValue | null>(null);
const LIVE_POLL_INTERVAL_MS = 2000;
const LIVE_RUN_MAX_AGE_MS = 24 * 60 * 60 * 1000;

export function RunsProvider({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const [runs, setRuns] = useState<AsyncState<RunSummary[]>>(idleState);
  const [activeRunId, setActiveRunId] = useState("");
  const [activeDetail, setActiveDetail] = useState<AsyncState<RunDetail>>(idleState);
  const cache = useRef(new Map<string, RunDetail>());
  const pendingDetails = useRef(new Map<string, Promise<RunDetail>>());
  const runsRef = useRef(runs); runsRef.current = runs;
  const activeRunIdRef = useRef(activeRunId); activeRunIdRef.current = activeRunId;
  const loadRuns = useCallback(async (force = false, silent = false) => {
    if (!enabled) return [];
    if (!force && runsRef.current.status === "success" && runsRef.current.data) return runsRef.current.data;
    if (!silent) setRuns((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const response = await getJson<{ runs?: RunSummaryDto[] }>("/api/runs", { cache: "no-store" });
      const data = (response.runs || []).map(adaptRunSummary);
      setRuns({ status: "success", data, error: "", updatedAt: Date.now() });
      const selected = data.some((run) => run.runId === activeRunIdRef.current)
        ? activeRunIdRef.current
        : data.find(isLiveRun)?.runId || data[0]?.runId || "";
      activeRunIdRef.current = selected; setActiveRunId(selected);
      return data;
    } catch (error) {
      if (!silent) setRuns((current) => ({ ...current, status: "error", error: error instanceof Error ? error.message : String(error) }));
      throw error;
    }
  }, [enabled]);
  const selectRun = useCallback(async (runId: string, force = false, silent = false) => {
    activeRunIdRef.current = runId; setActiveRunId(runId);
    if (!force && cache.current.has(runId)) {
      const detail = cache.current.get(runId)!;
      setActiveDetail({ status: "success", data: detail, error: "", updatedAt: Date.now() });
      return detail;
    }
    if (!force && pendingDetails.current.has(runId)) {
      const detail = await pendingDetails.current.get(runId)!;
      setActiveDetail({ status: "success", data: detail, error: "", updatedAt: Date.now() });
      return detail;
    }
    if (!silent) setActiveDetail((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const request = getJson<RunDetailDto>(`/api/run?run_id=${encodeURIComponent(runId)}`, { cache: "no-store" }).then(adaptRunDetail);
      pendingDetails.current.set(runId, request);
      const detail = await request; cache.current.set(runId, detail);
      if (activeRunIdRef.current === runId) setActiveDetail({ status: "success", data: detail, error: "", updatedAt: Date.now() });
      return detail;
    } catch (error) {
      if (!silent && activeRunIdRef.current === runId) setActiveDetail((current) => ({ ...current, status: "error", error: error instanceof Error ? error.message : String(error) }));
      throw error;
    } finally {
      pendingDetails.current.delete(runId);
    }
  }, []);
  const refreshActiveRun = useCallback(async () => { if (activeRunId) await selectRun(activeRunId, true); }, [activeRunId, selectRun]);
  const live = Boolean(
    runs.data?.some(isLiveRun)
    || isLiveDetail(activeDetail.data)
  );
  const activeIsRunning = Boolean(
    isLiveRun(runs.data?.find((run) => run.runId === activeRunId))
    || isLiveDetail(activeDetail.data)
  );
  useEffect(() => {
    if (!enabled || !live) return;
    let polling = false;
    const poll = async () => {
      if (polling) return;
      polling = true;
      try {
        const items = await loadRuns(true, true);
        const selected = activeRunIdRef.current;
        const selectedStillExists = items.some((run) => run.runId === selected);
        const id = selectedStillExists ? selected : items[0]?.runId || "";
        const selectedIsRunning = isLiveRun(items.find((run) => run.runId === id));
        if (id && (activeIsRunning || selectedIsRunning)) await selectRun(id, true, true);
      } catch {
        // Keep the latest successful snapshot visible and retry on the next tick.
      } finally {
        polling = false;
      }
    };
    const timer = window.setInterval(() => void poll(), LIVE_POLL_INTERVAL_MS);
    return () => window.clearInterval(timer);
  }, [activeIsRunning, enabled, live, loadRuns, selectRun]);
  const value = useMemo(() => ({ runs, activeRunId, activeDetail, live, pollIntervalMs: LIVE_POLL_INTERVAL_MS, loadRuns, selectRun, refreshActiveRun }), [runs, activeRunId, activeDetail, live, loadRuns, selectRun, refreshActiveRun]);
  return <RunsContext.Provider value={value}>{children}</RunsContext.Provider>;
}

export function useRuns() {
  const value = useContext(RunsContext);
  if (!value) throw new Error("RunsProvider is unavailable");
  return value;
}

function isLiveRun(run: RunSummary | undefined) {
  if (!run || run.status.toLowerCase() !== "running") return false;
  return isRecentTimestamp(run.startedAt);
}

function isLiveDetail(detail: RunDetail | null | undefined) {
  if (!detail || String(detail.runState.status || "").toLowerCase() !== "running") return false;
  return isRecentTimestamp(String(detail.runState.started_at || ""));
}

function isRecentTimestamp(value: string) {
  const timestamp = new Date(value).getTime();
  return !Number.isFinite(timestamp) || Date.now() - timestamp <= LIVE_RUN_MAX_AGE_MS;
}
