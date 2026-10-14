import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ModelSettingsResponse } from "../api/modelSettings";
import ModelSettings from "../components/ModelSettings";

const initial: ModelSettingsResponse = {
  revision: 3, configured: true, default_profile: "primary", profiles: [{
    name: "primary", provider: "openai", base_url: "https://provider.example/v1", model: "original-model",
    has_api_key: true, wire_api: "responses", thinking_value: "high", fallbacks: [],
  }], routes: { default: ["primary"] }, provider_defaults: {}, route_purposes: ["default", "reflection"],
};
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "content-type": "application/json" } });
const successfulProbe = { status: "ok", message: "鉴权与模型列表接口可用；这不代表模型生成已验证。", elapsed_ms: 45, models: ["discovered-model"], truncated: false };
const successfulPreview = {
  revision: 3, configured: true, routes: [{ purpose: "reflection", source: "default", source_purpose: "default",
    profiles: [{ name: "primary", provider: "openai", model: "draft-model", source: "route" }],
  }],
};
const setupApi = (handle: (path: string, init?: RequestInit) => Response | Promise<Response> = () => json({})) => {
  const fetchMock = vi.fn((input: RequestInfo | URL, init?: RequestInit) => String(input) === "/api/settings/models" && !init?.method
    ? Promise.resolve(json(initial)) : Promise.resolve(handle(String(input), init)));
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
};
const postCalls = (fetchMock: ReturnType<typeof setupApi>) => fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
const probeButton = () => screen.getByRole("button", { name: "测试 primary 连接并发现模型" });

