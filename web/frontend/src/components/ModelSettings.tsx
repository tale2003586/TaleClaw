import { useCallback, useEffect, useRef, useState } from "react";
import { ArrowDown, ArrowUp, Check, Cpu, Plus, RefreshCw, Trash2 } from "lucide-react";
import { ApiError, getJson, postJson } from "../api/client";
import type { ModelProbeResponse, ModelProfileSettings, ModelSettingsPayload, ModelSettingsPreviewResponse, ModelSettingsResponse } from "../api/modelSettings";
import { Button, EmptyState, Skeleton } from "./ui";
import "../styles/model-settings.css";

interface ProfileDraft {
  id: number;
  saved: boolean;
  profile: ModelProfileSettings;
  apiKey: string;
  thinkingText: string;
  fallbacksText: string;
}

const endpoint = "/api/settings/models";
const messageOf = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);
const profileNames = (text: string) => text.split(/[,，\s]+/).map((name) => name.trim()).filter(Boolean);
const normalizedName = (name: string) => name.trim().toLowerCase().replaceAll("-", "_");
const normalizedTarget = (profile: ModelProfileSettings) => {
  try {
    const url = new URL(profile.base_url.trim());
    const path = profile.base_url.trim().match(/^[a-z][a-z0-9+.-]*:\/\/[^/?#]*(\/[^?#]*)?/i)?.[1] || "";
    return JSON.stringify([normalizedName(profile.name), normalizedName(profile.provider), url.protocol,
      url.hostname.toLowerCase().replace(/\.$/, ""), url.port || (url.protocol === "https:" ? "443" : "80"), path.replace(/\/+$/, "")]);
  } catch { return null; }
};
const purposeLabels: Record<string, string> = {
  default: "默认路由", chat: "对话", coding: "Coding", summary: "摘要", hybrid: "混合任务",
  compact: "上下文压缩", teammate: "协作任务", reflection: "反思", task_conclusion: "任务结论", security_router: "安全路由",
};
const numericFields = [
  ["context_window_tokens", "上下文窗口 Token"], ["max_input_tokens", "输入 Token 上限"],
  ["max_output_tokens", "输出 Token 上限"], ["output_reserve_tokens", "预留输出 Token"],
] as const;

export default function ModelSettings({ canManage, onSaved }: { canManage: boolean; onSaved?: () => Promise<void> }) {
  return <section className="settings-card settings-card-wide model-settings" aria-labelledby="model-settings-heading">
    <header><span><Cpu aria-hidden="true" /></span><div><h3 id="model-settings-heading">模型与 Provider</h3><p>管理服务端共享的模型、API Key 和任务路由，修改会影响所有账号。</p></div></header>
    {canManage ? <ModelManager onSaved={onSaved} /> : <p className="model-settings-help">模型设置由管理员统一管理，当前账号不能查看或修改 Provider 配置。</p>}
  </section>;
}

function ModelManager({ onSaved }: { onSaved?: () => Promise<void> }) {
  const [settings, setSettings] = useState<ModelSettingsResponse | null>(null);
  const [profiles, setProfiles] = useState<ProfileDraft[]>([]);
  const [defaultProfile, setDefaultProfile] = useState("");
  const [routes, setRoutes] = useState<Record<string, string[]>>({});
  const [customPurposes, setCustomPurposes] = useState<string[]>([]);
  const [newPurpose, setNewPurpose] = useState("");
  const [dirty, setDirty] = useState(false);
  const [busy, setBusy] = useState<"load" | "save" | null>("load");
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const [notice, setNotice] = useState("");
  const [preview, setPreview] = useState<ModelSettingsPreviewResponse | null>(null);
  const [checking, setChecking] = useState(false);
  const [probeStates, setProbeStates] = useState<Record<number, { pending?: boolean; result?: ModelProbeResponse; error?: string }>>({});
  const draftVersion = useRef(0);
  const sequence = useRef(0);
  const mounted = useRef(true);
  const reloadController = useRef<AbortController | null>(null);

  const applySettings = useCallback((data: ModelSettingsResponse) => {
    draftVersion.current++;
    setPreview(null);
    setProbeStates({});
    const drafts = data.profiles.map((profile) => {
      const sanitized = { ...profile };
      delete sanitized.api_key;
      return {
        id: ++sequence.current, saved: true, profile: sanitized, apiKey: "",
        thinkingText: JSON.stringify(profile.thinking_value ?? true), fallbacksText: (profile.fallbacks || []).join(", "),
      };
    });
    setSettings(data);
    setProfiles(drafts);
    setDefaultProfile(data.routes.default?.[0] || data.default_profile);
    setRoutes(data.routes);
    setCustomPurposes([]);
    setNewPurpose("");
    setDirty(false);
    setConflict(false);
  }, []);

  const load = useCallback(async () => {
    reloadController.current?.abort();
    const controller = new AbortController();
    reloadController.current = controller;
    setBusy("load"); setError(""); setNotice("");
    try {
      const data = await getJson<ModelSettingsResponse>(endpoint, { signal: controller.signal });
      if (!controller.signal.aborted) applySettings(data);
    } catch (reason) {
      if (!controller.signal.aborted) setError(messageOf(reason));
    } finally {
      if (!controller.signal.aborted) setBusy(null);
    }
  }, [applySettings]);

  useEffect(() => {
    mounted.current = true;
    void load();
    return () => { mounted.current = false; reloadController.current?.abort(); };
  }, [load]);

  const markDirty = () => {
    draftVersion.current++;
    setDirty(true); setError(""); setNotice(""); setPreview(null); setProbeStates({});
  };
  const updateProfile = (id: number, patch: Partial<ProfileDraft>) => {
    setProfiles((current) => current.map((draft) => draft.id === id ? { ...draft, ...patch } : draft));
    markDirty();
  };
  const updateRoutes = (purpose: string, chain: string[]) => {
    const nextChain = purpose === "default" ? Array.from(new Set(chain)) : chain;
    setRoutes((current) => ({ ...current, [purpose]: nextChain }));
    if (purpose === "default" && nextChain.length) setDefaultProfile(nextChain[0]);
    markDirty();
  };
  const changeDefault = (name: string) => {
    setDefaultProfile(name);
    setRoutes((current) => ({ ...current, default: Array.from(new Set([name, ...(current.default || []).slice(1).filter((item) => item !== name)])) }));
    markDirty();
  };

  const addProfile = () => {
    if (!settings) return;
    let index = 1;
    while (profiles.some(({ profile }) => profile.name === `model_${index}`)) index++;
    const provider = settings.provider_defaults.openai ? "openai" : Object.keys(settings.provider_defaults)[0] || "openai";
    const defaults = settings.provider_defaults[provider];
    const name = `model_${index}`;
    setProfiles((current) => [...current, {
      id: ++sequence.current, saved: false, apiKey: "", thinkingText: "true", fallbacksText: "",
      profile: {
        name, provider, base_url: defaults?.base_url || "", model: defaults?.model || "",
        max_tokens_param: defaults?.max_tokens_param || "max_tokens", wire_api: "chat_completions", has_api_key: false,
      },
    }]);
    if (!profiles.some(({ profile }) => profile.name === defaultProfile)) {
      setDefaultProfile(name);
      setRoutes((current) => ({ ...current, default: [name] }));
    }
    markDirty();
  };

  const removeProfile = (id: number) => {
    const removed = profiles.find((draft) => draft.id === id);
    if (!removed) return;
    const remaining = profiles.filter((draft) => draft.id !== id);
    setProfiles(remaining.map((draft) => ({
      ...draft, fallbacksText: profileNames(draft.fallbacksText).filter((name) => name !== removed.profile.name).join(", "),
    })));
    const nextRoutes = Object.fromEntries(Object.entries(routes).map(([purpose, chain]) => [purpose, chain.filter((name) => name !== removed.profile.name)]));
    const nextDefault = nextRoutes.default?.[0] || (defaultProfile === removed.profile.name ? remaining[0]?.profile.name || "" : defaultProfile);
    if (nextDefault && !nextRoutes.default?.length) nextRoutes.default = [nextDefault];
    setRoutes(nextRoutes);
    setDefaultProfile(nextDefault);
    markDirty();
  };

  const renameProfile = (id: number, name: string) => {
    const previous = profiles.find((draft) => draft.id === id)?.profile.name;
    if (previous === undefined) return;
    setProfiles((current) => current.map((draft) => ({
      ...draft,
      profile: draft.id === id ? { ...draft.profile, name } : draft.profile,
      fallbacksText: profileNames(draft.fallbacksText).map((item) => item === previous ? name : item).join(", "),
    })));
    setRoutes((current) => Object.fromEntries(Object.entries(current).map(([purpose, chain]) => [purpose, chain.map((item) => item === previous ? name : item)])));
    if (defaultProfile === previous) setDefaultProfile(name);
    markDirty();
  };

  const canRetainKey = (profile: ModelProfileSettings) => {
    const original = settings?.profiles.find((item) => normalizedName(item.name) === normalizedName(profile.name));
    return Boolean(original?.has_api_key && normalizedTarget(profile) && normalizedTarget(original) === normalizedTarget(profile));
  };

  const profilePayload = ({ profile, apiKey, thinkingText, fallbacksText }: ProfileDraft) => {
    const value: Record<string, unknown> = { ...profile };
    delete value.has_api_key;
    delete value.api_key;
    value.name = profile.name.trim(); value.provider = profile.provider.trim();
    value.base_url = profile.base_url.trim(); value.model = profile.model.trim();
    if (!value.provider || !value.base_url || !value.model) throw new Error(`${value.name} 的 Provider、Base URL 和模型名称不能为空。`);
    if (!canRetainKey(profile) && !apiKey.trim()) throw new Error(`请为 ${value.name} 填写 API Key；目标已修改时需要重新填写。`);
    if (apiKey.trim()) value.api_key = apiKey.trim();
    try { value.thinking_value = JSON.parse(thinkingText); }
    catch { throw new Error(`${value.name} 的思考参数值必须是有效的 JSON，例如 true 或 "high"。`); }
    value.fallbacks = profileNames(fallbacksText);
    return value;
  };

  const buildPayload = (): ModelSettingsPayload => {
    if (!settings) throw new Error("请先读取模型设置。");
    const names = profiles.map(({ profile }) => normalizedName(profile.name));
    if (!names.length) throw new Error("请至少配置一个模型。");
    if (names.some((name) => !/^[a-z0-9][a-z0-9_]{0,63}$/.test(name))) throw new Error("配置名称只能包含字母、数字、下划线和短横线，长度最多 64 个字符。");
    if (new Set(names).size !== names.length) throw new Error("模型配置名称不能重复。");
    if (!names.includes(normalizedName(defaultProfile))) throw new Error("请选择有效的默认模型。");
    const payload = {
      revision: settings.revision, default_profile: defaultProfile.trim(), profiles: profiles.map(profilePayload),
      routes: Object.fromEntries(Object.entries(routes).filter(([, chain]) => chain.length).map(([purpose, chain]) => [purpose, chain.map((name) => name.trim())])),
    };
    if (payload.profiles.some((item) => (item.fallbacks as string[]).some((name) => !names.includes(normalizedName(name))))) throw new Error("备用配置包含不存在的模型。");
    if (Object.values(payload.routes).some((chain) => chain.some((name) => !names.includes(normalizedName(name))))) throw new Error("任务路由包含不存在的模型，请重新选择。");
    return payload;
  };

  const checkRoutes = async () => {
    if (!settings || busy || checking) return;
    let payload: ModelSettingsPayload;
    try { payload = buildPayload(); }
    catch (reason) { setError(messageOf(reason)); return; }
    const version = draftVersion.current;
    setChecking(true); setError(""); setNotice(""); setPreview(null);
    try {
      const data = await postJson<ModelSettingsPreviewResponse>(`${endpoint}/preview`, payload);
      if (mounted.current && version === draftVersion.current) setPreview(data);
    } catch (reason) {
      if (mounted.current && version === draftVersion.current) {
        setConflict(reason instanceof ApiError && reason.status === 409); setError(messageOf(reason));
      }
    } finally { if (mounted.current) setChecking(false); }
  };

  const probeProfile = async (draft: ProfileDraft) => {
    if (!settings || busy || probeStates[draft.id]?.pending) return;
    let submitted: Record<string, unknown>;
    try { submitted = profilePayload(draft); }
    catch (reason) { setProbeStates((current) => ({ ...current, [draft.id]: { error: messageOf(reason) } })); return; }
    const version = draftVersion.current;
    setProbeStates((current) => ({ ...current, [draft.id]: { pending: true } }));
    try {
      const result = await postJson<ModelProbeResponse>(`${endpoint}/probe`, { revision: settings.revision, profile: submitted });
      if (mounted.current && version === draftVersion.current) setProbeStates((current) => ({ ...current, [draft.id]: { result } }));
    } catch (reason) {
      if (mounted.current && version === draftVersion.current) {
        const stale = reason instanceof ApiError && reason.status === 409;
        setProbeStates((current) => ({ ...current, [draft.id]: { error: stale ? "服务端版本已变化，草稿仍保留；请重新读取设置后重试。" : messageOf(reason) } }));
      }
    }
  };

  const save = async () => {
    if (!settings || !dirty || busy) return;
    setError(""); setNotice("");
    let payload: ModelSettingsPayload;
    try { payload = buildPayload(); }
    catch (reason) { setError(messageOf(reason)); return; }
    setBusy("save");
    try {
      const data = await postJson<ModelSettingsResponse>(endpoint, payload);
      if (!mounted.current) return;
      applySettings(data);
      setNotice("模型设置已保存，无需重启服务。后续任务使用新配置，已运行的任务保持原配置。");
      try { await onSaved?.(); }
      catch { if (mounted.current) setNotice("模型设置已保存，无需重启服务。模型选项刷新失败，请刷新网页读取最新选项。"); }
    } catch (reason) {
      if (mounted.current) {
        setConflict(reason instanceof ApiError && reason.status === 409);
        setError(messageOf(reason));
      }
    } finally { if (mounted.current) setBusy(null); }
  };

  const purposes = Array.from(new Set([...(settings?.route_purposes || []), ...Object.keys(routes), ...customPurposes]));
  const names = profiles.map(({ profile }) => profile.name);
  const addPurpose = () => {
    const purpose = newPurpose.trim().toLowerCase();
    if (!/^[a-z0-9_-]+$/.test(purpose)) { setError("任务用途只能包含字母、数字、下划线和短横线。"); return; }
    if (purposes.includes(purpose)) { setError("该任务用途已在路由列表中。"); return; }
    setCustomPurposes((current) => [...current, purpose]); setNewPurpose("");
    markDirty();
  };

  return <>
    <p className="model-settings-help">保存后无需重启服务，后续任务会使用新配置；已运行的任务保持启动时的配置。</p>
    <p className="model-settings-help">API Key 不会回显或保存在浏览器本地；配置名称、Provider 和接口地址均未改变时，留空保留原 Key。更换目标需要重新填写 Key。</p>
    <div className="model-settings-toolbar"><span>{settings ? `${profiles.length} 个模型配置${dirty ? " · 有未保存的修改" : ""}` : "读取模型设置"}</span><Button type="button" onClick={() => void load()} disabled={Boolean(busy)}><RefreshCw aria-hidden="true" size={14} />{busy === "load" ? "正在读取…" : dirty ? "放弃草稿并重新读取" : "重新读取设置"}</Button></div>
    {notice && <div className="settings-success" role="status"><Check aria-hidden="true" /><span>{notice}</span></div>}
    {error && <div className="model-settings-error" role="alert"><strong>{settings ? "操作未完成" : "模型设置读取失败"}</strong><p>{error}</p>{settings && <small>{conflict ? "服务端版本已变化，你的草稿仍保留。请记录需要保留的修改，再点击“放弃草稿并重新读取”获取最新配置。" : "未保存的修改仍保留，请修正后重试。"}</small>}{!settings && <Button type="button" disabled={Boolean(busy)} onClick={() => void load()}>重试读取模型设置</Button>}</div>}
    {busy === "load" && !settings && <Skeleton lines={5} />}
    {settings && <form onSubmit={(event) => { event.preventDefault(); void save(); }}>
      <fieldset className="model-settings-fields" disabled={Boolean(busy)}>
        {settings.error && <p className="model-settings-warning">当前服务端模型配置不可用：{settings.error}。修正并保存后可恢复后续任务。</p>}
        {profiles.length === 0 && <EmptyState title="暂无模型配置" message="添加 Provider、模型地址和 API Key，再保存即可启用。" />}
        <div className="model-profile-list">{profiles.map((draft, index) => <ModelProfileEditor key={draft.id} draft={draft} index={index} providerDefaults={settings.provider_defaults} retainsKey={canRetainKey(draft.profile)} probeState={probeStates[draft.id]} onProbe={() => void probeProfile(draft)} onChange={(patch) => updateProfile(draft.id, patch)} onRename={(name) => renameProfile(draft.id, name)} onRemove={() => removeProfile(draft.id)} />)}</div>
        <div className="model-settings-toolbar"><Button type="button" onClick={addProfile}><Plus aria-hidden="true" size={14} />添加模型配置</Button></div>
        {profiles.length > 0 && <>
          <label className="field model-default-field"><span>默认模型配置</span><select aria-label="默认模型配置" value={defaultProfile} onChange={(event) => changeDefault(event.target.value)} required><option value="" disabled>请选择模型</option>{names.map((name, index) => <option key={index} value={name}>{name || "未命名配置"}</option>)}</select><small>同时设为默认路由的首选模型。</small></label>
          <section className="model-routes" aria-labelledby="model-routes-heading"><h4 id="model-routes-heading">任务路由</h4><p className="model-settings-help">从左至右依次尝试首选及备用模型。未设置的用途继承默认路由；默认路由为空时使用默认模型配置。</p>
            {purposes.map((purpose) => <RouteEditor key={purpose} purpose={purpose} chain={routes[purpose] || []} names={names} preferredName={purpose === "default" ? defaultProfile : undefined} onChange={(chain) => updateRoutes(purpose, chain)} />)}
            <div className="model-custom-route"><label className="field"><span>自定义任务用途</span><input value={newPurpose} onChange={(event) => setNewPurpose(event.target.value)} autoComplete="off" placeholder="例如 scheduler_analyze" /></label><Button type="button" disabled={!newPurpose.trim()} onClick={addPurpose}><Plus aria-hidden="true" size={14} />添加用途</Button></div>
          </section>
        </>}
        <div className="button-row model-settings-actions"><span className="model-settings-help">检查仅在本地校验草稿；保存后才应用配置。</span><Button type="button" onClick={() => void checkRoutes()} disabled={checking || profiles.length === 0}>{checking ? "正在检查…" : "检查配置与路由"}</Button><Button className="primary" type="submit" disabled={!dirty || profiles.length === 0}>{busy === "save" ? "正在保存…" : "保存模型设置"}</Button></div>
        {preview && <section className="model-route-preview" aria-label="有效路由预览"><h4>草稿校验通过 · 有效路由</h4><p className="model-settings-help">包含用途继承与配置备用模型，按实际尝试顺序展示；草稿修改后需重新检查。</p>{preview.routes.map((route) => <div className="model-route-preview-row" key={route.purpose}><strong>{purposeLabels[route.purpose] || route.purpose} <small>{route.purpose}</small></strong><span>{route.source === "explicit" ? "直接配置" : route.source === "alias" ? `继承 ${route.source_purpose} 用途` : route.source === "default" ? "继承默认路由" : "使用默认模型"}</span><ol>{route.profiles.map((profile, index) => <li key={profile.name}><b>{index === 0 ? "首选" : "备用"} · {profile.name}</b><small>{profile.provider} / {profile.model}{profile.source === "profile_fallback" ? ` · ${profile.fallback_of} 的备用配置` : ""}</small></li>)}</ol></div>)}</section>}
      </fieldset>
    </form>}
  </>;
}

function ModelProfileEditor({ draft, index, providerDefaults, retainsKey, probeState, onProbe, onChange, onRename, onRemove }: {
  draft: ProfileDraft; index: number; providerDefaults: ModelSettingsResponse["provider_defaults"];
  retainsKey: boolean; probeState?: { pending?: boolean; result?: ModelProbeResponse; error?: string }; onProbe: () => void;
  onChange: (patch: Partial<ProfileDraft>) => void; onRename: (name: string) => void; onRemove: () => void;
}) {
  const profile = draft.profile;
  const label = profile.name || `配置 ${index + 1}`;
  const update = (patch: Partial<ModelProfileSettings>) => onChange({ profile: { ...profile, ...patch } });
  return <article className="model-profile" aria-label={`模型配置 ${label}`}>
    <div className="model-profile-heading"><h4>{label}</h4><Button type="button" className="model-delete" onClick={onRemove} aria-label={`删除模型 ${label}`}><Trash2 aria-hidden="true" size={14} />删除</Button></div>
    <div className="model-profile-fields">
      <label className="field"><span>配置名称</span><input aria-label={`${label} 配置名称`} value={profile.name} onChange={(event) => onRename(event.target.value)} readOnly={draft.saved} required autoComplete="off" spellCheck={false} /><small>{draft.saved ? "此标识用于路由和 Key 绑定" : "使用字母、数字、下划线或短横线"}</small></label>
      <label className="field"><span>Provider</span><input aria-label={`${label} Provider`} list="model-provider-options" value={profile.provider} onChange={(event) => update({ provider: event.target.value })} required autoComplete="off" spellCheck={false} /><small>支持自定义 OpenAI 兼容服务</small></label>
      <label className="field"><span>Base URL</span><input aria-label={`${label} Base URL`} type="url" value={profile.base_url} onChange={(event) => update({ base_url: event.target.value })} required autoComplete="off" spellCheck={false} placeholder="https://example.com/v1" /></label>
      <label className="field"><span>模型名称</span><input aria-label={`${label} 模型名称`} value={profile.model} onChange={(event) => update({ model: event.target.value })} required autoComplete="off" spellCheck={false} /></label>
      <label className="field"><span>API Key</span><input aria-label={`${label} API Key`} type="password" value={draft.apiKey} onChange={(event) => onChange({ apiKey: event.target.value })} autoComplete="new-password" spellCheck={false} autoCapitalize="none" placeholder={retainsKey ? "已配置；留空保留原 Key" : "填写 API Key"} required={!retainsKey} /><small>{retainsKey ? "已配置 · 输入新 Key 后保存即可替换" : profile.has_api_key ? "接口目标已修改 · 必须重新填写 Key" : "未配置 · 新模型需要填写 Key"}</small></label>
      <label className="field"><span>接口协议（wire_api）</span><select aria-label={`${label} 接口协议`} value={profile.wire_api} onChange={(event) => update({ wire_api: event.target.value })}><option value="chat_completions">Chat Completions</option><option value="responses">Responses</option></select></label>
    </div>
    {!draft.saved && providerDefaults[profile.provider] && <Button className="model-provider-defaults" type="button" onClick={() => update({ ...providerDefaults[profile.provider], wire_api: "chat_completions" })}>填入 {profile.provider} 默认地址和模型</Button>}
    <div className="model-probe"><Button type="button" onClick={onProbe} disabled={probeState?.pending} aria-label={`测试 ${label} 连接并发现模型`}>{probeState?.pending ? "正在获取模型…" : "测试连接并发现模型"}</Button><small>点击才请求此接口的 /models，使用当前草稿；不会调用模型生成。</small>
      {probeState?.error && <p className="model-probe-error" role="alert">{probeState.error}</p>}
      {probeState?.result && <div className={`model-probe-result model-probe-${probeState.result.status}`} role="status"><p>{probeState.result.message} <small>耗时 {probeState.result.elapsed_ms} ms</small></p>{probeState.result.models.length > 0 && <label className="field"><span>发现的模型</span><select aria-label={`${label} 发现的模型`} value="" onChange={(event) => update({ model: event.target.value })}><option value="">选择后填入模型名称</option>{probeState.result.models.map((model) => <option key={model} value={model}>{model}</option>)}</select></label>}{probeState.result.truncated && <small>列表最多展示 200 个模型；可手动填写其他名称。</small>}</div>}
    </div>
    <details className="model-advanced"><summary>高级配置</summary><div className="model-profile-fields">
      <label className="field"><span>User Agent</span><input aria-label={`${label} User Agent`} value={profile.user_agent || ""} onChange={(event) => update({ user_agent: event.target.value })} autoComplete="off" /></label>
      <label className="field"><span>输出长度参数</span><select aria-label={`${label} 输出长度参数`} value={profile.max_tokens_param || "max_tokens"} onChange={(event) => update({ max_tokens_param: event.target.value })}><option value="max_tokens">max_tokens</option><option value="max_completion_tokens">max_completion_tokens</option>{profile.max_tokens_param && !["max_tokens", "max_completion_tokens"].includes(profile.max_tokens_param) && <option value={profile.max_tokens_param}>{profile.max_tokens_param}</option>}</select></label>
      {numericFields.map(([field, title]) => <label className="field" key={field}><span>{title}</span><input aria-label={`${label} ${title}`} type="number" min={1} step={1} value={profile[field] ?? ""} onChange={(event) => update({ [field]: event.target.value === "" ? null : Number(event.target.value) })} placeholder="服务端自动计算" /></label>)}
      <label className="field"><span>Tokenizer 模型</span><input aria-label={`${label} Tokenizer 模型`} value={profile.tokenizer_model || ""} onChange={(event) => update({ tokenizer_model: event.target.value })} autoComplete="off" /></label>
      <label className="field"><span>备用配置名称</span><input aria-label={`${label} 备用配置名称`} value={draft.fallbacksText} onChange={(event) => onChange({ fallbacksText: event.target.value })} autoComplete="off" placeholder="配置名称，使用逗号分隔" /></label>
      <label className="model-check"><input aria-label={`${label} 启用 BPE Tokenizer`} type="checkbox" checked={Boolean(profile.bpe_tokenizer_enabled)} onChange={(event) => update({ bpe_tokenizer_enabled: event.target.checked })} /><span>启用 BPE Tokenizer</span></label>
      <label className="model-check"><input aria-label={`${label} 支持思考模式`} type="checkbox" checked={Boolean(profile.supports_thinking)} onChange={(event) => update({ supports_thinking: event.target.checked })} /><span>支持思考模式</span></label>
      <label className="field"><span>思考参数名</span><input aria-label={`${label} 思考参数名`} value={profile.thinking_param || ""} onChange={(event) => update({ thinking_param: event.target.value })} autoComplete="off" placeholder="例如 reasoning_effort" required={Boolean(profile.supports_thinking)} /></label>
      <label className="field"><span>思考参数值（JSON）</span><input aria-label={`${label} 思考参数值`} value={draft.thinkingText} onChange={(event) => onChange({ thinkingText: event.target.value })} autoComplete="off" spellCheck={false} placeholder={'例如 true 或 "high"'} /></label>
    </div></details>
    {index === 0 && <datalist id="model-provider-options">{Object.keys(providerDefaults).map((provider) => <option key={provider} value={provider} />)}</datalist>}
  </article>;
}

function RouteEditor({ purpose, chain, names, preferredName, onChange }: { purpose: string; chain: string[]; names: string[]; preferredName?: string; onChange: (chain: string[]) => void }) {
  const label = purposeLabels[purpose] ? `${purposeLabels[purpose]}（${purpose}）` : purpose;
  const move = (index: number, step: number) => {
    const next = [...chain];
    [next[index], next[index + step]] = [next[index + step], next[index]];
    onChange(next);
  };
  return <div className="model-route-row"><div className="model-route-title"><strong>{label}</strong>{chain.length === 0 && <small>{purpose === "default" ? "使用默认模型配置" : "继承默认路由"}</small>}</div><div className="model-route-chain">
    {chain.map((name, index) => <div className="model-route-step" key={index}><select aria-label={`${purpose} 路由第 ${index + 1} 个模型`} value={name} onChange={(event) => onChange(chain.map((item, position) => position === index ? event.target.value : item))}>{!names.includes(name) && <option value={name}>{name}（已不存在）</option>}{names.map((profile, position) => <option key={position} value={profile}>{profile || "未命名配置"}</option>)}</select><Button type="button" aria-label={`${purpose} 路由第 ${index + 1} 个模型上移`} disabled={index === 0} onClick={() => move(index, -1)}><ArrowUp aria-hidden="true" size={13} /></Button><Button type="button" aria-label={`${purpose} 路由第 ${index + 1} 个模型下移`} disabled={index === chain.length - 1} onClick={() => move(index, 1)}><ArrowDown aria-hidden="true" size={13} /></Button><Button type="button" aria-label={`移除 ${purpose} 路由第 ${index + 1} 个模型`} onClick={() => onChange(chain.filter((_, position) => position !== index))}><Trash2 aria-hidden="true" size={13} /></Button></div>)}
    <Button type="button" disabled={chain.length >= names.length} aria-label={`添加 ${purpose} 路由模型`} onClick={() => onChange([...chain, !chain.length && preferredName && names.includes(preferredName) ? preferredName : names.find((name) => !chain.includes(name)) || names[0]])}><Plus aria-hidden="true" size={13} />{chain.length ? "备用模型" : "设置模型"}</Button>
  </div></div>;
}
