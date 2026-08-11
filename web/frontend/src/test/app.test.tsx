import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../app/App";

const longQuestion = `${"请详细分析这个问题。".repeat(60)}完整问题结尾标记`;

describe("App", () => {
  beforeEach(() => {
    localStorage.removeItem("taleclaw.model_profile");
    vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path === "/api/health") return json({ ok: true, workspace: "/app", coding_workspace: "/work", runtime: "ready", user: { id: "admin", role: "admin" }, default_model_profile: "plain", thinking_supported: true, models: [{ profile: "plain", provider: "relay", model: "plain-model", supports_thinking: false }, { profile: "reasoning", provider: "relay", model: "reasoning-model", supports_thinking: true }] });
      if (path.startsWith("/api/sessions")) return json({ sessions: [], has_more: false });
      if (path.includes("before=10")) return json({ session: { chat_id: "default", channel: "web", messages: [{ seq: 1, role: "assistant", content: "更早的历史消息" }], message_page: { has_more: false, next_before: null } } });
      if (path.startsWith("/api/session")) return json({ session: { chat_id: "default", channel: "web", title: "默认会话", active_agent: "hybrid", messages: [{ seq: 10, role: "user", content: longQuestion }, { seq: 11, role: "assistant", content: "处理完成" }, { seq: 12, role: "tool", name: "security_search", content: "{\n  \"score\": 0.67,\n  \"source\": \"advisory.json\"\n}" }], message_page: { has_more: true, next_before: 10 } } });
      return json({});
    }));
  });

  it("boots the React shell, keeps primary navigation focused, and exposes account tools", async () => {
    const user = userEvent.setup();
    const { container } = render(<App />);
    expect(await screen.findByRole("button", { name: /日志/ })).toBeInTheDocument();
    expect(screen.getByText("taleclaw")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Runs/ })).toBeInTheDocument();
    expect(await screen.findByText("默认会话")).toBeInTheDocument();
    expect(await screen.findByText("处理完成")).toBeInTheDocument();
    const modelPicker = screen.getByRole("combobox", { name: "选择模型" });
    const thinkingButton = screen.getByRole("button", { name: "深度思考" });
    expect(thinkingButton).toBeDisabled();
    await user.selectOptions(modelPicker, "reasoning");
    expect(thinkingButton).toBeEnabled();
    expect(screen.queryByText(longQuestion)).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /展开完整问题/ }));
    expect(screen.getByText(longQuestion)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "收起问题" }));
    await user.click(screen.getByRole("button", { name: "向上滚动加载更早消息" }));
    expect(await screen.findByText("更早的历史消息")).toBeInTheDocument();
    expect(container.querySelector("details[data-technical-output]")).not.toHaveAttribute("open");
    expect(vi.mocked(fetch).mock.calls.some(([path]) => String(path).includes("/api/sessions?limit=30&offset=0"))).toBe(true);
    expect(vi.mocked(fetch).mock.calls.some(([path]) => String(path).includes("before=10"))).toBe(true);
    expect(screen.queryByRole("button", { name: /^设置/ })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /当前账号/ }));
    expect(screen.getByRole("menu", { name: "账号与更多功能" })).toBeInTheDocument();
    expect(screen.getByRole("menuitem", { name: /设置/ })).toBeInTheDocument();
    const themeToggle = screen.getByRole("menuitem", { name: /切换到暗色/ });
    await user.click(themeToggle);
    expect(document.documentElement.dataset.theme).toBe("dark");
    expect(localStorage.getItem("taleclawTheme")).toBe("dark");
    await user.click(screen.getByRole("menuitem", { name: /设置/ }));
    expect(await screen.findByRole("heading", { name: "设置" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "保存工作区" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "折叠侧栏" }));
    expect(screen.getByRole("button", { name: "展开侧栏" })).toBeInTheDocument();
  });

  it("opens Logs and Runs and explains the empty state", async () => {
    const user = userEvent.setup();
    render(<App />);
    const chatReply = await screen.findByText("处理完成");
    await user.click(await screen.findByRole("button", { name: /日志/ }));
    expect(await screen.findByRole("heading", { name: "系统日志" })).toBeInTheDocument();
    expect(await screen.findByText("还没有运行日志")).toBeInTheDocument();
    expect(chatReply).toBeInTheDocument();
    expect(chatReply).not.toBeVisible();
    await user.click(screen.getByRole("button", { name: /Runs/ }));
    expect(await screen.findByRole("heading", { name: "Runs / Trace" })).toBeInTheDocument();
    expect(await screen.findByText("还没有 Run")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /聊天/ }));
    expect(await screen.findByText("处理完成")).toBe(chatReply);
    expect(chatReply).toBeVisible();
  });
});

const json = (value: unknown) => new Response(JSON.stringify(value), { status: 200, headers: { "Content-Type": "application/json" } });
