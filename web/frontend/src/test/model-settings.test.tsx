import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";
import type { ModelProfileSettings, ModelSettingsPayload, ModelSettingsResponse } from "../api/modelSettings";
import ModelSettings from "../components/ModelSettings";

const profile = (name = "primary", overrides: Partial<ModelProfileSettings> = {}): ModelProfileSettings => ({
  name, provider: "openai", base_url: "https://relay.example.com/v1", model: "test-model",
  has_api_key: true, wire_api: "responses", user_agent: "test-agent", max_tokens_param: "max_completion_tokens",
  context_window_tokens: 128000, max_input_tokens: 120000, max_output_tokens: 8000, output_reserve_tokens: 8000,
  tokenizer_model: "test-tokenizer", bpe_tokenizer_enabled: true, supports_thinking: true,
  thinking_param: "reasoning_effort", thinking_value: "high", fallbacks: [], ...overrides,
});
const settings = (overrides: Partial<ModelSettingsResponse> = {}): ModelSettingsResponse => ({
  revision: 1, configured: true, default_profile: "primary",
  profiles: [profile("primary", { fallbacks: ["backup"] }), profile("backup")],
  routes: { default: ["primary"], chat: ["primary", "backup"], coding: ["backup"] },
  provider_defaults: { openai: { base_url: "https://api.example.com/v1", model: "provider-model", max_tokens_param: "max_tokens" } },
  route_purposes: ["default", "chat", "coding", "summary", "hybrid"], ...overrides,
});
const json = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status, headers: { "Content-Type": "application/json" } });
const savedResponse = (payload: ModelSettingsPayload) => settings({
  revision: payload.revision + 1, default_profile: payload.default_profile, routes: payload.routes,
  profiles: payload.profiles.map((item) => {
    const sanitized = { ...item, has_api_key: true };
    delete (sanitized as Record<string, unknown>).api_key;
    return sanitized as ModelProfileSettings;
  }),
});

function mockApi(handler?: (init?: RequestInit) => Response | Promise<Response> | undefined) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    expect(String(input)).toBe("/api/settings/models");
    const response = handler?.(init);
    if (response) return response;
    return init?.method === "POST" ? json(savedResponse(JSON.parse(String(init.body)))) : json(settings());
  });
  vi.stubGlobal("fetch", fetchMock);
  return fetchMock;
}
const saveButton = () => screen.getByRole("button", { name: "保存模型设置" });
const posts = (fetchMock: ReturnType<typeof mockApi>) => fetchMock.mock.calls.filter(([, init]) => init?.method === "POST");
const lastPayload = (fetchMock: ReturnType<typeof mockApi>) => JSON.parse(String(posts(fetchMock).at(-1)?.[1]?.body)) as ModelSettingsPayload;

