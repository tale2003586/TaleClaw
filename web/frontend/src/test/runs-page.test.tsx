import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import { adaptRunDetail } from "../api/adapters";
import RunsPage from "../pages/RunsPage";

const detail = adaptRunDetail({
  run_id: "run-live",
  run_state: { status: "running", started_at: "2026-07-30T02:00:00Z" },
  subagents: [{
    session_id: "subtask:explore:1",
    agent_type: "explore",
    description: "检查运行时事件",
    event_count: 2,
    reasoning_steps: 1,
    model_calls: 1,
    tool_calls: 1,
    events: [
      { timestamp: "2026-07-30T02:00:01Z", event: "subagent.started", payload: { description: "检查运行时事件" } },
      { timestamp: "2026-07-30T02:00:02Z", event: "tool.call.completed", step: 1, payload: { tool_name: "read_file", output_preview: "done" } },
    ],
  }],
});

const controller = {
  runs: { status: "success" as const, data: [{ runId: "run-live", sessionId: "web:1", status: "running", mode: "coding", startedAt: "2026-07-30T02:00:00Z", finishedAt: "", reasoningSteps: 1, modelCalls: 1, toolCalls: 1 }], error: "", updatedAt: Date.now() },
  activeRunId: "run-live",
  activeDetail: { status: "success" as const, data: detail, error: "", updatedAt: Date.now() },
  live: true,
  pollIntervalMs: 2000,
  loadRuns: vi.fn(async () => controller.runs.data),
  selectRun: vi.fn(async () => detail),
  refreshActiveRun: vi.fn(async () => undefined),
};

vi.mock("../hooks/useRuns", () => ({ useRuns: () => controller }));

describe("RunsPage subagent hierarchy", () => {
  it("shows a parent summary and lazily renders folded child events", async () => {
    const user = userEvent.setup();
    render(<RunsPage />);
    expect(screen.getByText("Subagent 执行汇总")).toBeInTheDocument();
    expect(screen.getByText("实时更新 · 2s")).toBeInTheDocument();
    expect(screen.getByText("检查运行时事件")).toBeInTheDocument();
    expect(screen.queryByText("tool.call.completed")).not.toBeInTheDocument();

    await user.click(screen.getByText("检查运行时事件"));

    expect(await screen.findByText("tool.call.completed")).toBeInTheDocument();
    expect(screen.getAllByText("查看 Payload").length).toBeGreaterThan(0);
  });
});
