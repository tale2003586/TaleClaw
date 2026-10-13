import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import SkillsSettings from "../components/SkillsSettings";
import type { SkillCatalogResponse, SkillSummary } from "../api/types";

const skill = (overrides: Partial<SkillSummary> = {}): SkillSummary => ({
  id: "review", name: "代码审查", description: "检查代码改动", enabled: true, valid: true,
  diagnostics: [], revision: "r1", path: "review/SKILL.md", tags: [], triggers: [],
  applies_to: ["coding"], requires_tools: ["read_file"], ...overrides,
});
const catalog = (...skills: SkillSummary[]): SkillCatalogResponse => ({ skills, revision: "catalog-1" });
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const original = "---\nname: review\n---\nReview carefully.\n";
const detail = (content = original, revision = "r1") => ({ id: "review", content, revision });

function mockApi(handler?: (path: string, init?: RequestInit) => Response | Promise<Response> | undefined) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    const custom = handler?.(path, init);
    if (custom) return custom;
    if (path === "/api/skills") return json(catalog(skill()));
    if (path === "/api/skills/detail?id=review") return json(detail());
    throw new Error(`Unexpected test request: ${path}`);
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}

async function edit(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "编辑技能 代码审查" }));
  return screen.findByRole("textbox", { name: "技能 review 内容" });
}

