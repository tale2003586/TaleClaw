import { useCallback, useState } from "react";
import { getJson } from "../api/client";
import type { MemoryFile, MemoryResponse } from "../api/types";
import { useAsyncResource } from "./useAsyncResource";

export function useMemory() {
  const loader = useCallback(async (signal: AbortSignal) => (await getJson<MemoryResponse>("/api/memory", { signal })).files || [], []);
  const resource = useAsyncResource<MemoryFile[]>(loader); const [requestedActive, setActive] = useState("MEMORY.md");
  const current = resource.data?.find((file) => file.name === requestedActive) || resource.data?.[0] || null;
  return { ...resource, active: current?.name || requestedActive, setActive, current };
}
