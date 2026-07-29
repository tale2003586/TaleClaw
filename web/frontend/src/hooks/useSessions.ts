import { useCallback, useMemo, useState } from "react";
import { deleteJson, getJson } from "../api/client";
import type { SessionDto, SessionResponse, SessionsResponse } from "../api/types";
import { useAsyncResource } from "./useAsyncResource";

const sessionKey = (session: SessionDto) => session.channel === "web" ? String(session.chat_id || "") : String(session.id || "");

export function useSessions() {
  const loader = useCallback(async (signal: AbortSignal) => (await getJson<SessionsResponse>("/api/sessions", { signal })).sessions || [], []);
  const resource = useAsyncResource<SessionDto[]>(loader);
  const [activeId, setActiveId] = useState("default");
  const [active, setActive] = useState<SessionDto | null>(null);
  const [filter, setFilter] = useState("");
  const [busy, setBusy] = useState(false);
  const visible = useMemo(() => (resource.data || []).filter((session) => {
    const text = `${session.title || ""} ${sessionKey(session)} ${session.current_mode || ""}`.toLowerCase();
    return !filter.trim() || text.includes(filter.trim().toLowerCase());
  }), [resource.data, filter]);
  const loadSession = useCallback(async (id: string, raw = false) => {
    setBusy(true);
    try {
      const data = await getJson<SessionResponse>(`/api/session?session_id=${encodeURIComponent(id)}&raw=${raw ? "1" : "0"}`);
      const session = data.session || {};
      setActiveId(String(session.chat_id || id)); setActive(session);
      return session;
    } finally { setBusy(false); }
  }, []);
  const newSession = useCallback(() => {
    const id = `web-${Date.now().toString(36)}`;
    setActiveId(id); setActive({ chat_id: id, channel: "web", current_mode: "hybrid", messages: [] });
  }, []);
  const removeSession = useCallback(async (id: string) => {
    const data = await deleteJson<SessionsResponse>("/api/session", { session_id: id });
    resource.setState({ status: "success", data: data.sessions || [], error: "", updatedAt: Date.now() });
    if (id === activeId) {
      const next = (data.sessions || []).find((item) => item.channel === "web");
      if (next?.chat_id) await loadSession(next.chat_id); else newSession();
    }
  }, [activeId, loadSession, newSession, resource]);
  return { ...resource, visible, activeId, active, filter, setFilter, setActive, loadSession, newSession, removeSession, busy };
}

export type SessionsController = ReturnType<typeof useSessions>;
