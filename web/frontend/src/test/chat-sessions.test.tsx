import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import App from "../app/App";
import type { ChatRunDto, SessionDto } from "../api/types";

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
let sessions: Record<string, SessionDto>;
let records: Record<string, ChatRunDto>;
let streams: Record<string, ReadableStreamDefaultController<Uint8Array>>;
let submitBodies: Array<Record<string, unknown>>;
let completeUpload: ((response: Response) => void) | undefined;
const encoder = new TextEncoder();

beforeEach(() => {
  localStorage.clear();
  window.location.hash = "";
  sessions = Object.fromEntries(["default", "B"].map((id) => [id, { chat_id: id, title: id === "default" ? "会话 A" : "会话 B", channel: "web", active_agent: "hybrid", messages: [] }]));
  records = {}; streams = {}; submitBodies = []; completeUpload = undefined;
  vi.stubGlobal("fetch", vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === "/api/health") return json({ ok: true, workspace: "/app", coding_workspace: "/source", runtime: "ready", user: { id: "admin", role: "admin" }, models: [] });
    if (path.startsWith("/api/sessions")) return json({ sessions: Object.values(sessions), has_more: false });
    if (path.startsWith("/api/session?")) return json({ session: sessions[new URL(path, "http://test").searchParams.get("session_id")!] });
    if (path === "/api/chat/runs" && init?.method === "POST") {
      const body = JSON.parse(String(init.body)) as Record<string, unknown>;
      submitBodies.push(body);
      const sessionId = String(body.session_id), id = "run-" + sessionId;
      const run: ChatRunDto = { run_id: id, session_id: sessionId, status: "running", last_seq: 0, created_at: new Date().toISOString(), updated_at: new Date().toISOString() };
      records[id] = run;
      return json({ run });
    }
    if (path === "/api/chat/runs") return json({ runs: Object.values(records) });
    if (path.includes("/events?")) {
      const id = path.split("/")[4];
      return new Response(new ReadableStream<Uint8Array>({ start(controller) {
        streams[id] = controller;
        init?.signal?.addEventListener("abort", () => { try { controller.close(); } catch { /* already closed */ } }, { once: true });
      } }));
    }
    if (path.startsWith("/api/chat/runs/")) return json({ run: records[path.split("/")[4]] });
    if (path === "/api/files/upload") return new Promise<Response>((resolve) => { completeUpload = resolve; });
    if (path === "/api/chat") {
      const body = JSON.parse(String(init?.body));
      const id = String(body.session_id);
      sessions[id] = { ...sessions[id], active_agent: body.message === "/coding" ? "coding" : "hybrid" };
      return json({ session: sessions[id] });
    }
    return json({});
  }));
});
async function openB(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /会话 B/ }));
  await screen.findByRole("heading", { name: "会话 B" });
}
async function openA(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole("button", { name: /会话 A/ }));
  await screen.findByRole("heading", { name: "会话 A" });
}
async function boot() {
  const user = userEvent.setup();
  render(<App />);
  await screen.findByRole("heading", { name: "会话 A" });
  await waitFor(() => expect(screen.getByRole("textbox", { name: "消息" })).toBeEnabled());
  return user;
}

