import { useCallback, useEffect, useState } from "react";
import { getJson, postJson } from "../api/client";
import type { CodingDelivery, WorkspaceBinding } from "../api/types";
import { Button } from "./ui";

const STATUS: Record<string, string> = { preparing: "正在准备", prepared: "待验证", conflict: "合并冲突", verifying: "验证中", verified: "验证通过", failed: "失败", interrupted: "操作已中断", stale: "目标已变化", published: "已交付候选分支" };

export function CodingWorkspacePanel({ workspace, running, canManage }: { workspace: WorkspaceBinding; running: boolean; canManage: boolean }) {
  const [open, setOpen] = useState(false);
  const [binding, setBinding] = useState(workspace);
  const [deliveries, setDeliveries] = useState<CodingDelivery[]>([]);
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [target, setTarget] = useState("");
  const [commands, setCommands] = useState<Record<string, string>>({});
  const worktreeId = workspace.worktree_id;
  const load = useCallback(async (signal?: AbortSignal) => {
    if (!worktreeId || !canManage) return;
    const [catalog, history] = await Promise.all([
      getJson<{ worktrees: WorkspaceBinding[] }>("/api/coding/worktrees", { signal }),
      getJson<{ deliveries: CodingDelivery[] }>(`/api/coding/deliveries?worktree_id=${encodeURIComponent(worktreeId)}`, { signal }),
    ]);
    if (signal?.aborted) return;
    const current = catalog.worktrees?.find((item) => item.worktree_id === worktreeId);
    if (current) setBinding(current);
    setDeliveries(history.deliveries || []);
  }, [canManage, worktreeId]);
  useEffect(() => {
    if (!open) return;
    const controller = new AbortController();
    void load(controller.signal).catch((reason) => { if (!controller.signal.aborted) setError(String(reason instanceof Error ? reason.message : reason)); });
    return () => controller.abort();
  }, [load, open, running]);
  const action = async (key: string, operation: () => Promise<void>) => {
    setBusy(key); setError(""); setNotice("");
    try { await operation(); }
    catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
    finally { setBusy(""); }
  };
  const checkpoint = () => action("checkpoint", async () => {
    const result = await postJson<{ checkpoint: { commit: string } }>(`/api/coding/worktrees/${encodeURIComponent(worktreeId!)}/checkpoint`, {});
    setNotice(`任务修改已保存到提交 ${result.checkpoint.commit}。`);
    try { await load(); } catch { setNotice(`任务修改已保存到提交 ${result.checkpoint.commit}；列表刷新失败，可手动刷新。`); }
  });
  const prepare = () => action("prepare", async () => {
    const result = await postJson<{ delivery: CodingDelivery }>("/api/coding/deliveries", { worktree_id: worktreeId, ...(target.trim() ? { target_ref: target.trim() } : {}) });
    setDeliveries((current) => [result.delivery, ...current.filter((item) => item.id !== result.delivery.id)]);
    setNotice(result.delivery.status === "conflict" ? "候选存在冲突。请先在任务工作区合入目标分支并解决冲突，再保存任务提交、重新创建候选。" : "已准备独立集成候选，请运行验证。");
  });
  const verify = (delivery: CodingDelivery) => action(delivery.id, async () => {
    const result = await postJson<{ delivery: CodingDelivery }>(`/api/coding/deliveries/${encodeURIComponent(delivery.id)}/verify`, { command: commands[delivery.id]?.trim(), timeout_seconds: 300 });
    setDeliveries((current) => current.map((item) => item.id === delivery.id ? result.delivery : item));
    setNotice(result.delivery.status === "verified" ? "该候选验证通过；主分支尚未更新。" : "验证未通过，请查看候选状态和输出。");
  });
  const publish = (delivery: CodingDelivery) => action(delivery.id, async () => {
    const result = await postJson<{ delivery: CodingDelivery }>(`/api/coding/deliveries/${encodeURIComponent(delivery.id)}/publish`, {});
    setDeliveries((current) => current.map((item) => item.id === delivery.id ? result.delivery : item));
    setNotice("已生成独立交付分支；用户主分支保持原状。");
  });
  const copyRef = async (ref: string) => {
    try {
      if (!navigator.clipboard) throw new Error("当前浏览器不支持复制，请手动复制显示的分支名。");
      await navigator.clipboard.writeText(ref);
      setNotice("交付分支名已复制。");
    } catch (reason) { setError(reason instanceof Error ? reason.message : String(reason)); }
  };
  const writeBlocked = running || binding.status === "active" || binding.status === "quarantined" || binding.status === "removed" || Boolean(busy);
  return <details className="chat-workspace-binding" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>任务工作区 · {workspace.branch || workspace.workspace_root}</summary>
    <dl><dt>执行目录</dt><dd>{binding.workspace_root}</dd><dt>分支</dt><dd>{binding.branch || "原目录"}</dd><dt>基点</dt><dd>{binding.base_commit || "未报告"}</dd><dt>状态</dt><dd>{binding.status || "未报告"}{binding.dirty ? " · 有未保存修改" : ""}</dd></dl>
    {binding.dirty_source_excluded && <p>本任务未包含源目录的未提交修改和未跟踪文件。</p>}
    {canManage && worktreeId && <div className="coding-delivery-controls">
      <p>停止任务后先保存任务提交，再创建独立集成候选。验证仅针对显示的候选提交；主分支由后续审核流程接纳。</p>
      <div className="button-row"><Button disabled={writeBlocked} onClick={() => void checkpoint()}>{busy === "checkpoint" ? "保存中…" : "保存任务提交"}</Button><Button disabled={Boolean(busy)} onClick={() => void action("refresh", () => load())}>刷新工作区</Button></div>
      <label>目标分支<input aria-label="集成目标分支" value={target} disabled={Boolean(busy)} onChange={(event) => setTarget(event.target.value)} placeholder="留空使用源仓库当前分支，或 refs/heads/main" /></label>
      <Button disabled={writeBlocked} onClick={() => void prepare()}>{busy === "prepare" ? "准备候选中…" : "创建集成候选"}</Button>
      {running && <p>当前任务仍在写入，请完成或停止后保存。</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      {notice && <p role="status">{notice}</p>}
      {deliveries.map((delivery) => <section className="coding-delivery" key={delivery.id} aria-label={`集成候选 ${delivery.id}`}>
        <header><strong>{STATUS[delivery.status] || delivery.status}</strong><code>{delivery.id}</code></header>
        <dl><dt>候选提交</dt><dd>{delivery.candidate_sha || "尚未生成"}</dd><dt>目标基点</dt><dd>{delivery.target_ref} · {delivery.target_sha}</dd><dt>交付分支</dt><dd>{delivery.published_ref || delivery.ref || delivery.branch || "尚未生成"}</dd><dt>集成目录</dt><dd>{delivery.workspace_root}</dd></dl>
        {Boolean(delivery.conflicts?.length) && <p className="inline-error">冲突文件：{delivery.conflicts!.join("、")}</p>}
        {delivery.error && <p className="inline-error">{delivery.error}</p>}
        <label>验证命令<input aria-label={`验证命令 ${delivery.id}`} value={commands[delivery.id] || ""} onChange={(event) => setCommands((current) => ({ ...current, [delivery.id]: event.target.value }))} disabled={Boolean(busy)} placeholder="输入该项目的测试命令" /></label>
        <Button disabled={Boolean(busy) || !commands[delivery.id]?.trim() || !delivery.candidate_sha || !["prepared", "verified", "failed"].includes(delivery.status)} onClick={() => void verify(delivery)}>{busy === delivery.id || delivery.status === "verifying" ? "验证中…" : "运行验证"}</Button>
        {delivery.status === "verified" && <Button disabled={Boolean(busy)} onClick={() => void publish(delivery)}>生成交付分支</Button>}
        {delivery.published_ref && <Button onClick={() => void copyRef(delivery.published_ref!)}>复制交付分支</Button>}
        {delivery.verification && <details className="delivery-verification"><summary>验证记录 · {delivery.verification.status || "未知"} · 退出码 {delivery.verification.exit_code ?? "未知"}</summary><pre>{delivery.verification.command}\n{delivery.verification.output || ""}</pre></details>}
      </section>)}
    </div>}
  </details>;
}
