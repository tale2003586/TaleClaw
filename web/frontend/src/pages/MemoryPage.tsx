import { useEffect, useRef, useState } from "react";
import { RefreshCw } from "lucide-react";
import type { MemoryAction, MemoryActionResponse, MemoryItem } from "../api/types";
import { ApiError, postJson } from "../api/client";
import { useSessionsContext } from "../app/contexts";
import { useLegacyMemory, useMemory } from "../hooks/useMemory";
import { Badge, Button, EmptyState, ErrorState, PageHeader, Skeleton } from "../components/ui";
import "../styles/memory.css";

const statusLabels: Record<string, string> = { active: "已生效", candidate: "待确认", superseded: "已替代", revoked: "已撤销", expired: "已过期", rejected: "已拒绝" };
const scopeLabels: Record<string, string> = { user: "个人", project: "项目", workspace: "工作区", application: "应用", task: "任务" };
const kindLabels: Record<string, string> = { preference: "偏好", fact: "事实", decision: "决策", procedure: "流程", constraint: "约束", relationship: "关系" };
const sourceLabels: Record<string, string> = { explicit_user: "用户明确提供", inferred: "推断", coding_conclusion: "编码结论", legacy_import: "旧记忆导入" };
const label = (labels: Record<string, string>, value: string) => labels[value] || value;
const date = (value: string | null) => {
  if (!value) return "—";
  const parsed = new Date(value);
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
};

function MemoryDetail({ item, sessionId, onApplied }: { item: MemoryItem; sessionId: string; onApplied: (message: string) => void }) {
  const [pending, setPending] = useState<MemoryAction | null>(null);
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [conflict, setConflict] = useState(false);
  const mounted = useRef(true);
  useEffect(() => { mounted.current = true; return () => { mounted.current = false; }; }, []);
  const expired = item.valid_until !== null && new Date(item.valid_until).getTime() <= Date.now();
  const canAct = item.manageable && !expired && !conflict;
  const actionLabels: Record<MemoryAction, string> = { confirm: "确认记忆", reject: "拒绝候选", revoke: "撤销记忆" };
  const apply = async () => {
    if (!pending || busy || !canAct) return;
    setBusy(true); setError("");
    try {
      const result = await postJson<MemoryActionResponse>("/api/memory/action", {
        id: item.id, expected_version: item.version, action: pending, reason,
        ...(sessionId ? { session_id: sessionId } : {}),
      });
      if (mounted.current) onApplied(result.message);
    } catch (value) {
      if (!mounted.current) return;
      const changed = value instanceof ApiError && value.status === 409;
      setConflict(changed);
      setError(changed ? "这条记忆的版本或状态已变化。已保留当前内容，请刷新记忆后重新操作。" : value instanceof Error ? value.message : String(value));
    } finally { if (mounted.current) setBusy(false); }
  };
  return <article className="semantic-memory-detail" aria-label="记忆详情">
    <header><div className="semantic-memory-badges"><Badge tone={item.retrievable ? "success" : "neutral"}>{label(statusLabels, item.status)}</Badge><Badge>{label(kindLabels, item.kind)}</Badge><Badge>{label(scopeLabels, item.owner_scope)}记忆</Badge></div><span>版本 {item.version}</span></header>
    <p className="semantic-memory-copy">{item.content}</p>
    <p className="semantic-memory-retrieval">{item.retrievable ? "当前有效，可供语义检索使用。" : "当前不参与语义检索。"}</p>
    <section className="semantic-memory-actions" aria-label="管理记忆">
      {!item.manageable ? <p>此共享作用域仅供查看；可管理自己的个人记忆和已验证的当前编码任务记忆。</p> : expired ? <p>有效期已结束，这条记忆仅供查看。</p> : <>
        <div>{item.status === "candidate" && <><Button disabled={!canAct || busy} onClick={() => { setPending("confirm"); setError(""); }}>确认记忆</Button><Button disabled={!canAct || busy} onClick={() => { setPending("reject"); setError(""); }}>拒绝候选</Button></>}{item.status === "active" && <Button disabled={!canAct || busy} onClick={() => { setPending("revoke"); setError(""); }}>撤销记忆</Button>}</div>
        {!["candidate", "active"].includes(item.status) && <p>此生命周期状态仅供查看。</p>}
        {pending && <div className="semantic-memory-confirmation" role="group" aria-label="确认记忆操作"><strong>{actionLabels[pending]}</strong><p>{pending === "confirm" ? "确认后，这条候选记忆将按有效期参与检索。" : pending === "reject" ? "拒绝后，这条候选记忆不再生效，内容和来源会保留。" : "撤销后，这条记忆将停止生效，内容和来源会保留。"}</p><label>操作说明（可选）<textarea aria-label="记忆操作说明" maxLength={1000} disabled={busy} value={reason} onChange={(event) => setReason(event.target.value)} /></label><div><Button className="primary" disabled={busy || !canAct} onClick={() => void apply()}>{busy ? "正在提交…" : `确定${actionLabels[pending]}`}</Button><Button disabled={busy} onClick={() => setPending(null)}>取消</Button></div></div>}
      </>}
      {error && <p role="alert">{error}</p>}
    </section>
    <dl className="semantic-memory-facts">
      <div><dt>作用域</dt><dd>{label(scopeLabels, item.owner_scope)} · {item.owner_id}</dd></div>
      <div><dt>置信度 / 重要性</dt><dd>{Math.round(item.confidence * 100)}% / {Math.round(item.salience * 100)}%</dd></div>
      <div><dt>更新于</dt><dd>{date(item.updated_at)}</dd></div>
      <div><dt>最近确认</dt><dd>{date(item.last_confirmed_at)}</dd></div>
      <div><dt>有效期</dt><dd>{date(item.valid_from)} → {item.valid_until ? date(item.valid_until) : "长期"}</dd></div>
      <div><dt>记忆 ID</dt><dd>{item.id}</dd></div>
      {item.supersedes_id && <div><dt>替代的记忆</dt><dd>{item.supersedes_id}</dd></div>}
    </dl>
    <section className="semantic-memory-evidence" aria-label="记忆来源"><h3>来源与证据</h3>{item.evidence.length === 0 ? <p>暂无来源记录。</p> : item.evidence.map((evidence) => <div key={evidence.id}>
      <header><strong>{label(sourceLabels, evidence.source_type)}</strong><time>{date(evidence.created_at)}</time></header>
      {evidence.source_ref && <p>{evidence.source_ref}</p>}{evidence.excerpt && <blockquote>{evidence.excerpt}</blockquote>}
      {evidence.session_id && <p>来源会话：{evidence.session_id}</p>}{evidence.task_id && <p>来源任务：{evidence.task_id}</p>}
      {evidence.project_id && <p>来源项目：{evidence.project_id}</p>}{evidence.workspace_id && <p>来源工作区：{evidence.workspace_id}</p>}
    </div>)}</section>
  </article>;
}

