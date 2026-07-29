import { useCallback, useEffect, useRef, useState } from "react";
import type { AsyncState } from "../api/types";

export const idleState = <T,>(): AsyncState<T> => ({ status: "idle", data: null, error: "", updatedAt: null });

export function useAsyncResource<T>(loader: (signal: AbortSignal) => Promise<T>, auto = true) {
  const [state, setState] = useState<AsyncState<T>>(idleState);
  const controller = useRef<AbortController | null>(null);
  const load = useCallback(async () => {
    controller.current?.abort();
    const next = new AbortController(); controller.current = next;
    setState((current) => ({ ...current, status: "loading", error: "" }));
    try {
      const data = await loader(next.signal);
      if (!next.signal.aborted) setState({ status: "success", data, error: "", updatedAt: Date.now() });
      return data;
    } catch (error) {
      if (!next.signal.aborted) setState((current) => ({ ...current, status: "error", error: error instanceof Error ? error.message : String(error) }));
      throw error;
    }
  }, [loader]);
  useEffect(() => { if (auto) void load().catch(() => undefined); return () => controller.current?.abort(); }, [auto, load]);
  return { ...state, reload: load, setState };
}
