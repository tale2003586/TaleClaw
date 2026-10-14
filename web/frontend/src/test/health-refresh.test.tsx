import { useState } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { expect, it, vi } from "vitest";
import { useAppContext } from "../app/contexts";
import { AppBootstrap } from "../app/AppBootstrap";

vi.mock("../app/AppShell", () => ({ AppShell: function Shell() {
  const { health, refreshHealth } = useAppContext();
  const [draft, setDraft] = useState("");
  return <><input aria-label="草稿" value={draft} onChange={(event) => setDraft(event.target.value)} />
    <span>{health.models?.[0]?.model}</span>
    <button onClick={() => void refreshHealth?.()}>刷新模型</button></>;
} }));

it("refreshes configured models after a settings save without remounting the app", async () => {
  let model = "old-model";
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL) => new Response(JSON.stringify(
    String(input) === "/api/health" ? {
      ok: true, workspace: "/app", coding_workspace: "/app", runtime: "lazy",
      user: { id: "admin", role: "admin" }, models: [{ profile: "relay", model, provider: "relay", supports_thinking: false }],
    } : { sessions: [], runs: [] },
  ), { headers: { "Content-Type": "application/json" } })));
  render(<AppBootstrap />);
  expect(await screen.findByText("old-model")).toBeInTheDocument();
  const input = screen.getByRole("textbox", { name: "草稿" });
  fireEvent.change(input, { target: { value: "未发送的消息" } });
  model = "new-model";
  fireEvent.click(screen.getByRole("button", { name: "刷新模型" }));
  expect(await screen.findByText("new-model")).toBeInTheDocument();
  expect(screen.getByRole("textbox", { name: "草稿" })).toBe(input);
  expect(input).toHaveValue("未发送的消息");
});
