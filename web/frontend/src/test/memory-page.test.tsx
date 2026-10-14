import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import MemoryPage from "../pages/MemoryPage";
import type { MemoryItem, MemoryResponse } from "../api/types";

vi.mock("../app/contexts", () => ({ useSessionsContext: () => ({ activeId: "chat-a", active: { title: "编码任务" } }) }));

const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const item = (overrides: Partial<MemoryItem> = {}): MemoryItem => ({
  id: "memory-1", owner_scope: "user", owner_id: "alice", kind: "preference", content: "优先使用中文回复。",
  status: "active", confidence: 1, salience: .8, version: 2, retrievable: true, manageable: true,
  valid_from: "2026-10-01T00:00:00Z", valid_until: null, last_confirmed_at: "2026-10-01T00:00:00Z",
  created_at: "2026-10-01T00:00:00Z", updated_at: "2026-10-01T00:00:00Z", supersedes_id: null,
  source_types: ["explicit_user"], evidence: [{ id: "evidence-1", source_type: "explicit_user", source_ref: "message:1",
    session_id: "web:alice:chat-a", task_id: null, project_id: null, workspace_id: null,
    excerpt: "请记住这个偏好。", created_at: "2026-10-01T00:00:00Z" }], ...overrides,
});
const overview = (overrides: Partial<MemoryResponse> = {}): MemoryResponse => ({
  backend: "postgres", available: true, items: [item()], scopes: [{ scope: "user", id: "alice" }],
  counts: { total: 1, retrievable: 1, by_status: { active: 1 } }, has_more: false, next_offset: null,
  filtered_total: 1, filters: { query: "", status: "", kind: "" },
  runtime: { write_enabled: true, read_enabled: true, context_enabled: true }, message: "", ...overrides,
});
function mockApi(handler?: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const response = handler?.(path, init);
    if (response) return response;
    if (path.startsWith("/api/memory?")) return json(overview());
    if (path === "/api/memory/legacy") return json({ files: [{ name: "MEMORY.md", content: "以前的偏好" }, { name: "SELF.md", content: "" }] });
    throw new Error(`Unexpected memory request: ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

describe("semantic memory console", () => {
  it("shows actual structured memory, lifecycle, source evidence, and account scope", async () => {
    const fetchMock = mockApi();
    render(<MemoryPage />);
    expect(await screen.findByRole("article", { name: "记忆详情" })).toHaveTextContent("优先使用中文回复。");
    expect(screen.getByRole("article")).toHaveTextContent("个人记忆");
    expect(screen.getByRole("article")).toHaveTextContent("版本 2");
    expect(screen.getByRole("region", { name: "记忆来源" })).toHaveTextContent("请记住这个偏好。");
    expect(screen.getByText("当前有效，可供语义检索使用。")).toBeInTheDocument();
    expect(screen.getByText("PostgreSQL")).toBeInTheDocument();
    expect(screen.getByText("语义读取：开启")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledWith("/api/memory?limit=200&offset=0", expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(fetchMock.mock.calls.some(([path]) => path === "/api/memory/legacy")).toBe(false);
    expect(screen.queryByText("MEMORY.md")).not.toBeInTheDocument();
  });

  it("searches and filters the entire authorized scope on the server", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((path) => {
      const params = new URL(path, "http://localhost").searchParams;
      const filters = { query: params.get("query") || "", status: params.get("status") || "", kind: params.get("kind") || "" };
      const rows = [item(), item({ id: "candidate", status: "candidate", kind: "decision", retrievable: false, content: "候选架构决策" }), item({ id: "revoked", status: "revoked", retrievable: false, content: "已撤销的偏好" })].filter((value) => (!filters.status || value.status === filters.status) && (!filters.kind || value.kind === filters.kind) && (!filters.query || value.content.includes(filters.query)));
      return json(overview({ items: rows, filters, filtered_total: rows.length, counts: { total: 3, retrievable: 1, by_status: { active: 1, candidate: 1, revoked: 1 } } }));
    });
    render(<MemoryPage />);
    await screen.findByRole("article");
    await user.selectOptions(screen.getByRole("combobox", { name: "记忆生命周期" }), "candidate");
    expect(await screen.findByRole("article")).toHaveTextContent("候选架构决策");
    expect(screen.getByText("当前不参与语义检索。")).toBeInTheDocument();
    expect(screen.getByRole("navigation", { name: "长期记忆列表" })).not.toHaveTextContent("已撤销的偏好");
    await user.selectOptions(screen.getByRole("combobox", { name: "记忆类型" }), "decision");
    expect(await screen.findByRole("article")).toHaveTextContent("候选架构决策");
    await user.type(screen.getByRole("searchbox", { name: "搜索全部记忆" }), "不存在的内容");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(await screen.findByText("没有匹配的记忆")).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([path]) => String(path).includes("status=candidate") && String(path).includes("kind=decision") && String(path).includes("query="))).toBe(true);
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
  });

  it("distinguishes unavailable storage from an empty memory set and can refresh", async () => {
    const user = userEvent.setup();
    let unavailable = true;
    mockApi(() => json(overview({ available: !unavailable, items: [], counts: { total: 0, retrievable: 0, by_status: {} }, message: unavailable ? "请检查 PostgreSQL。" : "" })));
    render(<MemoryPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("长期记忆存储不可用");
    expect(screen.queryByText("暂无长期记忆")).not.toBeInTheDocument();
    unavailable = false;
    await user.click(screen.getByRole("button", { name: "刷新记忆" }));
    expect(await screen.findByText("暂无长期记忆")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("loads legacy files only when opened and hides empty placeholder files", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi();
    render(<MemoryPage />);
    await screen.findByRole("article");
    await user.click(screen.getByText("旧版 Markdown 记忆（兼容查看）"));
    expect(await screen.findByText("MEMORY.md")).toBeInTheDocument();
    expect(screen.getByText("以前的偏好")).toBeInTheDocument();
    expect(screen.queryByText("SELF.md")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([path]) => path === "/api/memory/legacy")).toHaveLength(1);
  });

  it("changes to server-authorized session scope without displaying earlier personal results", async () => {
    const user = userEvent.setup();
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    const fetchMock = mockApi((path) => path.includes("session_id=chat-a") ? pending : undefined);
    render(<MemoryPage />);
    await screen.findByRole("article");
    await user.selectOptions(screen.getByRole("combobox", { name: "查看范围" }), "session");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByLabelText("正在加载")).toBeInTheDocument();
    await act(async () => finish(json(overview({ items: [item({ id: "task-memory", owner_scope: "task", owner_id: "coding-123", content: "编码任务结论" })], scopes: [{ scope: "user", id: "alice" }, { scope: "task", id: "coding-123" }] }))));
    expect(await screen.findByRole("article")).toHaveTextContent("编码任务结论");
    expect(screen.getByRole("article")).toHaveTextContent("任务记忆");
    expect(fetchMock).toHaveBeenCalledWith("/api/memory?limit=200&offset=0&session_id=chat-a", expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it("reports scope totals and moves pages without confusing totals with the current page", async () => {
    const user = userEvent.setup();
    mockApi((path) => path.includes("offset=200") ? json(overview({ items: [item({ id: "page-2", content: "第二页记忆" })], counts: { total: 201, retrievable: 201, by_status: { active: 201 } } })) : json(overview({ counts: { total: 201, retrievable: 201, by_status: { active: 201 } }, has_more: true, next_offset: 200 })));
    render(<MemoryPage />);
    await screen.findByRole("article");
    expect(screen.getByText("本页 1 条 / 匹配 1 条 / 总计 201 条")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "上一页" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByRole("article")).toHaveTextContent("第二页记忆");
    expect(screen.getByText("第 2 页")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "下一页" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "上一页" })).toBeEnabled();
  });

  it("shows runtime switches and safe context fallback messages separately from stored records", async () => {
    mockApi(() => json(overview({ runtime: { read_enabled: false, write_enabled: false, context_enabled: false }, context_message: "绑定编码任务的记忆作用域暂不可用。" })));
    render(<MemoryPage />);
    await screen.findByRole("article");
    expect(screen.getByText("上下文注入：关闭")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("绑定编码任务");
    expect(screen.getByRole("article")).toHaveTextContent("优先使用中文回复。");
  });

  it("shows an HTTP error and retries without leaking an old result", async () => {
    const user = userEvent.setup();
    let calls = 0;
    mockApi(() => ++calls === 1 ? json({ error: "暂时无法读取" }, 503) : json(overview()));
    render(<MemoryPage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时无法读取");
    await user.click(within(screen.getByRole("alert")).getByRole("button", { name: "重试" }));
    await waitFor(() => expect(screen.getByRole("article")).toBeInTheDocument());
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("requires an explicit confirmation step and sends the displayed version with an action", async () => {
    const user = userEvent.setup();
    let confirmed = false;
    const fetchMock = mockApi((path, init) => {
      if (path === "/api/memory/action") {
        expect(JSON.parse(String(init?.body))).toEqual({ id: "memory-1", expected_version: 2, action: "confirm", reason: "本人确认" });
        confirmed = true;
        return json({ item: item({ version: 3 }), index_sync: "queued", message: "记忆已确认。语义索引更新已排队。" });
      }
      return json(overview({ items: [item(confirmed ? { version: 3 } : { status: "candidate", retrievable: false })] }));
    });
    render(<MemoryPage />);
    await screen.findByRole("article");
    await user.click(screen.getByRole("button", { name: "确认记忆" }));
    expect(fetchMock.mock.calls.filter(([path]) => String(path) === "/api/memory/action")).toHaveLength(0);
    expect(screen.getByRole("group", { name: "确认记忆操作" })).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "记忆操作说明" }), "本人确认");
    await user.click(screen.getByRole("button", { name: "确定确认记忆" }));
    expect(await screen.findByText("记忆已确认。语义索引更新已排队。")).toBeInTheDocument();
    expect(await screen.findByRole("article")).toHaveTextContent("版本 3");
    expect(screen.queryByRole("button", { name: "确认记忆" })).not.toBeInTheDocument();
  });

  it("preserves evidence and content after a 409 and requires a refresh before another action", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((path) => path === "/api/memory/action" ? json({ error: "changed" }, 409) : undefined);
    render(<MemoryPage />);
    await screen.findByRole("article");
    await user.click(screen.getByRole("button", { name: "撤销记忆" }));
    await user.click(screen.getByRole("button", { name: "确定撤销记忆" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("已保留当前内容，请刷新记忆");
    expect(screen.getByRole("article")).toHaveTextContent("优先使用中文回复。");
    expect(screen.getByRole("region", { name: "记忆来源" })).toHaveTextContent("请记住这个偏好。");
    expect(screen.getByRole("button", { name: "撤销记忆" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "确定撤销记忆" })).toBeDisabled();
    expect(fetchMock.mock.calls.filter(([path]) => String(path).startsWith("/api/memory?"))).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "刷新记忆" }));
    expect(await screen.findByRole("button", { name: "撤销记忆" })).toBeEnabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("shows shared scope records as read-only", async () => {
    mockApi(() => json(overview({ items: [item({ manageable: false, owner_scope: "project", owner_id: "shared-repo" })] })));
    render(<MemoryPage />);
    expect(await screen.findByRole("region", { name: "管理记忆" })).toHaveTextContent("此共享作用域仅供查看");
    expect(screen.queryByRole("button", { name: "撤销记忆" })).not.toBeInTheDocument();
  });

  it("disables actions for records whose validity has elapsed", async () => {
    mockApi(() => json(overview({ items: [item({ status: "candidate", retrievable: false, valid_until: "2020-01-01T00:00:00Z" })] })));
    render(<MemoryPage />);
    expect(await screen.findByRole("region", { name: "管理记忆" })).toHaveTextContent("有效期已结束");
    expect(screen.queryByRole("button", { name: "确认记忆" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "拒绝候选" })).not.toBeInTheDocument();
  });

  it("resets offset when filters change and never shows the old query while waiting", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((path) => {
      const params = new URL(path, "http://localhost").searchParams;
      const query = params.get("query") || "";
      const offset = Number(params.get("offset"));
      return json(overview({ items: [item({ content: query ? "结果：新条件" : offset ? "第二页的旧记忆" : "第一页" })], filters: { query, status: "", kind: "" }, has_more: !offset && !query, next_offset: !offset && !query ? 200 : null, counts: { total: 201, retrievable: 201, by_status: { active: 201 } } }));
    });
    render(<MemoryPage />);
    await screen.findByRole("article");
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByRole("article")).toHaveTextContent("第二页的旧记忆");
    await user.type(screen.getByRole("searchbox", { name: "搜索全部记忆" }), "新条件");
    expect(screen.queryByRole("article")).not.toBeInTheDocument();
    expect(screen.getByRole("searchbox", { name: "搜索全部记忆" })).toHaveValue("新条件");
    expect(await screen.findByRole("article")).toHaveTextContent("结果：新条件");
    const lastPath = String(fetchMock.mock.calls.at(-1)?.[0]);
    expect(new URL(lastPath, "http://localhost").searchParams.get("offset")).toBe("0");
  });

  it("returns to the previous populated page when an action removes the last filtered item", async () => {
    const user = userEvent.setup();
    let applied = false;
    mockApi((path) => {
      if (path === "/api/memory/action") { applied = true; return json({ item: item(), index_sync: "queued", message: "已拒绝" }); }
      const params = new URL(path, "http://localhost").searchParams;
      const offset = Number(params.get("offset"));
      const status = params.get("status") || "";
      return json(overview({ items: offset && applied ? [] : [item({ id: offset ? "last" : "first", content: offset ? "最后一条候选" : "前一页的候选", status: "candidate", retrievable: false })], filters: { query: "", status, kind: "" }, filtered_total: applied ? 200 : 201, has_more: !offset && !applied, next_offset: !offset && !applied ? 200 : null, counts: { total: 201, retrievable: 0, by_status: { candidate: applied ? 200 : 201 } } }));
    });
    render(<MemoryPage />);
    await screen.findByRole("article");
    await user.selectOptions(screen.getByRole("combobox", { name: "记忆生命周期" }), "candidate");
    await screen.findByRole("article");
    await user.click(screen.getByRole("button", { name: "下一页" }));
    expect(await screen.findByRole("article")).toHaveTextContent("最后一条候选");
    await user.click(screen.getByRole("button", { name: "拒绝候选" }));
    await user.click(screen.getByRole("button", { name: "确定拒绝候选" }));
    await waitFor(() => expect(screen.getByRole("article")).toHaveTextContent("前一页的候选"));
    expect(screen.queryByText("第 2 页")).not.toBeInTheDocument();
  });

  it("ignores a completed action from a previously selected scope", async () => {
    const user = userEvent.setup();
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    const fetchMock = mockApi((path) => {
      if (path === "/api/memory/action") return pending;
      if (path.includes("session_id=chat-a")) return json(overview({ items: [item({ id: "task-memory", content: "当前任务记忆" })] }));
      return undefined;
    });
    render(<MemoryPage />);
    await screen.findByRole("article");
    await user.click(screen.getByRole("button", { name: "撤销记忆" }));
    await user.click(screen.getByRole("button", { name: "确定撤销记忆" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "查看范围" }), "session");
    expect(await screen.findByRole("article")).toHaveTextContent("当前任务记忆");
    await act(async () => finish(json({ item: item({ status: "revoked", version: 3 }), index_sync: "queued", message: "旧作用域操作已完成" })));
    expect(screen.getByRole("article")).toHaveTextContent("当前任务记忆");
    expect(screen.queryByText("旧作用域操作已完成")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([path]) => String(path).startsWith("/api/memory?"))).toHaveLength(2);
  });
});
