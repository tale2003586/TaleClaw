import { describe, expect, it, vi } from "vitest";
import { getJson, subscribeNdjson, uploadFormData } from "../api/client";

describe("api client", () => {
  it("parses JSON and exposes server errors", async () => {
    vi.stubGlobal("fetch", vi.fn().mockResolvedValueOnce(new Response(JSON.stringify({ ok: true }), { headers: { "Content-Type": "application/json" } })).mockResolvedValueOnce(new Response("unavailable", { status: 503 })));
    await expect(getJson<{ ok: boolean }>("/ok")).resolves.toEqual({ ok: true });
    await expect(getJson("/bad")).rejects.toThrow("unavailable");
    vi.unstubAllGlobals();
  });

  it("reassembles NDJSON split across stream chunks", async () => {
    const encoder = new TextEncoder();
    const body = new ReadableStream({ start(controller) { controller.enqueue(encoder.encode('{"type":"delta","text":"a')); controller.enqueue(encoder.encode('"}\n{"type":"complete"}')); controller.close(); } });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body, { status: 200 })));
    const events: Array<Record<string, unknown>> = [];
    await subscribeNdjson("/stream", (event) => events.push(event as Record<string, unknown>));
    expect(events).toEqual([{ type: "delta", text: "a" }, { type: "complete" }]);
    vi.unstubAllGlobals();
  });

  it("cancels only a malformed subscription body and releases its reader", async () => {
    const cancelled = vi.fn();
    const body = new ReadableStream({
      start(controller) { controller.enqueue(new TextEncoder().encode("not-json\n")); },
      cancel: cancelled,
    });
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue(new Response(body)));
    await expect(subscribeNdjson("/events", () => undefined)).rejects.toThrow();
    expect(cancelled).toHaveBeenCalledTimes(1);
    expect(body.locked).toBe(false);
    vi.unstubAllGlobals();
  });

  it("does not force a JSON content type for uploads", async () => {
    const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), { headers: { "Content-Type": "application/json" } }));
    vi.stubGlobal("fetch", fetchMock);
    await uploadFormData("/upload", new FormData());
    expect(fetchMock.mock.calls[0][1].headers).toBeUndefined();
    vi.unstubAllGlobals();
  });
});