describe("chat session ownership", () => {
  it("keeps drafts and attachments per session while switching", async () => {
    const user = await boot();
    await user.type(screen.getByRole("textbox", { name: "消息" }), "A draft");
    fireEvent.change(screen.getByLabelText("附件文件"), { target: { files: [new File(["a"], "A.pdf", { type: "application/pdf" })] } });
    await openB(user);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("");
    expect(screen.queryByText("A.pdf")).not.toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "消息" }), "B draft");
    await openA(user);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("A draft");
    expect(screen.getByText("A.pdf")).toBeInTheDocument();
    await openB(user);
    expect(screen.getByRole("textbox", { name: "消息" })).toHaveValue("B draft");
  });

  it("keeps upload and late completion bound to A after switching and starting B", async () => {
    const user = await boot();
    await user.type(screen.getByRole("textbox", { name: "消息" }), "A request");
    fireEvent.change(screen.getByLabelText("附件文件"), { target: { files: [new File(["a"], "A.pdf", { type: "application/pdf" })] } });
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(completeUpload).toBeDefined());
    await openB(user);
    await user.type(screen.getByRole("textbox", { name: "消息" }), "B request");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await act(async () => completeUpload!(json({ saved: [{ path: "chat-attachments/A.pdf" }] })));
    await waitFor(() => expect(submitBodies).toHaveLength(2));
    expect(submitBodies.find((body) => body.message === "A request")).toMatchObject({ session_id: "default", attachments: ["chat-attachments/A.pdf"] });
    await waitFor(() => expect(streams["run-default"]).toBeDefined());
    await act(async () => {
      const session: SessionDto = { ...sessions.default, messages: [{ seq: 1, role: "user", content: "A request" }, { seq: 2, role: "assistant", content: "A-only completed reply" }] };
      sessions.default = session;
      records["run-default"] = { ...records["run-default"], status: "completed", last_seq: 1 };
      streams["run-default"].enqueue(encoder.encode(JSON.stringify({ type: "complete", seq: 1, run_id: "run-default", session_id: "default", reply: "A-only completed reply", session }) + "\n"));
      streams["run-default"].close();
    });
    await waitFor(() => expect(screen.getByRole("heading", { name: "会话 B" })).toBeInTheDocument());
    expect(screen.queryByText("A-only completed reply")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停止" })).toBeEnabled();
    await openA(user);
    expect(await screen.findByText("A-only completed reply")).toBeInTheDocument();
    expect(screen.getAllByText("A-only completed reply")).toHaveLength(1);
  });

  it("sends explicit new Coding worktree settings and explains committed-only source", async () => {
    const user = await boot();
    await user.click(screen.getByRole("button", { name: "Coding" }));
    await screen.findByLabelText("Coding 任务操作");
    expect(screen.getByLabelText("Coding 工作区方式")).toHaveValue("worktree");
    expect(screen.getByText(/HEAD 已提交版本/)).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "消息" }), "implement");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(submitBodies).toHaveLength(1));
    expect(submitBodies[0]).toMatchObject({ workspace_root: "/source", coding_task: { mode: "new", workspace_mode: "worktree", base_ref: "HEAD" } });
  });

  it("does not send a worktree base_ref for an explicitly in-place task", async () => {
    const user = await boot();
    await user.click(screen.getByRole("button", { name: "Coding" }));
    await screen.findByLabelText("Coding 任务操作");
    await user.selectOptions(screen.getByLabelText("Coding 工作区方式"), "in_place");
    await user.type(screen.getByRole("textbox", { name: "消息" }), "edit here");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(submitBodies).toHaveLength(1));
    expect(submitBodies[0]).toMatchObject({ workspace_root: "/source", coding_task: { mode: "new", workspace_mode: "in_place" } });
    expect(submitBodies[0].coding_task).not.toHaveProperty("base_ref");
  });

  it("continuing a task omits the global workspace even if settings differ", async () => {
    const user = await boot();
    await user.click(screen.getByRole("button", { name: "Coding" }));
    await screen.findByLabelText("Coding 任务操作");
    await user.selectOptions(screen.getByLabelText("Coding 任务操作"), "continue");
    await user.type(screen.getByLabelText("Coding 任务 ID"), "existing-task");
    await user.type(screen.getByRole("textbox", { name: "消息" }), "continue");
    await user.click(screen.getByRole("button", { name: "发送" }));
    await waitFor(() => expect(submitBodies).toHaveLength(1));
    expect(submitBodies[0]).not.toHaveProperty("workspace_root");
    expect(submitBodies[0].coding_task).toEqual({ mode: "continue", task_id: "existing-task" });
  });
});
