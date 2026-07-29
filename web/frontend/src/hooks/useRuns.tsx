import { createContext, useCallback, useContext, useMemo, useRef, useState, type ReactNode } from "react";
import { getJson } from "../api/client";
import { adaptRunDetail, adaptRunSummary } from "../api/adapters";
import type { AsyncState, RunDetail, RunDetailDto, RunSummary, RunSummaryDto } from "../api/types";
import { idleState } from "./useAsyncResource";

interface RunsContextValue {
  runs: AsyncState<RunSummary[]>;
  activeRunId: string;
  activeDetail: AsyncState<RunDetail>;
  loadRuns(force?: boolean): Promise<RunSummary[]>;
  selectRun(runId: string, force?: boolean): Promise<RunDetail>;
  refreshActiveRun(): Promise<void>;
}

const RunsContext = createContext<RunsContextValue | null>(null);

export function RunsProvider({ enabled, children }: { enabled: boolean; children: ReactNode }) {
  const [runs, setRuns] = useState<AsyncState<RunSummary[]>>(idleState);
  const [activeRunId, setActiveRunId] = useState("");
  const [activeDetail, setActiveDetail] = useState<AsyncState<RunDetail>>(idleState);
  const cache = useRef(new Map<string, RunDetail>());
  const pendingDetails = useRef(new Map<string, Promise<RunDetail>>());
  const runsRef = useRef(runs); runsRef.current = runs;
  const activeRunIdRef = useRef(activeRunId); activeRunIdRef.current = activeRunId;
  const loadRuns = useCallback(async (force = false) => {
    if (!enabled) return [];
    if (!force && runsRef.current.status === "success" && runsRef.current.data) return runsRef.current.data;
    setRuns((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const response = await getJson<{ runs?: RunSummaryDto[] }>("/api/runs");
      const data = (response.runs || []).map(adaptRunSummary);
      setRuns({ status: "success", data, error: "", updatedAt: Date.now() });
      const selected = data.some((run) => run.runId === activeRunIdRef.current) ? activeRunIdRef.current : data[0]?.runId || "";
      setActiveRunId(selected);
      return data;
    } catch (error) {
      setRuns((current) => ({ ...current, status: "error", error: error instanceof Error ? error.message : String(error) }));
      throw error;
    }
  }, [enabled]);
  const selectRun = useCallback(async (runId: string, force = false) => {
    setActiveRunId(runId);
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
    setActiveDetail((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const request = getJson<RunDetailDto>(`/api/run?run_id=${encodeURIComponent(runId)}`).then(adaptRunDetail);
      pendingDetails.current.set(runId, request);
      const detail = await request; cache.current.set(runId, detail);
      setActiveDetail({ status: "success", data: detail, error: "", updatedAt: Date.now() });
      return detail;
    } catch (error) {
      setActiveDetail((current) => ({ ...current, status: "error", error: error instanceof Error ? error.message : String(error) }));
      throw error;
    } finally {
      pendingDetails.current.delete(runId);
    }
  }, []);
  const refreshActiveRun = useCallback(async () => { if (activeRunId) await selectRun(activeRunId, true); }, [activeRunId, selectRun]);
  const value = useMemo(() => ({ runs, activeRunId, activeDetail, loadRuns, selectRun, refreshActiveRun }), [runs, activeRunId, activeDetail, loadRuns, selectRun, refreshActiveRun]);
  return <RunsContext.Provider value={value}>{children}</RunsContext.Provider>;
}

export function useRuns() {
  const value = useContext(RunsContext);
  if (!value) throw new Error("RunsProvider is unavailable");
  return value;
}
