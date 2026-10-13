import { act, renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useSessions } from "../hooks/useSessions";
import type { SessionResponse } from "../api/types";
const mocks = vi.hoisted(() => ({ getJson: vi.fn(), deleteJson: vi.fn() }));
vi.mock("../api/client", () => mocks);
beforeEach(() => { vi.clearAllMocks(); });

describe("session cache ownership", () => {
  it("does not reactivate a slow old selection or merge completed A into B", async () => {
    let finishA: (value: SessionResponse) => void = () => undefined;
    mocks.getJson.mockImplementation(async (path: string) => {
      if (path.startsWith("/api/sessions")) return { sessions: [] };
      if (path.includes("session_id=A")) return new Promise<SessionResponse>((resolve) => { finishA = resolve; });
      return { session: { chat_id: "B", channel: "web", messages: [{ seq: 1, role: "assistant", content: "B history" }] } };
    });
    const { result } = renderHook(() => useSessions());
    let loadA: Promise<unknown>;
    act(() => { loadA = result.current.loadSession("A"); });
    await act(async () => { await result.current.loadSession("B"); });
    await act(async () => { finishA({ session: { chat_id: "A", channel: "web", messages: [{ seq: 1, role: "assistant", content: "A history" }] } }); await loadA!; });
    expect(result.current.activeId).toBe("B");
    expect(result.current.active?.messages?.[0].content).toBe("B history");
    act(() => result.current.mergeSession("A", { chat_id: "A", channel: "web", messages: [{ seq: 1, role: "assistant", content: "A complete" }] }));
    expect(result.current.active?.messages?.[0].content).toBe("B history");
    expect(result.current.cache.A.messages?.[0].content).toBe("A complete");
  });

  it("does not let a pre-completion response overwrite newer completed session data", async () => {
    let finish: (value: SessionResponse) => void = () => undefined;
    mocks.getJson.mockImplementation(async (path: string) => path.startsWith("/api/sessions") ? { sessions: [] } : new Promise<SessionResponse>((resolve) => { finish = resolve; }));
    const { result } = renderHook(() => useSessions());
    await waitFor(() => expect(result.current.status).toBe("success"));
    let loading: Promise<unknown>;
    act(() => { loading = result.current.loadSession("A"); });
    act(() => result.current.mergeSession("A", { chat_id: "A", channel: "web", active_agent: "coding", messages: [{ seq: 4, role: "assistant", content: "new reply" }] }));
    await act(async () => { finish({ session: { chat_id: "A", channel: "web", active_agent: "hybrid", messages: [] } }); await loading!; });
    expect(result.current.active?.active_agent).toBe("coding");
    expect(result.current.active?.messages?.[0].content).toBe("new reply");
  });
});
