import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../app/App";

describe("App", () => {
  beforeEach(() => {
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/health") return json({ ok: true, workspace: "/app", coding_workspace: "/work", runtime: "ready", user: { id: "admin", role: "admin" } });
      if (path === "/api/sessions") return json({ sessions: [] });
      if (path.startsWith("/api/session")) return json({ session: { chat_id: "default", channel: "web", title: "默认会话", current_mode: "hybrid", messages: [{ role: "assistant", content: "处理完成" }, { role: "tool", name: "security_search", content: "{\n  \"score\": 0.67,\n  \"source\": \"advisory.json\"\n}" }] } });
      return json({});
    }));
  });

  it("boots the React shell and exposes all admin navigation", async () => {
    const user = userEvent.setup();
    const { container } = render(<App />);
    expect(await screen.findByRole("button", { name: /日志/ })).toBeInTheDocument();
    expect(screen.getByText("taleclaw")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Runs/ })).toBeInTheDocument();
    expect(await screen.findByText("默认会话")).toBeInTheDocument();
    expect(await screen.findByText("处理完成")).toBeInTheDocument();
    expect(container.querySelector("details[data-technical-output]")).not.toHaveAttribute("open");
    const themeToggle = screen.getByRole("button", { name: "切换到暗色主题" });
    expect(themeToggle).toHaveAttribute("title", "切换到暗色主题");
    await user.click(themeToggle);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("taleclawTheme")).toBe("dark");
    await user.click(screen.getByRole("button", { name: "折叠侧栏" }));
    expect(screen.getByRole("button", { name: "展开侧栏" })).toBeInTheDocument();
  });
});

const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
