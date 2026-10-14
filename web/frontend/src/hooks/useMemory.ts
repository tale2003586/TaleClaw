import { useCallback, useEffect, useState } from "react";
import { getJson } from "../api/client";
import type { LegacyMemoryResponse, MemoryResponse } from "../api/types";
import { useAsyncResource } from "./useAsyncResource";

const PAGE_SIZE = 200;

export function useMemory(sessionId = "", query = "", status = "", kind = "") {
  const normalizedQuery = query.trim();
  const [debouncedQuery, setDebouncedQuery] = useState(normalizedQuery);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebouncedQuery(normalizedQuery), 275);
    return () => window.clearTimeout(timer);
  }, [normalizedQuery]);
  const scope = JSON.stringify([sessionId, normalizedQuery, status, kind]);
  const requestScope = JSON.stringify([sessionId, debouncedQuery, status, kind]);
  const [page, setPage] = useState({ scope, offset: 0 });
  const offset = page.scope === scope ? page.offset : 0;
  const loader = useCallback(async (signal: AbortSignal) => {
    const params = new URLSearchParams({ limit: String(PAGE_SIZE), offset: String(offset) });
    if (sessionId) params.set("session_id", sessionId);
    if (debouncedQuery) params.set("query", debouncedQuery);
    if (status) params.set("status", status);
    if (kind) params.set("kind", kind);
    return { scope: requestScope, offset, response: await getJson<MemoryResponse>(`/api/memory?${params}`, { signal }) };
  }, [sessionId, offset, debouncedQuery, status, kind, requestScope]);
  const waitingForQuery = normalizedQuery !== debouncedQuery;
  const resource = useAsyncResource(loader, !waitingForQuery);
  // Never render an earlier context under a newly selected session or page.
  const response = resource.data?.response;
  const identityMatches = response?.filters.query === normalizedQuery && response.filters.status === status && response.filters.kind === kind;
  const data = resource.data?.scope === scope && resource.data.offset === offset && identityMatches ? response : null;
  // If an action removed the last matching row, return to a populated page.
  useEffect(() => {
    if (data?.available && data.items.length === 0 && offset > 0) {
      const previous = Math.max(0, Math.ceil(data.filtered_total / PAGE_SIZE) - 1) * PAGE_SIZE;
      const timer = window.setTimeout(() => setPage({ scope, offset: Math.min(offset - PAGE_SIZE, previous) }), 0);
      return () => window.clearTimeout(timer);
    }
  }, [data, offset, scope]);
  const [active, setActive] = useState("");
  const current = data?.items.find((item) => item.id === active) || data?.items[0] || null;
  const goToPage = (nextOffset: number) => setPage({ scope, offset: Math.max(0, nextOffset) });
  return { ...resource, status: waitingForQuery ? "loading" as const : resource.status, data, current, active, setActive, offset, goToPage, pageSize: PAGE_SIZE };
}

export function useLegacyMemory() {
  const loader = useCallback(async (signal: AbortSignal) =>
    (await getJson<LegacyMemoryResponse>("/api/memory/legacy", { signal })).files.filter((file) => file.content.trim()), []);
  return useAsyncResource(loader, false);
}
