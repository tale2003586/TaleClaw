import { useCallback, useEffect, useRef, useState } from "react";
import { Check, Puzzle, RefreshCw } from "lucide-react";
import { getJson, postJson } from "../api/client";
import type { SkillCatalogResponse, SkillDetailResponse, SkillSummary } from "../api/types";
import { Button, EmptyState, Skeleton } from "./ui";

const messageOf = (reason: unknown) => reason instanceof Error ? reason.message : String(reason);

export default function SkillsSettings({ canManage }: { canManage: boolean }) {
  return <section className="settings-card settings-card-wide skills-settings" aria-labelledby="skills-heading">
    <header><span><Puzzle aria-hidden="true" /></span><div><h3 id="skills-heading">服务端技能</h3><p>管理服务端共享的技能目录，修改会影响所有账号。</p></div></header>
    {canManage ? <SkillManager /> : <p className="skills-help">技能由管理员统一管理，当前账号不能查看或修改技能设置。</p>}
  </section>;
}

function SkillManager() {
  const [catalog, setCatalog] = useState<SkillCatalogResponse | null>(null);
  const [catalogLoading, setCatalogLoading] = useState(true);
  const [catalogError, setCatalogError] = useState("");
  const [selectedId, setSelectedId] = useState("");
  const [detail, setDetail] = useState<SkillDetailResponse | null>(null);
  const [draft, setDraft] = useState("");
  const [detailLoading, setDetailLoading] = useState(false);
  const [detailError, setDetailError] = useState("");
  const [busy, setBusy] = useState<"reload" | "save" | "toggle" | null>(null);
  const [actionError, setActionError] = useState("");
  const [notice, setNotice] = useState("");
  const detailController = useRef<AbortController | null>(null);
  const dirty = detail !== null && draft !== detail.content;
  const selected = catalog?.skills.find((skill) => skill.id === selectedId);
  const changedOnServer = Boolean(detail && selected && !catalogError && !catalogLoading && !busy && detail.revision !== selected.revision);

  const loadCatalog = useCallback(async (signal?: AbortSignal) => {
    setCatalogLoading(true);
    setCatalogError("");
    try {
      const data = await getJson<SkillCatalogResponse>("/api/skills", { signal });
      if (!signal?.aborted) setCatalog(data);
      return data;
    } catch (reason) {
      if (!signal?.aborted) setCatalogError(messageOf(reason));
      throw reason;
    } finally {
      if (!signal?.aborted) setCatalogLoading(false);
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void loadCatalog(controller.signal).catch(() => undefined);
    return () => { controller.abort(); detailController.current?.abort(); };
  }, [loadCatalog]);

  const loadDetail = async (id: string) => {
    detailController.current?.abort();
    const controller = new AbortController();
    detailController.current = controller;
    setSelectedId(id);
    setDetailLoading(true);
    setDetailError("");
    setDetail(null);
    setDraft("");
    try {
      const data = await getJson<SkillDetailResponse>(`/api/skills/detail?id=${encodeURIComponent(id)}`, { signal: controller.signal });
      if (!controller.signal.aborted) { setDetail(data); setDraft(data.content); }
    } catch (reason) {
      if (!controller.signal.aborted) setDetailError(messageOf(reason));
    } finally {
      if (!controller.signal.aborted) setDetailLoading(false);
    }
  };

  const selectSkill = (id: string) => {
    if (busy || detailLoading || dirty) return;
    setNotice("");
    setActionError("");
    void loadDetail(id);
  };

  const reload = async () => {
    setBusy("reload"); setActionError(""); setNotice("");
    try {
      const data = await postJson<SkillCatalogResponse>("/api/skills/reload", {});
      setCatalog(data); setCatalogError("");
      if (dirty) {
        setNotice("目录已刷新，未保存的编辑仍保留。保存时会检查版本是否冲突。");
      } else {
        setNotice("技能目录已刷新。");
        if (selectedId) await loadDetail(selectedId);
      }
    } catch (reason) { setActionError(messageOf(reason)); }
    finally { setBusy(null); }
  };

  const refreshAfterUpdate = async () => {
    try { await loadCatalog(); }
    catch { setNotice("修改已保存，但目录刷新失败。请重试刷新目录。"); }
  };

  const toggle = async (skill: SkillSummary) => {
    setBusy("toggle"); setActionError(""); setNotice("");
    try {
      const data = await postJson<SkillDetailResponse>("/api/skills/update", {
        id: skill.id, revision: skill.revision, enabled: !skill.enabled,
      });
      if (selectedId === skill.id) { setDetail(data); setDraft(data.content); }
      setNotice(`${skill.name} 已${skill.enabled ? "停用" : "启用"}。后续搜索和加载将使用新设置。`);
      await refreshAfterUpdate();
    } catch (reason) { setActionError(messageOf(reason)); }
    finally { setBusy(null); }
  };

  const save = async () => {
    if (!detail || !dirty) return;
    setBusy("save"); setActionError(""); setNotice("");
    try {
      const data = await postJson<SkillDetailResponse>("/api/skills/update", {
        id: detail.id, revision: detail.revision, content: draft,
      });
      setDetail(data); setDraft(data.content);
      setNotice("技能内容已保存。后续搜索和加载将使用新内容。");
      await refreshAfterUpdate();
    } catch (reason) { setActionError(messageOf(reason)); }
    finally { setBusy(null); }
  };

  const discard = () => {
    setActionError(""); setNotice("");
    void loadDetail(selectedId);
  };
  const controlsBusy = Boolean(busy) || catalogLoading || detailLoading;

  return <>
    <p className="skills-help">默认目录：服务端项目下的 <code>skills/</code>。启停无需重启服务。</p>
    <p className="skills-help">启停和编辑对后续搜索、加载生效。已经进入现有对话的技能文本不会自动撤回；新建会话可完整采用最新设置。</p>
    <div className="skills-toolbar"><span>{catalog ? `${catalog.skills.length} 个技能 · ${catalog.skills.filter((skill) => skill.enabled && skill.valid).length} 个启用且有效` : "读取技能目录"}</span><Button onClick={() => void reload()} disabled={controlsBusy}><RefreshCw aria-hidden="true" size={14} />{busy === "reload" ? "正在刷新…" : "刷新目录"}</Button></div>
    {notice && <div className="settings-success" role="status"><Check aria-hidden="true" /><span>{notice}</span></div>}
    {actionError && <div className="skills-error" role="alert"><strong>操作未完成</strong><p>{actionError}</p><small>未保存的内容仍保留。若版本已变化，请复制需要保留的内容，再放弃修改并重新读取。</small></div>}
    {catalogError && <div className="skills-error" role="alert"><strong>目录加载失败</strong><p>{catalogError}</p><Button onClick={() => void loadCatalog().catch(() => undefined)} disabled={controlsBusy}>重试读取目录</Button></div>}
    {catalogLoading && !catalog && <Skeleton lines={4} />}
    {catalog && catalog.skills.length === 0 && <EmptyState title="暂无技能" message="将包含 SKILL.md 的技能目录放入服务端 skills/ 后，点击刷新目录。" />}
    {catalog && (catalog.skills.length > 0 || selectedId) && <div className="skills-layout">
      <div className="skills-list" aria-label="技能列表">
        {catalog.skills.map((skill) => <article className={`skill-row ${selectedId === skill.id ? "selected" : ""}`} key={skill.id}>
          <button type="button" className="skill-select" onClick={() => selectSkill(skill.id)} disabled={controlsBusy || (dirty && selectedId !== skill.id)} aria-pressed={selectedId === skill.id} aria-label={`编辑技能 ${skill.name}`}><strong>{skill.name}</strong><span>{skill.description || "暂无描述"}</span><small>{skill.path}</small></button>
          <div className="skill-row-actions"><span className={`skill-status ${!skill.valid ? "invalid" : ""}`}>{!skill.valid ? "配置无效" : skill.enabled ? "已启用" : "已停用"}</span><Button onClick={() => void toggle(skill)} disabled={controlsBusy || dirty || !skill.valid} title={!skill.valid ? "请先编辑并修复技能配置" : undefined} aria-label={`${skill.enabled ? "停用" : "启用"} ${skill.name}`}>{skill.enabled ? "停用" : "启用"}</Button></div>
          {!skill.valid && <p className="skills-help">请先编辑并修复配置，再调整启停状态。</p>}
          {skill.diagnostics.length > 0 && <ul className="skill-diagnostics">{skill.diagnostics.map((item, index) => <li key={index}>{item}</li>)}</ul>}
        </article>)}
      </div>
      <div className="skill-editor">
        {!selectedId && <p className="skills-help">选择技能查看和编辑 SKILL.md。</p>}
        {detailLoading && <Skeleton lines={6} />}
        {detailError && <div className="skills-error" role="alert"><strong>技能读取失败</strong><p>{detailError}</p><Button onClick={() => void loadDetail(selectedId)} disabled={controlsBusy}>重新读取技能</Button></div>}
        {detail && <>
          <div className="skill-editor-heading"><h4>{selected?.name || detail.id}</h4><span>SKILL.md</span></div>
          {selected && <p className="skills-help">适用范围：{selected.applies_to.join("、") || "未指定"} · 依赖工具：{selected.requires_tools.join("、") || "未指定"}</p>}
          {dirty && <p className="skills-draft-note" role="status">有未保存的修改。保存或放弃修改后可切换技能和调整启停状态。</p>}
          {changedOnServer && <p className="skills-draft-note">服务端版本已变化；你的草稿尚未覆盖，保存时将检查版本。</p>}
          {!selected && <p className="skills-draft-note">此技能已不在当前目录中，编辑内容仍保留。</p>}
          <label className="field"><span>技能内容</span><textarea aria-label={`技能 ${detail.id} 内容`} value={draft} onChange={(event) => { setDraft(event.target.value); setActionError(""); setNotice(""); }} disabled={Boolean(busy)} spellCheck={false} rows={18} /></label>
          <div className="button-row skill-editor-actions"><Button onClick={discard} disabled={!dirty || controlsBusy}>放弃修改并重新读取</Button><Button className="primary" onClick={() => void save()} disabled={!dirty || controlsBusy}>{busy === "save" ? "正在保存…" : "保存技能"}</Button></div>
        </>}
      </div>
    </div>}
  </>;
}
