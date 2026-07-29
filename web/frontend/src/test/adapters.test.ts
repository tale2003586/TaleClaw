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
});
