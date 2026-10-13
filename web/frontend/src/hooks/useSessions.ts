import { useCallback, useMemo, useRef, useState, type SetStateAction } from "react";
import { deleteJson, getJson } from "../api/client";
import type { MessageDto, SessionDto, SessionResponse, SessionsResponse } from "../api/types";
import { useAsyncResource } from "./useAsyncResource";

export const sessionKey = (session: SessionDto) => session.channel === "web" ? String(session.chat_id || "") : String(session.id || "");
const SESSION_PAGE_SIZE = 30;
const MESSAGE_PAGE_SIZE = 40;
const EMPTY_SESSIONS: SessionDto[] = [];

export function useSessions() {
  const loader = useCallback((signal: AbortSignal) => getJson<SessionsResponse>(`/api/sessions?limit=${SESSION_PAGE_SIZE}&offset=0`, { signal }), []);
  const resource = useAsyncResource<SessionsResponse>(loader);
  const [activeId, setActiveId] = useState("default");
  const activeIdRef = useRef("default");
  const selectionRequest = useRef(0);
  const [cache, setCache] = useState<Record<string, SessionDto>>({});
  const generations = useRef<Record<string, number>>({});
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const active = cache[activeId] || null;
  const sessions = resource.data?.sessions || EMPTY_SESSIONS;
  const visible = useMemo(() => sessions.filter((session) => {
    const text = `${session.title || ""} ${sessionKey(session)} ${session.active_agent || ""}`.toLowerCase();
    return !filter.trim() || text.includes(filter.trim().toLowerCase());
  }), [sessions, filter]);
  const mergeSession = useCallback((id: string, incoming: SessionDto) => {
    if (incoming.channel === "web" && incoming.chat_id && incoming.chat_id !== id) return;
    generations.current[id] = (generations.current[id] || 0) + 1;
    setCache((current) => ({ ...current, [id]: mergeSessionMessages(current[id], incoming) }));
  }, []);
  const setActive = useCallback((value: SetStateAction<SessionDto | null>) => {
    const id = activeIdRef.current;
    setCache((current) => {
      const next = typeof value === "function" ? value(current[id] || null) : value;
      if (!next) { const copy = { ...current }; delete copy[id]; return copy; }
      return { ...current, [id]: next };
    });
  }, []);
  const loadSession = useCallback(async (id: string, raw = false) => {
    const selection = ++selectionRequest.current;
    activeIdRef.current = id;
    setActiveId(id);
    setBusy(true);
    setCache((current) => current[id] ? current : { ...current, [id]: { chat_id: id, channel: raw ? "raw" : "web", messages: [] } });
    const generation = (generations.current[id] || 0) + 1;
    generations.current[id] = generation;
    try {
      const data = await getJson<SessionResponse>(`/api/session?session_id=${encodeURIComponent(id)}&raw=${raw ? "1" : "0"}&limit=${MESSAGE_PAGE_SIZE}`);
      const session = data.session || {};
      if (generations.current[id] === generation) mergeSession(id, session);
      return session;
    } finally { if (selectionRequest.current === selection) setBusy(false); }
  }, [mergeSession]);
  const loadMoreSessions = useCallback(async () => {
    const page = resource.data;
    if (loadingMore || !page?.has_more) return;
    setLoadingMore(true);
    try {
      const offset = page.next_offset ?? page.sessions.length;
      const next = await getJson<SessionsResponse>(`/api/sessions?limit=${SESSION_PAGE_SIZE}&offset=${offset}`);
      resource.setState((current) => {
        const existing = current.data?.sessions || [];
        const known = new Set(existing.map(sessionKey));
        const appended = (next.sessions || []).filter((session) => !known.has(sessionKey(session)));
        return { status: "success", data: { ...next, sessions: [...existing, ...appended] }, error: "", updatedAt: Date.now() };
      });
    } finally { setLoadingMore(false); }
  }, [loadingMore, resource]);
  const loadOlderMessages = useCallback(async () => {
    const cursor = active?.message_page?.next_before;
    if (loadingHistory || !active?.message_page?.has_more || cursor == null) return 0;
    const requestedId = activeId;
    setLoadingHistory(true);
    try {
      const data = await getJson<SessionResponse>(`/api/session?session_id=${encodeURIComponent(requestedId)}&limit=${MESSAGE_PAGE_SIZE}&before=${cursor}`);
      const older = data.session?.messages || [];
      setCache((current) => ({
        ...current,
        [requestedId]: { ...current[requestedId], messages: prependUniqueMessages(older, current[requestedId]?.messages || []), message_page: data.session?.message_page },
      }));
      return older.length;
    } finally { setLoadingHistory(false); }
  }, [active?.message_page, activeId, loadingHistory]);
  const newSession = useCallback(() => {
    const id = `web-${typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`}`;
    activeIdRef.current = id;
    setActiveId(id);
    selectionRequest.current += 1;
    setBusy(false);
    setCache((current) => ({ ...current, [id]: { chat_id: id, channel: "web", active_agent: "hybrid", messages: [] } }));
  }, []);
  const removeSession = useCallback(async (id: string) => {
    const data = await deleteJson<SessionsResponse>("/api/session", { session_id: id });
    resource.setState({ status: "success", data, error: "", updatedAt: Date.now() });
    setCache((current) => { const next = { ...current }; delete next[id]; return next; });
    if (id === activeIdRef.current) {
      const next = (data.sessions || []).find((item) => item.channel === "web");
      if (next?.chat_id) await loadSession(next.chat_id); else newSession();
    }
  }, [loadSession, newSession, resource]);
  return { ...resource, data: sessions, visible, cache, activeId, active, filter, setFilter, setActive, mergeSession, loadSession, loadMoreSessions, loadOlderMessages, newSession, removeSession, busy, loadingMore, loadingHistory, hasMore: Boolean(resource.data?.has_more) };
}
function prependUniqueMessages(older: MessageDto[], current: MessageDto[]) {
  const known = new Set(current.flatMap((message) => message.seq == null ? [] : [message.seq]));
  return [...older.filter((message) => message.seq == null || !known.has(message.seq)), ...current];
}
function mergeSessionMessages(current: SessionDto | undefined, incoming: SessionDto) {
  if (!current || !incoming.messages) return { ...current, ...incoming };
  const existing = current.messages || [], next = incoming.messages;
  if (![...existing, ...next].some((message) => message.seq != null)) return incoming;
  const bySequence = new Map<number, MessageDto>();
  for (const message of [...existing, ...next]) if (message.seq != null) bySequence.set(message.seq, message);
  return { ...current, ...incoming, messages: [...bySequence.entries()].sort(([a], [b]) => a - b).map(([, message]) => message), message_page: existing.length > next.length ? current.message_page : incoming.message_page };
}
export type SessionsController = ReturnType<typeof useSessions>;