describe("hot model settings", () => {
  it("does not fetch or expose global settings for a non-admin", () => {
    const fetchMock = mockApi();
    render(<ModelSettings canManage={false} />);
    expect(screen.getByText(/模型设置由管理员统一管理/)).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(screen.queryByRole("button")).not.toBeInTheDocument();
    expect(screen.queryByLabelText(/API Key/)).not.toBeInTheDocument();
  });

  it("saves with the revision, retains advanced fields, and omits unchanged API keys", async () => {
    const user = userEvent.setup();
    const refresh = vi.fn(async () => undefined);
    const fetchMock = mockApi();
    render(<ModelSettings canManage onSaved={refresh} />);
    const model = await screen.findByRole("textbox", { name: "primary 模型名称" });
    await user.clear(model); await user.type(model, "next-model");
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    const payload = lastPayload(fetchMock);
    expect(payload.revision).toBe(1);
    expect(payload.default_profile).toBe("primary");
    const original: Record<string, unknown> = { ...profile("primary", { fallbacks: ["backup"] }) };
    delete original.has_api_key;
    expect(payload.profiles[0]).toEqual({ ...original, model: "next-model" });
    expect(payload.profiles.every((item) => !Object.hasOwn(item, "api_key") && !Object.hasOwn(item, "has_api_key"))).toBe(true);
    expect(payload.routes).toEqual(settings().routes);
    expect(refresh).toHaveBeenCalledOnce();
    expect(screen.getByText(/模型设置已保存，无需重启服务/)).toBeInTheDocument();
    expect(screen.getByText(/已运行的任务保持启动时的配置/)).toBeInTheDocument();
  });

  it.each(["_Relay", "-Relay", "Relay-1", "a".repeat(64)])("saves a permitted profile name %s", async (name) => {
    const user = userEvent.setup();
    const fetchMock = mockApi((init) => init?.method === "POST" ? undefined : json(settings({
      default_profile: name, profiles: [profile(name)], routes: { default: [name] },
    })));
    render(<ModelSettings canManage />);
    const model = await screen.findByRole("textbox", { name: `${name} 模型名称` });
    await user.clear(model); await user.type(model, "next-model");
    await user.click(saveButton());
    await waitFor(() => expect(posts(fetchMock)).toHaveLength(1));
    expect(lastPayload(fetchMock).profiles[0].name).toBe(name);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it.each(["bad.name", "bad name", "中文", "a".repeat(65)])("identifies the invalid profile %s without losing edits", async (name) => {
    const user = userEvent.setup();
    const fetchMock = mockApi((init) => init?.method === "POST" ? undefined : json(settings({
      default_profile: name, profiles: [profile(name)], routes: { default: [name] },
    })));
    render(<ModelSettings canManage />);
    const model = await screen.findByRole("textbox", { name: `${name} 模型名称` });
    await user.clear(model); await user.type(model, "unsaved-model");
    await user.click(saveButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(`配置名称「${name}」无效`);
    expect(posts(fetchMock)).toHaveLength(0);
    expect(model).toHaveValue("unsaved-model");
  });

  it("keeps keys out of local storage and clears replacement input after a successful save", async () => {
    const user = userEvent.setup();
    const store = vi.spyOn(Storage.prototype, "setItem");
    const fetchMock = mockApi();
    render(<ModelSettings canManage />);
    const key = await screen.findByLabelText("primary API Key");
    expect(key).toHaveAttribute("type", "password");
    expect(key).toHaveValue("");
    await user.type(key, "secret-for-test");
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(lastPayload(fetchMock).profiles[0].api_key).toBe("secret-for-test");
    expect(lastPayload(fetchMock).profiles[1]).not.toHaveProperty("api_key");
    expect(screen.getByLabelText("primary API Key")).toHaveValue("");
    expect(screen.queryByText("secret-for-test")).not.toBeInTheDocument();
    expect(store).not.toHaveBeenCalled();
    store.mockRestore();
  });

  it.each([400, 403, 409])("retains an unsaved draft and revision on HTTP %s", async (status) => {
    const user = userEvent.setup();
    const fetchMock = mockApi((init) => init?.method === "POST" ? json({ error: status === 409 ? "版本冲突" : "暂时不能保存" }, status) : undefined);
    render(<ModelSettings canManage />);
    const model = await screen.findByRole("textbox", { name: "primary 模型名称" });
    await user.clear(model); await user.type(model, "unsaved-model");
    await user.type(screen.getByLabelText("primary API Key"), "unsaved-key");
    await user.click(saveButton());
    expect(await screen.findByRole("alert")).toHaveTextContent(status === 409 ? "版本冲突" : "暂时不能保存");
    expect(model).toHaveValue("unsaved-model");
    expect(screen.getByLabelText("primary API Key")).toHaveValue("unsaved-key");
    expect(saveButton()).toBeEnabled();
    expect(lastPayload(fetchMock).revision).toBe(1);
    if (status === 409) expect(screen.getByRole("alert")).toHaveTextContent("你的草稿仍保留");
  });

  it("keeps draft values during a failed re-read and applies a successful explicit re-read", async () => {
    const user = userEvent.setup();
    let gets = 0;
    mockApi((init) => {
      if (init?.method === "POST") return json({ error: "版本冲突" }, 409);
      gets++;
      if (gets === 2) return json({ error: "暂时不可用" }, 503);
      return json(settings({ revision: gets === 1 ? 1 : 3, profiles: [profile("primary", { model: gets === 1 ? "initial-model" : "external-model" }), profile("backup")] }));
    });
    render(<ModelSettings canManage />);
    const model = await screen.findByRole("textbox", { name: "primary 模型名称" });
    await user.clear(model); await user.type(model, "draft-model");
    await user.click(saveButton());
    await screen.findByRole("alert");
    await user.click(screen.getByRole("button", { name: "放弃草稿并重新读取" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("暂时不可用");
    expect(model).toHaveValue("draft-model");
    await user.click(screen.getByRole("button", { name: "放弃草稿并重新读取" }));
    await waitFor(() => expect(screen.getByRole("textbox", { name: "primary 模型名称" })).toHaveValue("external-model"));
    expect(saveButton()).toBeDisabled();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("removes deleted profiles from task routes and profile fallbacks", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi();
    render(<ModelSettings canManage />);
    await user.click(await screen.findByRole("button", { name: "删除模型 backup" }));
    expect(screen.queryByRole("article", { name: "模型配置 backup" })).not.toBeInTheDocument();
    expect(screen.queryByRole("combobox", { name: "coding 路由第 1 个模型" })).not.toBeInTheDocument();
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    const payload = lastPayload(fetchMock);
    expect(payload.profiles).toHaveLength(1);
    expect(payload.profiles[0].fallbacks).toEqual([]);
    expect(payload.routes).toEqual({ default: ["primary"], chat: ["primary"] });
  });

  it("adds a new Provider with a key, adjusts ordered routing, and saves custom purposes", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi();
    render(<ModelSettings canManage />);
    await user.click(await screen.findByRole("button", { name: "添加模型配置" }));
    await user.clear(screen.getByLabelText("model_1 Provider"));
    await user.type(screen.getByLabelText("model_1 Provider"), "custom_relay");
    await user.clear(screen.getByRole("textbox", { name: "model_1 Base URL" }));
    await user.type(screen.getByRole("textbox", { name: "model_1 Base URL" }), "https://new.example.com/v1");
    await user.type(screen.getByLabelText("model_1 API Key"), "new-provider-key");
    await user.selectOptions(screen.getByRole("combobox", { name: "model_1 接口协议" }), "responses");
    await user.selectOptions(screen.getByRole("combobox", { name: "默认模型配置" }), "model_1");
    await user.click(screen.getByRole("button", { name: "chat 路由第 2 个模型上移" }));
    await user.click(screen.getByRole("button", { name: "添加 chat 路由模型" }));
    await user.type(screen.getByRole("textbox", { name: "自定义任务用途" }), "scheduler_analyze");
    await user.click(screen.getByRole("button", { name: "添加用途" }));
    await user.click(screen.getByRole("button", { name: "添加 scheduler_analyze 路由模型" }));
    await user.selectOptions(screen.getByRole("combobox", { name: "scheduler_analyze 路由第 1 个模型" }), "model_1");
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    const payload = lastPayload(fetchMock);
    expect(payload.default_profile).toBe("model_1");
    expect(payload.routes.default[0]).toBe("model_1");
    expect(payload.profiles[2]).toMatchObject({ name: "model_1", provider: "custom_relay", base_url: "https://new.example.com/v1", api_key: "new-provider-key", wire_api: "responses" });
    expect(payload.routes.chat).toEqual(["backup", "primary", "model_1"]);
    expect(payload.routes.scheduler_analyze).toEqual(["model_1"]);
    expect(payload.routes).not.toHaveProperty("summary");
  });

  it("shows a retry after an initial error and permits bootstrapping an empty configuration", async () => {
    const user = userEvent.setup();
    let gets = 0;
    const fetchMock = mockApi((init) => {
      if (init?.method === "POST") return undefined;
      return ++gets === 1 ? json({ error: "无法读取配置" }, 500) : json(settings({ configured: false, default_profile: "", profiles: [], routes: {}, error: "尚未配置模型" }));
    });
    render(<ModelSettings canManage />);
    expect(screen.getByLabelText("正在加载")).toBeInTheDocument();
    expect(await screen.findByRole("alert")).toHaveTextContent("无法读取配置");
    await user.click(screen.getByRole("button", { name: "重试读取模型设置" }));
    expect(await screen.findByText("暂无模型配置")).toBeInTheDocument();
    expect(screen.getByText(/尚未配置模型/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "添加模型配置" }));
    await user.type(screen.getByLabelText("model_1 API Key"), "bootstrap-key");
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(lastPayload(fetchMock).default_profile).toBe("model_1");
    expect(screen.queryByText("暂无模型配置")).not.toBeInTheDocument();
  });

  it("synchronizes the default selector with ordered default routing and retains fallbacks", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((init) => init?.method === "POST" ? undefined : json(settings({ routes: { default: ["primary", "backup"] } })));
    render(<ModelSettings canManage />);
    const selector = await screen.findByRole("combobox", { name: "默认模型配置" });
    await user.selectOptions(selector, "backup");
    expect(screen.getByRole("combobox", { name: "default 路由第 1 个模型" })).toHaveValue("backup");
    expect(screen.queryByRole("combobox", { name: "default 路由第 2 个模型" })).not.toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "添加 default 路由模型" }));
    await user.click(screen.getByRole("button", { name: "default 路由第 2 个模型上移" }));
    expect(selector).toHaveValue("primary");
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(lastPayload(fetchMock)).toMatchObject({ default_profile: "primary", routes: { default: ["primary", "backup"] } });
  });

  it("shows the effective inherited default route and synchronizes removing or deleting its first model", async () => {
    const user = userEvent.setup();
    const fetchMock = mockApi((init) => init?.method === "POST" ? undefined : json(settings({ default_profile: "primary", routes: { default: ["backup", "primary"] } })));
    render(<ModelSettings canManage />);
    const selector = await screen.findByRole("combobox", { name: "默认模型配置" });
    expect(selector).toHaveValue("backup");
    await user.click(screen.getByRole("button", { name: "移除 default 路由第 1 个模型" }));
    expect(selector).toHaveValue("primary");
    await user.click(screen.getByRole("button", { name: "删除模型 primary" }));
    expect(selector).toHaveValue("backup");
    expect(screen.getByRole("combobox", { name: "default 路由第 1 个模型" })).toHaveValue("backup");
    await user.click(saveButton());
    await waitFor(() => expect(saveButton()).toBeDisabled());
    expect(lastPayload(fetchMock)).toMatchObject({ default_profile: "backup", routes: { default: ["backup"] } });
  });

  it("waits for the server before applying a save and reports a failed option refresh as saved", async () => {
    const user = userEvent.setup();
    let finish!: (response: Response) => void;
    const pending = new Promise<Response>((resolve) => { finish = resolve; });
    const fetchMock = mockApi((init) => init?.method === "POST" ? pending : undefined);
    render(<ModelSettings canManage onSaved={async () => { throw new Error("health temporarily unavailable"); }} />);
    await user.type(await screen.findByLabelText("primary API Key"), "new-key");
    await user.click(saveButton());
    expect(screen.getByRole("button", { name: "正在保存…" })).toBeDisabled();
    expect(screen.getByLabelText("primary API Key")).toHaveValue("new-key");
    expect(screen.getByRole("button", { name: "删除模型 primary" })).toBeDisabled();
    await act(async () => finish(json(savedResponse(lastPayload(fetchMock)))));
    expect(await screen.findByText(/模型设置已保存，无需重启服务。模型选项刷新失败/)).toBeInTheDocument();
    expect(screen.getByLabelText("primary API Key")).toHaveValue("");
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(saveButton()).toBeDisabled();
  });
});
