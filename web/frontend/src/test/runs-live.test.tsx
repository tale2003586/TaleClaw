import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { RunsProvider, useRuns } from "../hooks/useRuns";

const mocks = vi.hoisted(() => ({ getJson: vi.fn() }));
vi.mock("../api/client", () => mocks);

describe("live Runs polling", () => {
  let initialStatus = "running";
  let terminalStatus = "completed";
  beforeEach(() => {
    vi.useFakeTimers();
    initialStatus = "running";
    terminalStatus = "completed";
    let listCalls = 0;
    let detailCalls = 0;
    mocks.getJson.mockReset();
    mocks.getJson.mockImplementation(async (path: string) => {
      if (path === "/api/runs") {
        listCalls += 1;
        return { runs: [{ run_id: "run-live", status: listCalls === 1 ? initialStatus : terminalStatus }] };
      }
      if (path.startsWith("/api/run?")) {
        detailCalls += 1;
        const completed = detailCalls > 1;
        return {
          run_id: "run-live",
          run_state: { status: completed ? terminalStatus : initialStatus },
          events: Array.from({ length: completed ? 2 : 1 }, (_, index) => ({ event: `event.${index}` })),
        };
      }
      return {};
    });
  });

  afterEach(() => vi.useRealTimers());

  it.each([["running", "completed"], ["queued", "completed"], ["cancelling", "stopped"]])("silently refreshes a %s run until %s", async (initial, terminal) => {
    initialStatus = initial;
    terminalStatus = terminal;
    render(<RunsProvider enabled><Harness /></RunsProvider>);
    await act(async () => { fireEvent.click(screen.getByRole("button", { name: "load" })); });
    expect(screen.getByTestId("run-status")).toHaveTextContent(initial);
    expect(screen.getByTestId("event-count")).toHaveTextContent("1");
    expect(screen.getByTestId("live")).toHaveTextContent("yes");

    await act(async () => { await vi.advanceTimersByTimeAsync(2000); });

    expect(screen.getByTestId("run-status")).toHaveTextContent(terminal);
    expect(screen.getByTestId("event-count")).toHaveTextContent("2");
    expect(screen.getByTestId("live")).toHaveTextContent("no");
    expect(mocks.getJson).toHaveBeenCalledWith("/api/runs", { cache: "no-store" });
    const callsAtCompletion = mocks.getJson.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
    expect(mocks.getJson).toHaveBeenCalledTimes(callsAtCompletion);
  });
});

function Harness() {
  const controller = useRuns();
  const load = async () => {
    const items = await controller.loadRuns();
    if (items[0]) await controller.selectRun(items[0].runId);
  };
  return <>
    <button onClick={() => void load()}>load</button>
    <span data-testid="run-status">{String(controller.activeDetail.data?.runState.status || "")}</span>
    <span data-testid="event-count">{controller.activeDetail.data?.events.length || 0}</span>
    <span data-testid="live">{controller.live ? "yes" : "no"}</span>
  </>;
}