describe("server skills settings", () => {
  it("does not fetch the global catalog for a non-admin", () => {
    const fetchMock = mockApi();
    render(<SkillsSettings canManage={false} />);
    expect(screen.getByText(/技能由管理员统一管理/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("button", { name: "刷新目录" })).not.toBeInTheDocument();
  });

  it("shows disabled and invalid skills and describes the effective scope", async () => {
    mockApi((path) => path === "/api/skills" ? json(catalog(skill(), skill({ id: "broken", name: "待修复", enabled: false, valid: false, diagnostics: ["description 必须是文本"] }))) : undefined);
    render(<SkillsSettings canManage />);
    expect(await screen.findByRole("button", { name: "编辑技能 待修复" })).toBeInTheDocument();
    expect(screen.getByText("配置无效")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "启用 待修复" })).toBeDisabled();
    expect(screen.getByText(/启停无需重启服务/)).toBeInTheDocument();
    expect(screen.getByText("description 必须是文本")).toBeInTheDocument();
    expect(screen.getByText(/修改会影响所有账号/)).toBeInTheDocument();
    expect(screen.getByText(/已经进入现有对话的技能文本不会自动撤回/)).toBeInTheDocument();
  });

  it("waits for the server before changing enabled state", async () => {
    const user = userEvent.setup();
    let enabled = true;
    let finish!: (response: Response) => void;
    const update = new Promise<Response>((resolve) => { finish = resolve; });
    const fetchMock = mockApi((path, init) => {
      if (path === "/api/skills") return json(catalog(skill({ enabled, revision: enabled ? "r1" : "r2" })));
      if (path === "/api/skills/update") {
        expect(JSON.parse(String(init?.body))).toEqual({ id: "review", revision: "r1", enabled: false });
        return update;
      }
    });
    render(<SkillsSettings canManage />);
    await user.click(await screen.findByRole("button", { name: "停用 代码审查" }));
    expect(screen.getByText("已启用")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停用 代码审查" })).toBeDisabled();
    enabled = false;
    await act(async () => finish(json(detail(original, "r2"))));
    expect(await screen.findByRole("button", { name: "启用 代码审查" })).toBeEnabled();
    expect(screen.getByText("已停用")).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([path]) => path === "/api/skills")).toHaveLength(2);
  });

  it("keeps the previous enabled state when an update is forbidden", async () => {
    const user = userEvent.setup();
    mockApi((path) => path === "/api/skills/update" ? json({ error: "需要管理员权限" }, 403) : undefined);
    render(<SkillsSettings canManage />);
    await user.click(await screen.findByRole("button", { name: "停用 代码审查" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("需要管理员权限");
    expect(screen.getByText("已启用")).toBeInTheDocument();
  });

  it("saves exact content with its original revision and reloads the catalog", async () => {
    const user = userEvent.setup();
    const updated = `${original}Keep API v1.\n`;
    let saved = false;
    const fetchMock = mockApi((path, init) => {
      if (path === "/api/skills/update") {
        expect(JSON.parse(String(init?.body))).toEqual({ id: "review", revision: "r1", content: updated });
        saved = true;
        return json(detail(updated, "r2"));
      }
      if (path === "/api/skills" && saved) return json(catalog(skill({ revision: "r2" })));
    });
    render(<SkillsSettings canManage />);
    const editor = await edit(user);
    await user.type(editor, "Keep API v1.{enter}");
    await user.click(screen.getByRole("button", { name: "保存技能" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "保存技能" })).toBeDisabled());
    expect(editor).toHaveValue(updated);
    expect(screen.getByText(/技能内容已保存/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.filter(([path]) => path === "/api/skills")).toHaveLength(2);
  });

  it.each([400, 409])("keeps a draft and its revision when save returns %s", async (status) => {
    const user = userEvent.setup();
    const error = status === 409 ? "技能版本已变化，请重新加载" : "技能 frontmatter 无效";
    const fetchMock = mockApi((path) => path === "/api/skills/update" ? json({ error }, status) : undefined);
    render(<SkillsSettings canManage />);
    const editor = await edit(user);
    await user.type(editor, "draft retained");
    await user.click(screen.getByRole("button", { name: "保存技能" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(error);
    expect(editor).toHaveValue(`${original}draft retained`);
    expect(screen.getByRole("button", { name: "保存技能" })).toBeEnabled();
    const updateCall = fetchMock.mock.calls.find(([path]) => path === "/api/skills/update");
    expect(JSON.parse(String(updateCall?.[1]?.body)).revision).toBe("r1");
  });

  it("refreshes a changed catalog without replacing a dirty draft or silently switching skills", async () => {
    const user = userEvent.setup();
    const other = skill({ id: "other", name: "其他技能" });
    let refreshed = false;
    const fetchMock = mockApi((path) => {
      if (path === "/api/skills") return json(catalog(skill(), other));
      if (path === "/api/skills/reload") { refreshed = true; return json(catalog(skill({ revision: "r2" }), other)); }
      if (path === "/api/skills/detail?id=review") return json(refreshed ? detail("externally updated", "r2") : detail());
    });
    render(<SkillsSettings canManage />);
    const editor = await edit(user);
    await user.type(editor, "unsaved draft");
    expect(screen.getByRole("button", { name: "编辑技能 其他技能" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "停用 代码审查" })).toBeDisabled();
    await user.click(screen.getByRole("button", { name: "刷新目录" }));
    expect(await screen.findByText(/目录已刷新，未保存的编辑仍保留/)).toBeInTheDocument();
    expect(editor).toHaveValue(`${original}unsaved draft`);
    expect(fetchMock.mock.calls.filter(([path]) => path === "/api/skills/detail?id=review")).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "放弃修改并重新读取" }));
    expect(await screen.findByRole("textbox", { name: "技能 review 内容" })).toHaveValue("externally updated");
    expect(screen.getByRole("button", { name: "编辑技能 其他技能" })).toBeEnabled();
  });

  it("explains an empty catalog and permits a failed initial fetch to be retried", async () => {
    const user = userEvent.setup();
    let calls = 0;
    mockApi((path) => path === "/api/skills" ? (++calls === 1 ? json({ error: "读取失败" }, 500) : json(catalog())) : undefined);
    render(<SkillsSettings canManage />);
    expect(await screen.findByRole("alert")).toHaveTextContent("读取失败");
    await user.click(screen.getByRole("button", { name: "重试读取目录" }));
    expect(await screen.findByText("暂无技能")).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });
  it("retains a dirty draft when refresh removes its skill from the catalog", async () => {
    const user = userEvent.setup();
    mockApi((path) => path === "/api/skills/reload" ? json(catalog()) : undefined);
    render(<SkillsSettings canManage />);
    const editor = await edit(user);
    await user.type(editor, "keep this draft");
    await user.click(screen.getByRole("button", { name: "刷新目录" }));
    expect(await screen.findByText(/此技能已不在当前目录中/)).toBeInTheDocument();
    expect(editor).toHaveValue(`${original}keep this draft`);
    expect(screen.getByRole("button", { name: "保存技能" })).toBeEnabled();
  });

  it("distinguishes a successful save from a failed catalog refresh", async () => {
    const user = userEvent.setup();
    let saved = false;
    mockApi((path, init) => {
      if (path === "/api/skills/update") {
        saved = true;
        return json(detail(JSON.parse(String(init?.body)).content, "r2"));
      }
      if (path === "/api/skills" && saved) return json({ error: "目录暂不可用" }, 503);
    });
    render(<SkillsSettings canManage />);
    const editor = await edit(user);
    await user.type(editor, "saved text");
    await user.click(screen.getByRole("button", { name: "保存技能" }));
    expect(await screen.findByText(/修改已保存，但目录刷新失败/)).toBeInTheDocument();
    expect(editor).toHaveValue(`${original}saved text`);
    expect(screen.getByRole("button", { name: "保存技能" })).toBeDisabled();
    expect(screen.queryByText("操作未完成")).not.toBeInTheDocument();
    expect(screen.queryByText(/服务端版本已变化/)).not.toBeInTheDocument();
  });

});