describe("model diagnostics draft workbench", () => {
  it("only requests the provider probe on an explicit click and submits current unsaved fields", async () => {
    const user = userEvent.setup();
    const fetchMock = setupApi((path) => { expect(path).toBe("/api/settings/models/probe"); return json(successfulProbe); });
    render(<ModelSettings canManage />);
    const model = await screen.findByLabelText("primary 模型名称");
    await user.clear(model); await user.type(model, "draft-model");
    expect(postCalls(fetchMock)).toHaveLength(0);
    await user.click(probeButton());
    expect(await screen.findByText(/耗时 45 ms/)).toBeInTheDocument();
    const payload = JSON.parse(String(postCalls(fetchMock)[0][1]?.body));
    expect(payload).toMatchObject({ revision: 3, profile: { name: "primary", model: "draft-model", thinking_value: "high", wire_api: "responses" } });
    expect(payload.profile).not.toHaveProperty("api_key");
    await user.selectOptions(screen.getByLabelText("primary 发现的模型"), "discovered-model");
    expect(model).toHaveValue("discovered-model");
    expect(screen.queryByLabelText("primary 发现的模型")).not.toBeInTheDocument();
    expect(postCalls(fetchMock)).toHaveLength(1);
    expect(screen.getByRole("button", { name: "保存模型设置" })).toBeEnabled();
  });

  it("requires a new key after target changes before a probe or save and preserves drafts", async () => {
    const user = userEvent.setup();
    const fetchMock = setupApi(() => json(successfulProbe));
    render(<ModelSettings canManage />);
    const url = await screen.findByLabelText("primary Base URL");
    await user.clear(url); await user.type(url, "https://new-provider.example/v1");
    expect(screen.getByLabelText("primary API Key")).toBeRequired();
    expect(screen.getByText(/接口目标已修改/)).toBeInTheDocument();
    await user.click(probeButton());
    expect(await screen.findByRole("alert")).toHaveTextContent("目标已修改时需要重新填写");
    expect(postCalls(fetchMock)).toHaveLength(0);
    await user.type(screen.getByLabelText("primary API Key"), "new-test-key");
    await user.click(probeButton());
    expect(await screen.findByLabelText("primary 发现的模型")).toBeInTheDocument();
    expect(JSON.parse(String(postCalls(fetchMock)[0][1]?.body)).profile.api_key).toBe("new-test-key");
    expect(url).toHaveValue("https://new-provider.example/v1");
    expect(screen.getByLabelText("primary API Key")).toHaveValue("new-test-key");
  });

  it("permits equivalent target normalization to retain a hidden key", async () => {
    const user = userEvent.setup();
    const fetchMock = setupApi(() => json(successfulProbe));
    render(<ModelSettings canManage />);
    const url = await screen.findByLabelText("primary Base URL");
    await user.clear(url); await user.type(url, "https://PROVIDER.EXAMPLE:443/v1/");
    expect(screen.getByLabelText("primary API Key")).not.toBeRequired();
    await user.click(probeButton());
    expect(await screen.findByLabelText("primary 发现的模型")).toBeInTheDocument();
    expect(JSON.parse(String(postCalls(fetchMock)[0][1]?.body)).profile).not.toHaveProperty("api_key");
  });

  it("invalidates an in-flight probe after draft edits", async () => {
    const user = userEvent.setup();
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    setupApi(() => pending);
    render(<ModelSettings canManage />);
    const model = await screen.findByLabelText("primary 模型名称");
    await user.click(probeButton());
    expect(probeButton()).toBeDisabled();
    await user.clear(model); await user.type(model, "edited-during-probe");
    await act(async () => finish(json(successfulProbe)));
    expect(screen.queryByLabelText("primary 发现的模型")).not.toBeInTheDocument();
    expect(model).toHaveValue("edited-during-probe");
  });

  it("does not let an unsupported model-list endpoint prevent manual editing", async () => {
    const user = userEvent.setup();
    setupApi(() => json({ ...successfulProbe, status: "unsupported", message: "未提供模型列表接口；仍可手动填写模型并保存。", models: [] }));
    render(<ModelSettings canManage />);
    await user.click(await screen.findByRole("button", { name: "测试 primary 连接并发现模型" }));
    expect(await screen.findByText(/未提供模型列表接口/)).toBeInTheDocument();
    expect(screen.queryByLabelText("primary 发现的模型")).not.toBeInTheDocument();
    await user.type(screen.getByLabelText("primary 模型名称"), "-manual");
    expect(screen.getByRole("button", { name: "保存模型设置" })).toBeEnabled();
  });

  it("previews current effective routes without saving and clears the preview on edits", async () => {
    const user = userEvent.setup();
    const fetchMock = setupApi((path) => { expect(path).toBe("/api/settings/models/preview"); return json(successfulPreview); });
    render(<ModelSettings canManage />);
    const model = await screen.findByLabelText("primary 模型名称");
    await user.clear(model); await user.type(model, "draft-model");
    await user.click(screen.getByRole("button", { name: "检查配置与路由" }));
    const preview = await screen.findByRole("region", { name: "有效路由预览" });
    expect(within(preview).getByText("继承默认路由")).toBeInTheDocument();
    expect(preview).toHaveTextContent("首选 · primary");
    expect(postCalls(fetchMock)).toHaveLength(1);
    expect(JSON.parse(String(postCalls(fetchMock)[0][1]?.body)).profiles[0].model).toBe("draft-model");
    expect(screen.getByRole("button", { name: "保存模型设置" })).toBeEnabled();
    await user.type(model, "-modified");
    expect(screen.queryByRole("region", { name: "有效路由预览" })).not.toBeInTheDocument();
  });

  it("retains drafts and explains server revision conflicts during checks", async () => {
    const user = userEvent.setup();
    setupApi(() => json({ error: "模型设置已被更新，请刷新后重试。" }, 409));
    render(<ModelSettings canManage />);
    const model = await screen.findByLabelText("primary 模型名称");
    await user.type(model, "-draft");
    await user.click(screen.getByRole("button", { name: "检查配置与路由" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("你的草稿仍保留");
    expect(model).toHaveValue("original-model-draft");
    await user.click(probeButton());
    await waitFor(() => expect(screen.getAllByRole("alert")).toHaveLength(2));
    expect(screen.getAllByRole("alert")[1]).toHaveTextContent("草稿仍保留");
  });
});
