import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { ActivityTimeline } from "../components/chat/ActivityTimeline";
import type { ActivityItem } from "../hooks/useChatStream";

describe("ActivityTimeline", () => {
  it("groups tool start and completion events and keeps the result collapsed", () => {
    const items: ActivityItem[] = [
      event("tool.call.started", 100, { span_id: "tool-1", tool: "read_file", args: '{"path":"src/app.ts"}' }),
      event("tool.call.completed", 420, { span_id: "tool-1", tool: "read_file", duration_ms: 320, preview: "file content" }),
    ];
    const { container } = render(<ActivityTimeline items={items} status="complete" startedAt={0} finishedAt={1500} />);

    const details = container.querySelector("details.activity-panel");
    expect(details).not.toHaveAttribute("open");
    expect(screen.getByText("执行完成 · 1 次工具调用 · 1.5s")).toBeInTheDocument();
    expect(screen.getAllByText("read_file")).toHaveLength(1);
    expect(screen.getByText("完成 · 320ms")).toBeInTheDocument();
    expect(screen.getByText("src/app.ts")).toBeInTheDocument();
  });

  it("shows live execution metrics before any tool starts", () => {
    render(<ActivityTimeline items={[]} status="running" startedAt={performance.now()} />);
    expect(screen.getByText("执行中")).toBeInTheDocument();
    expect(screen.getByText("0 次工具调用")).toBeInTheDocument();
  });
});

function event(name: string, receivedAt: number, raw: Record<string, unknown>): ActivityItem {
  return { id: `${name}:${receivedAt}`, event: name, label: name, step: 1, receivedAt, raw };
}
