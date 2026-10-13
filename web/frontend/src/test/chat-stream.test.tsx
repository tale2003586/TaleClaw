import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ChatRunDto, ChatRunEvent } from "../api/types";
import { ApiError } from "../api/client";
import { useChatRuns } from "../hooks/useChatRuns";

const mocks = vi.hoisted(() => ({ postJson: vi.fn(), getJson: vi.fn(), subscribeNdjson: vi.fn() }));
vi.mock("../api/client", async (original) => ({ ...await original<typeof import("../api/client")>(), ...mocks }));
const dto = (id: string, session = id, status: ChatRunDto["status"] = "running", lastSeq = 0): ChatRunDto => ({ run_id: id, session_id: session, status, created_at: "2026-10-03T00:00:00Z", updated_at: "2026-10-03T00:00:00Z", last_seq: lastSeq });
type Feed = { emit: (event: Partial<ChatRunEvent> & { type: string; seq: number }) => void; close: () => void; signal: AbortSignal };
let feeds: Record<string, Feed>;
let records: Record<string, ChatRunDto>;

beforeEach(() => {
  vi.clearAllMocks();
  feeds = {}; records = {};
  mocks.getJson.mockImplementation(async (path: string) => {
    if (path === "/api/chat/runs") return { runs: [] };
    if (path.startsWith("/api/session?")) return { session: { chat_id: new URL(path, "http://test").searchParams.get("session_id"), channel: "web", messages: [{ role: "assistant", content: "saved reply" }] } };
    return { run: records[path.split("/").pop()!] };
  });
  mocks.postJson.mockImplementation(async (_path: string, request: { session_id: string }) => {
    const run = dto("run-" + request.session_id, request.session_id);
    records[run.run_id] = run;
    return { run };
  });
  mocks.subscribeNdjson.mockImplementation((path: string, emit: (event: ChatRunEvent) => void, signal: AbortSignal) => new Promise<void>((resolve) => {
    const id = path.split("/")[4];
    feeds[id] = { emit: (event) => emit({ run_id: id, session_id: records[id]?.session_id || id, ...event } as ChatRunEvent), close: resolve, signal };
    signal.addEventListener("abort", () => resolve(), { once: true });
  }));
});
afterEach(() => { vi.useRealTimers(); });

