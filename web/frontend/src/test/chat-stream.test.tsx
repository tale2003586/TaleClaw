import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { useState } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { useChatStream } from "../hooks/useChatStream";

const mocks = vi.hoisted(() => ({ postJson: vi.fn(), streamNdjson: vi.fn() }));

vi.mock("../api/client", () => mocks);

describe("useChatStream", () => {
  beforeEach(() => { mocks.streamNdjson.mockReset(); });

  it("renders deltas while streaming and clears the transient copy after completion", async () => {
    let finish: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => { finish = resolve; });
    mocks.streamNdjson.mockImplementation(async (...args: unknown[]) => {
      const onEvent = args.find((value) => typeof value === "function") as (event: Record<string, unknown>) => void;
      onEvent({ type: "delta", text: "streamed text" });
      await gate;
      onEvent({ type: "complete", reply: "streamed text", session: { chat_id: "default", messages: [{ role: "assistant", content: "streamed text" }] } });
    });

    render(<Harness />);
    fireEvent.click(screen.getByRole("button", { name: "send" }));
    expect(await screen.findByText("streamed text")).toBeInTheDocument();

    await act(async () => finish());
    await waitFor(() => expect(screen.queryByTestId("stream-copy")).not.toBeInTheDocument());
    expect(screen.getByTestId("completed-reply")).toHaveTextContent("streamed text");
  });
});

function Harness() {
  const [reply, setReply] = useState("");
  const stream = useChatStream((_session, value) => setReply(value));
  return <><button onClick={() => void stream.send("default", "hello")}>send</button>{stream.text && <span data-testid="stream-copy">{stream.text}</span>}<span data-testid="completed-reply">{reply}</span></>;
}