function LegacyMemory() {
  const legacy = useLegacyMemory();
  return <details className="legacy-memory" onToggle={(event) => {
    if (event.currentTarget.open && legacy.status === "idle") void legacy.reload().catch(() => undefined);
  }}><summary>旧版 Markdown 记忆（兼容查看）</summary>
    <p>这些文件是迁移前的兼容资料，当前长期记忆以结构化存储为准。会话状态与近期上下文由各会话维护。</p>
    {legacy.status === "loading" && <Skeleton lines={3} />}
    {legacy.status === "error" && <ErrorState message={legacy.error} retry={() => void legacy.reload().catch(() => undefined)} />}
    {legacy.status === "success" && !legacy.data?.length && <p>当前账号没有旧版记忆文件内容。</p>}
    {legacy.status === "success" && legacy.data?.map((file) => <details key={file.name}><summary>{file.name}</summary><pre>{file.content}</pre></details>)}
  </details>;
}

export default function MemoryPage() {
  const sessions = useSessionsContext();
  const [context, setContext] = useState("user");
  const [query, setQuery] = useState("");
  const [status, setStatus] = useState("");
  const [kind, setKind] = useState("");
  const [actionMessage, setActionMessage] = useState("");
  const sessionId = context === "session" ? sessions.activeId : "";
  const memory = useMemory(sessionId, query, status, kind);
  const data = memory.data;
  const items = data?.items || [];
  const current = items.find((item) => item.id === memory.active) || items[0];
  return <div className="page">
    <PageHeader eyebrow="Semantic Memory" title="记忆" description="搜索、确认和管理长期记忆，查看生命周期和来源。" action={<Button className="primary" disabled={memory.status === "loading"} onClick={() => { setActionMessage(""); void memory.reload().catch(() => undefined); }}><RefreshCw aria-hidden="true" size={14} />刷新记忆</Button>} />
    <div className="semantic-memory-body">
      <div className="semantic-memory-controls"><label>查看范围<select value={context} onChange={(event) => { setContext(event.target.value); setActionMessage(""); }}><option value="user">个人长期记忆</option><option value="session">当前会话可访问的记忆</option></select></label><p>{context === "session" ? `当前会话：${sessions.active?.title || sessions.activeId}。包含该会话及绑定编码任务可访问的个人、应用、项目、工作区和任务作用域。` : "仅显示当前账号的个人记忆。选择当前会话可查看其关联作用域。"}</p></div>
      <div className="semantic-memory-filters"><label>搜索全部授权记忆<input type="search" aria-label="搜索全部记忆" placeholder="搜索内容、作用域或 ID" maxLength={500} value={query} onChange={(event) => setQuery(event.target.value)} /></label><label>生命周期<select aria-label="记忆生命周期" value={status} onChange={(event) => setStatus(event.target.value)}><option value="">全部状态</option>{Object.entries(statusLabels).map(([value, text]) => <option value={value} key={value}>{text}</option>)}</select></label><label>记忆类型<select aria-label="记忆类型" value={kind} onChange={(event) => setKind(event.target.value)}><option value="">全部类型</option>{Object.entries(kindLabels).map(([value, text]) => <option value={value} key={value}>{text}</option>)}</select></label>{data?.available && <span>本页 {data.items.length} 条 / 匹配 {data.filtered_total} 条 / 总计 {data.counts.total} 条</span>}</div>
      {actionMessage && <p className="semantic-memory-note" role="status">{actionMessage}</p>}
      {memory.status === "loading" && <Skeleton lines={7} />}
      {memory.status === "error" && <ErrorState message={memory.error} retry={() => void memory.reload().catch(() => undefined)} />}
      {memory.status === "success" && data && !data.available && <div className="semantic-memory-unavailable" role="alert"><strong>长期记忆存储不可用</strong><p>{data.message || "暂时无法读取长期记忆，请稍后刷新。"}</p><p>存储不可用时无法判断是否已有记忆。</p></div>}
      {memory.status === "success" && data?.available && <>
        <div className="semantic-memory-summary"><div><span>作用域内总数</span><strong>{data.counts.total}</strong></div><div><span>当前可检索</span><strong>{data.counts.retrievable}</strong></div><div><span>待确认</span><strong>{data.counts.by_status.candidate || 0}</strong></div><div><span>存储</span><strong>PostgreSQL</strong></div></div>
        <div className="semantic-memory-runtime"><span>语义读取：{data.runtime.read_enabled ? "开启" : "关闭"}</span><span>记忆写入：{data.runtime.write_enabled ? "开启" : "关闭"}</span><span>上下文注入：{data.runtime.context_enabled ? "开启" : "关闭"}</span></div>
        {(!data.runtime.read_enabled || !data.runtime.context_enabled) && <p className="semantic-memory-note">已存储的记忆仍可在此查看。语义读取或上下文注入关闭时，相应运行时记忆路径不会使用这些记录。</p>}
        {data.context_message && <p className="semantic-memory-note" role="status">{data.context_message}</p>}
        <details className="semantic-memory-scopes"><summary>当前授权作用域（{data.scopes.length}）</summary><ul>{data.scopes.map((scope) => <li key={`${scope.scope}:${scope.id}`}>{label(scopeLabels, scope.scope)} · {scope.id}</li>)}</ul></details>
        {data.counts.total === 0 ? <EmptyState title="暂无长期记忆" message="当前授权作用域还没有结构化记忆。对话中明确要求记住的信息及经过确认的结论会按运行时配置保存。" /> : <>
          {!items.length ? <EmptyState title="没有匹配的记忆" message="搜索已覆盖全部授权作用域。请调整搜索内容、生命周期或记忆类型。" /> : <div className="semantic-memory-layout"><nav className="semantic-memory-index" aria-label="长期记忆列表">{items.map((item) => <button key={item.id} type="button" aria-pressed={current?.id === item.id} className={current?.id === item.id ? "active" : ""} onClick={() => memory.setActive(item.id)}><div><Badge>{label(statusLabels, item.status)}</Badge><small>{label(scopeLabels, item.owner_scope)} · {label(kindLabels, item.kind)}</small></div><p>{item.content}</p><small>{item.source_types.map((source) => label(sourceLabels, source)).join(" · ") || "无来源记录"}</small></button>)}</nav>{current && <MemoryDetail key={`${sessionId}:${current.id}:${current.version}`} item={current} sessionId={sessionId} onApplied={(message) => { setActionMessage(message); void memory.reload().catch(() => undefined); }} />}</div>}
          {(memory.offset > 0 || data.has_more) && <div className="semantic-memory-pagination"><Button disabled={memory.offset === 0} onClick={() => memory.goToPage(memory.offset - memory.pageSize)}>上一页</Button><span>第 {Math.floor(memory.offset / memory.pageSize) + 1} 页</span><Button disabled={!data.has_more || data.next_offset === null} onClick={() => memory.goToPage(data.next_offset!)}>下一页</Button></div>}
        </>}
      </>}
      <LegacyMemory />
    </div>
  </div>;
}
