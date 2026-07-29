import { useState, type ReactNode } from "react";
import { Bot, Brain, GitBranch, RefreshCw, Route, SlidersHorizontal } from "lucide-react";
import type { LucideIcon } from "lucide-react";
import { Button, PageHeader } from "../components/ui";

export default function SettingsPage() {
  const [notice, setNotice] = useState("预览模式 · 不会保存");
  return <div className="page"><PageHeader eyebrow="Control Plane" title="Agent 设置" description="模型、上下文与执行策略的界面预览。" action={<span className="preview-badge">Preview</span>} /><div className="page-body settings-body"><div className="settings-notice" role="status"><strong>尚未接入配置保存</strong><span>本页不会读取或修改 .env，所有控件仅用于确认后续配置体验。</span></div><form className="settings-grid" autoComplete="off" onSubmit={(event) => { event.preventDefault(); setNotice("预览模式：未发送任何配置请求。"); }}>
    <SettingsCard icon={Bot} title="Provider 与 API"><Field label="Provider Profile"><select defaultValue="openai_relay"><option>openai_relay</option><option>deepseek</option><option>gemini</option><option>mimo</option></select></Field><Field label="API Endpoint"><input defaultValue="https://api.example.com/v1" autoComplete="url" /></Field><Field label="API Key"><input type="password" placeholder="未读取真实密钥" autoComplete="new-password" /></Field></SettingsCard>
    <SettingsCard icon={Route} title="模型路由"><Field label="Chat Route"><input defaultValue="openai_relay, deepseek" /></Field><Field label="Coding Route"><input defaultValue="openai_relay, deepseek" /></Field><Field label="Fallback Route"><input placeholder="可选后备链" /></Field></SettingsCard>
    <SettingsCard icon={SlidersHorizontal} title="上下文策略"><Toggle title="Section Budget" note="按区段限制上下文预算" checked /><Field label="Conversation Strategy"><select defaultValue="summary_middle"><option>summary_middle</option><option>head_tail</option><option>tail</option></select></Field><Field label="Context Budget"><input type="number" defaultValue="24000" /></Field></SettingsCard>
    <SettingsCard icon={Brain} title="记忆与 RAG"><Toggle title="Working Memory" note="Checkpoint 与 Resume" checked /><Toggle title="Semantic Memory" note="长期语义记忆读取" /><Toggle title="Security RAG" note="安全知识检索与注入" /></SettingsCard>
    <SettingsCard icon={RefreshCw} title="反思策略"><Toggle title="Reflection" note="长执行中的阶段性反思" /><Field label="Minimum Steps"><input type="number" defaultValue="10" /></Field><Field label="Interval"><input type="number" defaultValue="5" /></Field></SettingsCard>
    <SettingsCard icon={GitBranch} title="Subagent"><Field label="Max Reasoning Steps"><input type="number" defaultValue="12" /></Field><Field label="Max Fanouts"><input type="number" defaultValue="4" /></Field><Field label="Max Scope Files"><input type="number" defaultValue="5" /></Field></SettingsCard>
    <div className="settings-actions"><span>{notice}</span><Button className="primary" type="submit">保存设置（未接入）</Button></div>
  </form></div></div>;
}

function SettingsCard({ icon: Icon, title, children }: { icon: LucideIcon; title: string; children: ReactNode }) { return <section className="settings-card"><header><span><Icon aria-hidden="true" /></span><h3>{title}</h3></header>{children}</section>; }
function Field({ label, children }: { label: string; children: ReactNode }) { return <label className="field"><span>{label}</span>{children}</label>; }
function Toggle({ title, note, checked = false }: { title: string; note: string; checked?: boolean }) { return <label className="toggle"><span><strong>{title}</strong><small>{note}</small></span><input type="checkbox" role="switch" defaultChecked={checked} /></label>; }
