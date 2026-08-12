import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStream } from "../hooks/useChatStream";

const mocks = vi.hoisted(() => ({ postJson: vi.fn(), streamNdjson: vi.fn() }));

vi.mock("../api/client", () => mocks);

describe("useChatStream", () => {
  beforeEach(() => { mocks.streamNdjson.mockReset(); });

  it("keeps ordered segments visible while finalizing and commits without duplication", async () => {
    let assistantDone: () => void = () => undefined;
    let turnDone: () => void = () => undefined;
    const assistantGate = new Promise<void>((resolve) => { assistantDone = resolve; });
    const turnGate = new Promise<void>((resolve) => { turnDone = resolve; });
    mocks.streamNdjson.mockImplementation(async (...args: unknown[]) => {
      const onEvent = args.find((value) => typeof value === "function") as (event: Record<string, unknown>) => void;
      onEvent({ type: "thinking", text: "reasoning" });
      onEvent({ type: "delta", text: "先检查 registry。" });
      onEvent({ type: "assistant_segment", step: 1, has_content: true, final: false });
      onEvent({ type: "event", event: "tool.call.started", span_id: "tool-1", step: 1, tool: "read_file" });
      onEvent({ type: "event", event: "tool.call.completed", span_id: "tool-1", step: 1, tool: "read_file" });
      onEvent({ type: "delta", text: "发现 policy 分离。" });
      onEvent({ type: "assistant_segment", step: 2, has_content: true, final: false });
      onEvent({ type: "delta", text: "最终结论。" });
      onEvent({ type: "assistant_segment", step: 3, has_content: true, final: true });
      await assistantGate;
      onEvent({ type: "assistant_completed", step: 3, reason: "assistant_final_message" });
      await turnGate;
      const reply = "先检查 registry。\n\n发现 policy 分离。\n\n最终结论。";
      onEvent({ type: "complete", reply, session: { chat_id: "default", messages: [{ role: "assistant", content: reply }] } });
    });

    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    expect(await screen.findByTestId("stream-copy")).toHaveTextContent("先检查 registry。 发现 policy 分离。 最终结论。");
    expect(screen.getByTestId("stream-status")).toHaveTextContent("streaming");
    expect(screen.getByTestId("activity-count")).toHaveTextContent("2");

    await act(async () => assistantDone());
    await waitFor(() => expect(screen.getByTestId("stream-status")).toHaveTextContent("finalizing"));
    expect(screen.getByTestId("stream-copy")).toHaveTextContent("最终结论。");
    expect(screen.getByTestId("completed-reply")).toBeEmptyDOMElement();

    await act(async () => turnDone());
    await waitFor(() => expect(screen.queryByTestId("stream-copy")).not.toBeInTheDocument());
    expect(screen.queryByTestId("stream-thinking")).not.toBeInTheDocument();
    expect(screen.getByTestId("stream-status")).toHaveTextContent("idle");
    expect(screen.getByTestId("completed-reply")).toHaveTextContent("先检查 registry。 发现 policy 分离。 最终结论。");
    expect(screen.getByTestId("completed-reply").textContent?.match(/最终结论。/g)).toHaveLength(1);
    expect(mocks.streamNdjson.mock.calls[0][1]).toMatchObject({
      thinking_enabled: true,
      model_profile: "reasoning",
    });
  });
});

function Harness() {
  const [reply, setReply] = useState("");
  const stream = useChatStream((_session, value) => setReply(value));
  return <><button onClick={() => void stream.send("default", "hello", "", [], true, "reasoning")}>send</button><span data-testid="stream-status">{stream.status}</span><span data-testid="activity-count">{stream.activity.length}</span>{stream.text && <span data-testid="stream-copy">{stream.text}</span>}{stream.thinking && <span data-testid="stream-thinking">{stream.thinking}</span>}<span data-testid="completed-reply">{reply}</span></>;
}
