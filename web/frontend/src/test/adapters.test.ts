import { describe, expect, it } from "vitest";
import { adaptRunDetail, adaptRunSummary, deriveLogRows, groupTrace } from "../api/adapters";

describe("run adapters", () => {
  it("normalizes missing and incorrectly typed summary values", () => {
    expect(adaptRunSummary({ run_id: 42, status: null, reasoning_steps: "3" })).toMatchObject({
      runId: "42", status: "unknown", reasoningSteps: 3, modelCalls: 0,
    });
  });

  it("keeps hostile trace text as inert data", () => {
    const detail = adaptRunDetail({ run_id: "run-1", events: [{ event: "tool.failed", payload: { error_message: "<img src=x onerror=alert(1)>" } }] });
    const rows = deriveLogRows(detail);
    expect(rows[0].level).toBe("error");
    expect(rows[0].message).toContain("<img");
  });

  it("groups step events while retaining run-level events", () => {
    const detail = adaptRunDetail({ events: [{ event: "run.started" }, { event: "model.called", step: 2 }, { event: "tool.called", payload: { step: 2 } }] });
    const grouped = groupTrace(detail);
    expect(grouped.steps).toHaveLength(1);
    expect(grouped.steps[0].events).toHaveLength(2);
    expect(grouped.runEvents[0].event).toBe("run.started");
  });

  it("adapts grouped subagent metrics and events into trace records", () => {
    const detail = adaptRunDetail({
      run_id: "run-live",
      subagents: [{
        session_id: "subtask:explore:1",
        description: "inspect runtime",
        event_count: 2,
        reasoning_steps: 3,
        events: [{ event: "tool.call.completed", step: 2, payload: { tool_name: "read_file" } }],
      }],
    });
    expect(detail.subagents[0]).toMatchObject({
      sessionId: "subtask:explore:1",
      description: "inspect runtime",
      eventCount: 2,
      reasoningSteps: 3,
    });
    expect(detail.subagents[0].events[0]).toMatchObject({
      runId: "run-live",
      sessionId: "subtask:explore:1",
      event: "tool.call.completed",
      step: 2,
    });
  });
});