describe("background chat runs", () => {
  it("streams two sessions independently and completes the source session with no duplicate segments", async () => {
    const complete = vi.fn();
    const { result } = renderHook(() => useChatRuns(complete));
    await act(async () => { await result.current.submit({ session_id: "A", message: "a" }); await result.current.submit({ session_id: "B", message: "b" }); });
    await waitFor(() => expect(Object.keys(feeds)).toHaveLength(2));
    act(() => {
      feeds["run-A"].emit({ type: "delta", seq: 1, text: "first" });
      feeds["run-A"].emit({ type: "assistant_segment", seq: 2, has_content: true, final: false });
      feeds["run-A"].emit({ type: "delta", seq: 3, text: "second" });
      feeds["run-A"].emit({ type: "delta", seq: 3, text: "duplicate" });
      feeds["run-A"].emit({ type: "delta", seq: 4, text: "wrong", session_id: "B" });
      feeds["run-B"].emit({ type: "thinking", seq: 1, text: "B thinking" });
    });
    expect(result.current.views["run-A"].text).toBe("first\n\nsecond");
    expect(result.current.views["run-A"].cursor).toBe(3);
    expect(result.current.views["run-B"].thinking).toBe("B thinking");
    await act(async () => {
      feeds["run-A"].emit({ type: "complete", seq: 4, reply: "A done", session: { chat_id: "A", channel: "web", messages: [{ role: "assistant", content: "A done" }] } });
      records["run-A"] = dto("run-A", "A", "completed", 4);
      feeds["run-A"].close();
    });
    await waitFor(() => expect(result.current.views["run-A"].hydrated).toBe(true));
    expect(complete).toHaveBeenCalledTimes(1);
    expect(complete.mock.calls[0][0]).toBe("A");
    expect(result.current.views["run-B"].run.status).toBe("running");
    expect(feeds["run-B"].signal.aborted).toBe(false);
  });

  it("reconnects from consumed sequence and ignores replay duplicates", async () => {
    vi.useFakeTimers();
    const complete = vi.fn();
    let attempts = 0;
    mocks.subscribeNdjson.mockImplementation(async (path: string, emit: (event: ChatRunEvent) => void) => {
      attempts += 1;
      const event = (seq: number, text: string): ChatRunEvent => ({ run_id: "run-A", session_id: "A", type: "delta", seq, text });
      if (attempts === 1) { emit(event(1, "one")); throw new Error("offline"); }
      expect(path).toMatch(/after=1$/);
      emit(event(1, "duplicate"));
      emit(event(2, "two"));
      records["run-A"] = dto("run-A", "A", "completed", 2);
    });
    const { result } = renderHook(() => useChatRuns(complete));
    await act(async () => { await result.current.submit({ session_id: "A", message: "a" }); });
    expect(result.current.views["run-A"].cursor).toBe(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1000); });
    expect(result.current.views["run-A"].text).toBe("onetwo");
    expect(result.current.views["run-A"].hydrated).toBe(true);
    expect(complete).toHaveBeenCalledWith("A", expect.objectContaining({ chat_id: "A" }));
  });

  it("restores a running task from the server and replays from zero rather than last_seq", async () => {
    records.A = dto("A", "A", "running", 9);
    mocks.getJson.mockImplementation(async (path: string) => path === "/api/chat/runs" ? { runs: [records.A] } : { run: records.A });
    const { result } = renderHook(() => useChatRuns(vi.fn()));
    await waitFor(() => expect(feeds.A).toBeDefined());
    expect(mocks.subscribeNdjson.mock.calls[0][0]).toBe("/api/chat/runs/A/events?after=0");
    act(() => feeds.A.emit({ type: "delta", seq: 1, text: "restored" }));
    expect(result.current.views.A.text).toBe("restored");
    expect(result.current.views.A.cursor).toBe(1);
  });

  it("unmounting aborts only subscriptions and does not cancel server jobs", async () => {
    const { result, unmount } = renderHook(() => useChatRuns(vi.fn()));
    await act(async () => { await result.current.submit({ session_id: "A", message: "a" }); });
    await waitFor(() => expect(feeds["run-A"]).toBeDefined());
    unmount();
    expect(feeds["run-A"].signal.aborted).toBe(true);
    expect(mocks.postJson.mock.calls.map(([path]) => path)).toEqual(["/api/chat/runs"]);
  });

  it("cancels the specified run while another session keeps running", async () => {
    const { result } = renderHook(() => useChatRuns(vi.fn()));
    await act(async () => { await result.current.submit({ session_id: "A", message: "a" }); await result.current.submit({ session_id: "B", message: "b" }); });
    mocks.postJson.mockResolvedValueOnce({ run: dto("run-A", "A", "cancelling") });
    await act(async () => { await result.current.cancel("run-A"); });
    expect(mocks.postJson).toHaveBeenLastCalledWith("/api/chat/runs/run-A/cancel", {});
    expect(result.current.views["run-A"].run.status).toBe("cancelling");
    expect(result.current.views["run-B"].run.status).toBe("running");
  });

  it("retains an idempotency key across uncertain submission and prevents a second intent", async () => {
    mocks.postJson.mockRejectedValueOnce(new TypeError("network lost"));
    const { result } = renderHook(() => useChatRuns(vi.fn()));
    await act(async () => { await expect(result.current.submit({ session_id: "A", message: "original" })).rejects.toThrow("network lost"); });
    const original = mocks.postJson.mock.calls[0][1];
    expect(original.request_id).toBeTruthy();
    await act(async () => { await expect(result.current.submit({ session_id: "A", message: "different" })).rejects.toThrow("尚待确认"); });
    expect(mocks.postJson).toHaveBeenCalledTimes(1);
    await act(async () => { await result.current.retrySubmission("A"); });
    expect(mocks.postJson.mock.calls[1][1]).toEqual(original);
    expect(result.current.pending.A).toBeUndefined();
  });

  it("releases an explicitly rejected submission and does not optimistically cancel on failure", async () => {
    mocks.postJson.mockRejectedValueOnce(new ApiError("already running", 409));
    const { result } = renderHook(() => useChatRuns(vi.fn()));
    await act(async () => { await expect(result.current.submit({ session_id: "A", message: "a" })).rejects.toThrow("already running"); });
    expect(result.current.pending.A).toBeUndefined();
    await act(async () => { await result.current.submit({ session_id: "B", message: "b" }); });
    mocks.postJson.mockRejectedValueOnce(new ApiError("forbidden", 403));
    await act(async () => { await result.current.cancel("run-B"); });
    expect(result.current.views["run-B"].run.status).toBe("running");
    expect(result.current.views["run-B"].connectionError).toContain("停止失败");
  });
});
