import { useCallback, useMemo, useState } from "react";
import { deleteJson, getJson } from "../api/client";
import type { MessageDto, SessionDto, SessionResponse, SessionsResponse } from "../api/types";
import { useAsyncResource } from "./useAsyncResource";

const sessionKey = (session: SessionDto) => session.channel === "web" ? String(session.chat_id || "") : String(session.id || "");
const SESSION_PAGE_SIZE = 30;
const MESSAGE_PAGE_SIZE = 40;
const EMPTY_SESSIONS: SessionDto[] = [];

export function useSessions() {
  const loader = useCallback(
    async (signal: AbortSignal) => getJson<SessionsResponse>(
      `/api/sessions?limit=${SESSION_PAGE_SIZE}&offset=0`,
      { signal },
    ),
    [],
  );
  const resource = useAsyncResource<SessionsResponse>(loader);
  const [activeId, setActiveId] = useState("default");
  const [active, setActive] = useState<SessionDto | null>(null);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [loadingHistory, setLoadingHistory] = useState(false);
  const sessions = resource.data?.sessions || EMPTY_SESSIONS;
  const visible = useMemo(() => sessions.filter((session) => {
    const text = `${session.title || ""} ${sessionKey(session)} ${session.current_mode || ""}`.toLowerCase();
    return !filter.trim() || text.includes(filter.trim().toLowerCase());
  }), [sessions, filter]);
  const loadSession = useCallback(async (id: string, raw = false) => {
    setBusy(true);
    try {
      const data = await getJson<SessionResponse>(`/api/session?session_id=${encodeURIComponent(id)}&raw=${raw ? "1" : "0"}&limit=${MESSAGE_PAGE_SIZE}`);
      const session = data.session || {};
      setActiveId(String(session.chat_id || id)); setActive(session);
      return session;
    } finally { setBusy(false); }
  }, []);
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
        return {
          status: "success",
          data: { ...next, sessions: [...existing, ...appended] },
          error: "",
          updatedAt: Date.now(),
        };
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
      setActive((current) => {
        if (!current || String(current.chat_id || requestedId) !== requestedId) return current;
        return {
          ...current,
          messages: prependUniqueMessages(older, current.messages || []),
          message_page: data.session?.message_page,
        };
      });
      return older.length;
    } finally { setLoadingHistory(false); }
  }, [active?.message_page, activeId, loadingHistory]);
  const newSession = useCallback(() => {
    const id = `web-${Date.now().toString(36)}`;
    setActiveId(id); setActive({ chat_id: id, channel: "web", current_mode: "hybrid", messages: [] });
  }, []);
  const removeSession = useCallback(async (id: string) => {
    const data = await deleteJson<SessionsResponse>("/api/session", { session_id: id });
    resource.setState({ status: "success", data, error: "", updatedAt: Date.now() });
    if (id === activeId) {
      const next = (data.sessions || []).find((item) => item.channel === "web");
      if (next?.chat_id) await loadSession(next.chat_id); else newSession();
    }
  }, [activeId, loadSession, newSession, resource]);
  return {
    ...resource,
    data: sessions,
    visible,
    activeId,
    active,
    filter,
    setFilter,
    setActive,
    loadSession,
    loadMoreSessions,
    loadOlderMessages,
    newSession,
    removeSession,
    busy,
    loadingMore,
    loadingHistory,
    hasMore: Boolean(resource.data?.has_more),
  };
}

function prependUniqueMessages(older: MessageDto[], current: MessageDto[]) {
  const known = new Set(current.flatMap((message) => message.seq == null ? [] : [message.seq]));
  return [...older.filter((message) => message.seq == null || !known.has(message.seq)), ...current];
}

export type SessionsController = ReturnType<typeof useSessions>;
