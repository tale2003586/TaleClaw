import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, CircleStop, Paperclip, Send, Sparkles, X } from "lucide-react";
import { ApiError, postJson, uploadFormData } from "../api/client";
import type { CodingTaskRequest, FileEntry, FilesResponse, MessageDto, ModelOption, SessionResponse } from "../api/types";
import { useAppContext, useChatRunsContext, useSessionsContext } from "../app/contexts";
import { Button, EmptyState } from "../components/ui";
import { SafeMarkdown } from "../components/chat/SafeMarkdown";
import { ActivityTimeline } from "../components/chat/ActivityTimeline";
import { chatRunStatusLabel, isActiveChatRun, type ActivitySnapshot } from "../hooks/useChatRuns";
import { CodingWorkspacePanel } from "../components/CodingWorkspacePanel";
import { TechnicalOutput } from "../components/chat/TechnicalOutput";

const MODES = [["/hybrid", "Hybrid", "hybrid"], ["/chat", "Chat Only", "bot"], ["/coding", "Coding", "coding"]] as const;
const USER_MESSAGE_PREVIEW_CHARS = 480;
const USER_MESSAGE_PREVIEW_LINES = 8;
interface ComposerState {
  draft: string;
  attachments: File[];
  error: string;
  busy: boolean;
  optimistic?: MessageDto;
  optimisticRunId?: string;
  optimisticAfterSeq?: number;
  originalSubmission?: { draft: string; attachments: File[] };
  modeBusy: string;
  modeNotice: string;
  taskMode?: CodingTaskRequest["mode"];
  taskId?: string;
  workspaceMode?: CodingTaskRequest["workspace_mode"];
}
const EMPTY_COMPOSER: ComposerState = { draft: "", attachments: [], error: "", busy: false, modeBusy: "", modeNotice: "" };

export default function ChatPage() {
  const sessions = useSessionsContext();
  const runs = useChatRunsContext();
  const { user, codingWorkspace, health } = useAppContext();
  const [composers, setComposers] = useState<Record<string, ComposerState>>({});
  const submitting = useRef(new Set<string>());
  const [thinkingEnabled, setThinkingEnabled] = useState(false);
  const [modelProfile, setModelProfile] = useState(() => {
    try { return window.localStorage.getItem("taleclaw.model_profile") || ""; } catch { return ""; }
  });
  const bottom = useRef<HTMLDivElement>(null);
  const messageList = useRef<HTMLDivElement>(null);
  const messageScroller = useRef<HTMLDivElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
  const attachmentInput = useRef<HTMLInputElement>(null);
  const shouldStickToBottom = useRef(true);
  const { active, activeId, busy, loadSession, loadOlderMessages, loadingHistory, newSession, reload, mergeSession } = sessions;
  const composer = composers[activeId] || EMPTY_COMPOSER;
  const editComposer = useCallback((id: string, update: Partial<ComposerState> | ((current: ComposerState) => ComposerState)) => {
    setComposers((current) => ({ ...current, [id]: typeof update === "function" ? update(current[id] || EMPTY_COMPOSER) : { ...(current[id] || EMPTY_COMPOSER), ...update } }));
  }, []);
  const runView = Object.values(runs.views).filter((view) => view.run.session_id === activeId).sort((a, b) => b.run.created_at.localeCompare(a.run.created_at))[0];
  const run = runView?.run;
  const running = isActiveChatRun(run);
  const awaitingSession = Boolean(runView && !runView.hydrated && !running);
  const pending = runs.pending[activeId];
  const sending = composer.busy || Boolean(pending?.sending);
  const raw = Boolean(active?.channel && active.channel !== "web");
  const mode = String(active?.active_agent || "hybrid");
  const taskId = composer.taskId ?? run?.task_id ?? String(active?.metadata?.active_coding_task_id || "");
  const taskMode = composer.taskMode ?? (taskId ? "continue" : "new");
  const workspaceMode = composer.workspaceMode ?? "worktree";
  const models = useMemo(() => health.models || [], [health.models]);
  const selectedModel = models.find((item) => item.profile === modelProfile);
  const automaticModel = models.find((item) => item.profile === health.default_model_profile);
  const thinkingAvailable = selectedModel ? selectedModel.supports_thinking : Boolean(automaticModel?.supports_thinking);
  const optimisticView = composer.optimisticRunId ? runs.views[composer.optimisticRunId] : undefined;
  const savedOptimistic = (active?.messages || []).some((message) => message.role === "user" && message.content === composer.optimistic?.content && (message.seq ?? -1) > (composer.optimisticAfterSeq ?? -1));
  const optimistic = composer.optimistic && !optimisticView?.hydrated && !savedOptimistic ? [composer.optimistic] : [];
  const messages = [...(active?.messages || []).filter((message) => message.role !== "system"), ...optimistic];
  let lastAssistantIndex = -1;
  messages.forEach((message, index) => { if (message.role === "assistant") lastAssistantIndex = index; });
  const completedActivity: ActivitySnapshot | null = runView?.hydrated ? {
    sessionId: activeId, items: runView.activity, startedAt: runView.startedAt, finishedAt: runView.finishedAt,
    status: run?.status === "failed" || run?.status === "interrupted" ? "error" : "complete",
  } : null;

  useEffect(() => { if (!active && !busy) void loadSession("default").catch(() => newSession()); }, [active, busy, loadSession, newSession]);
  useEffect(() => { shouldStickToBottom.current = true; }, [activeId]);
  useEffect(() => { if (modelProfile && !models.some((item) => item.profile === modelProfile)) setModelProfile(""); }, [modelProfile, models]);
  useEffect(() => {
    try {
      if (modelProfile) window.localStorage.setItem("taleclaw.model_profile", modelProfile);
      else window.localStorage.removeItem("taleclaw.model_profile");
    } catch { /* storage can be unavailable */ }
  }, [modelProfile]);
  useEffect(() => {
    if (!shouldStickToBottom.current) return;
    const frame = window.requestAnimationFrame(() => bottom.current?.scrollIntoView?.({ behavior: running ? "auto" : "smooth", block: "end" }));
    return () => window.cancelAnimationFrame(frame);
  }, [activeId, messages.length, running, runView?.text]);

  const handleMessageScroll = useCallback(() => {
    const node = messageScroller.current;
    if (!node) return;
    shouldStickToBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 140;
    if (node.scrollTop > 120 || loadingHistory || !active?.message_page?.has_more) return;
    const previousHeight = node.scrollHeight;
    void loadOlderMessages().then((added) => {
      if (!added) return;
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => { node.scrollTop += node.scrollHeight - previousHeight; }));
    });
  }, [active?.message_page?.has_more, loadOlderMessages, loadingHistory]);

  const send = async () => {
    const sessionId = activeId;
    const selectedFiles = [...composer.attachments];
    const message = composer.draft.trim() || (selectedFiles.length ? "请分析附件内容。" : "");
    if (!message || running || awaitingSession || pending || sending || busy || raw || composer.modeBusy || submitting.current.has(sessionId)) return;
    if (mode === "coding" && taskMode !== "new" && !taskId.trim()) {
      editComposer(sessionId, { error: "请填写要继续或分叉的任务 ID。" }); return;
    }
    submitting.current.add(sessionId);
    shouldStickToBottom.current = true;
    const display = selectedFiles.length ? `${message}\n\n附件：${selectedFiles.map((file) => file.name).join("、")}` : message;
    // Capture every binding before uploading: changing tabs/settings must not
    // retarget this intent to a different session, workspace, or task.
    const codingTask: CodingTaskRequest | undefined = mode === "coding" ? {
      mode: taskMode,
      ...(taskMode !== "continue" ? { workspace_mode: workspaceMode } : {}),
      ...(taskMode === "new" ? (workspaceMode === "worktree" ? { base_ref: "HEAD" } : {}) : { task_id: taskId.trim() }),
    } : undefined;
    const request = {
      session_id: sessionId, message,
      ...(user.role === "admin" && codingWorkspace && (codingTask?.mode === "new" || (!codingTask && !taskId)) ? { workspace_root: codingWorkspace } : {}),
      thinking_enabled: thinkingEnabled, model_profile: modelProfile,
      ...(codingTask ? { coding_task: codingTask } : {}),
    };
    editComposer(sessionId, { draft: "", attachments: [], error: "", busy: true, originalSubmission: { draft: composer.draft, attachments: selectedFiles }, optimistic: { role: "user", content: display }, optimisticRunId: undefined, optimisticAfterSeq: Math.max(-1, ...(active?.messages || []).map((message) => message.seq ?? -1)) });
    if (composerInput.current) composerInput.current.style.height = "auto";
    let submitted = false;
    try {
      let paths: string[] = [];
      if (selectedFiles.length) {
        const form = new FormData();
        form.append("path", `chat-attachments/${uploadId()}`);
        selectedFiles.forEach((file) => form.append("file", file));
        const response = await uploadFormData<{ saved: FileEntry[]; files: FilesResponse }>("/api/files/upload", form);
        paths = (response.saved || []).map((entry) => entry.path);
        if (paths.length !== selectedFiles.length) throw new Error("部分附件上传失败");
      }
      submitted = true;
      const accepted = await runs.submit({ ...request, attachments: paths });
      editComposer(sessionId, { optimisticRunId: accepted.run_id, originalSubmission: undefined, taskMode: undefined, taskId: undefined, workspaceMode: undefined });
      void reload().catch(() => undefined);
    } catch (reason) {
      const rejected = !submitted || (reason instanceof ApiError && reason.status >= 400 && reason.status < 500 && reason.status !== 408);
      editComposer(sessionId, (current) => ({
        ...current, error: reason instanceof Error ? reason.message : String(reason),
        ...(rejected ? { draft: current.draft || composer.draft, attachments: current.attachments.length ? current.attachments : selectedFiles, optimistic: undefined, originalSubmission: undefined } : {}),
      }));
    } finally {
      submitting.current.delete(sessionId);
      editComposer(sessionId, { busy: false });
    }
  };
  const retrySubmission = async () => {
    const sessionId = activeId;
    try {
      const accepted = await runs.retrySubmission(sessionId);
      if (accepted) editComposer(sessionId, { optimisticRunId: accepted.run_id, error: "", originalSubmission: undefined, taskMode: undefined, taskId: undefined, workspaceMode: undefined });
    } catch (reason) {
      const rejected = reason instanceof ApiError && reason.status >= 400 && reason.status < 500 && reason.status !== 408;
      editComposer(sessionId, (current) => ({
        ...current, error: reason instanceof Error ? reason.message : String(reason),
        ...(rejected ? { draft: current.originalSubmission?.draft || current.draft, attachments: current.originalSubmission?.attachments || current.attachments, optimistic: undefined, originalSubmission: undefined } : {}),
      }));
    }
  };
  const selectAttachments = (files: FileList | null) => {
    const incoming = Array.from(files || []);
    editComposer(activeId, (current) => ({ ...current, attachments: [...current.attachments, ...incoming].slice(0, 5), error: incoming.length + current.attachments.length > 5 ? "每条消息最多添加 5 个附件。" : "" }));
    if (attachmentInput.current) attachmentInput.current.value = "";
  };
  const changeMode = async (command: string, label: string) => {
    const sessionId = activeId;
    if (raw || running || awaitingSession || pending || sending || busy) return;
    editComposer(sessionId, { modeBusy: command, modeNotice: "" });
    try {
      const response = await postJson<SessionResponse>("/api/chat", { session_id: sessionId, message: command });
      if (response.session) mergeSession(sessionId, response.session);
      editComposer(sessionId, { modeNotice: `已切换到 ${label}` });
      void reload().catch(() => undefined);
    } catch (reason) { editComposer(sessionId, { modeNotice: `切换失败：${reason instanceof Error ? reason.message : String(reason)}` }); }
    finally { editComposer(sessionId, { modeBusy: "" }); }
  };
  const setAllOutputs = (open: boolean) => messageList.current?.querySelectorAll<HTMLDetailsElement>("details[data-technical-output]").forEach((item) => { item.open = open; });
  const disabled = raw || sending || Boolean(pending) || busy;
  const liveOutput = runView && !runView.hydrated;
  const runError = run?.error || runView?.connectionError;

  return <div className="page chat-page">
    <header className="chat-header">
      <div><span className="eyebrow"><Sparkles aria-hidden="true" size={12} />当前会话</span><h2>{active?.title || activeId}</h2><span className={`mode-notice ${composer.modeNotice.startsWith("切换失败") ? "error" : ""}`} aria-live="polite">{composer.modeNotice}</span></div>
      <div className="chat-controls"><div className="output-controls" aria-label="技术输出显示"><Button onClick={() => setAllOutputs(true)}><ChevronDown aria-hidden="true" size={14} />展开输出</Button><Button onClick={() => setAllOutputs(false)}><ChevronUp aria-hidden="true" size={14} />收起输出</Button></div><div className="mode-switch">{MODES.map(([command, label, value]) => user.role !== "admin" && value === "coding" ? null : <Button key={command} disabled={Boolean(composer.modeBusy) || running || awaitingSession || disabled} className={mode === value ? "active" : ""} onClick={() => void changeMode(command, label)}>{composer.modeBusy === command ? "切换中…" : label}</Button>)}</div></div>
    </header>
    {run && <div className="chat-run-summary" aria-live="polite"><strong className={`chat-run-badge ${run.status}`}>{chatRunStatusLabel(run.status)}</strong><span>{run.task_id ? `任务 ${run.task_id}` : `运行 ${run.run_id}`}</span>{running && <small>可切换会话，后台继续运行</small>}</div>}
    {run?.workspace && <CodingWorkspacePanel key={run.workspace.worktree_id || run.run_id} workspace={run.workspace} running={Object.values(runs.views).some((view) => isActiveChatRun(view.run) && view.run.workspace?.worktree_id === run.workspace?.worktree_id)} canManage={user.role === "admin"} />}
    <div className="message-scroll" ref={messageScroller} onScroll={handleMessageScroll}>
      <div className="message-list" ref={messageList}>
        {active?.message_page?.has_more && <div className="message-history-loader"><Button disabled={loadingHistory} onClick={() => handleMessageScroll()}>{loadingHistory ? "正在加载更早消息…" : "向上滚动加载更早消息"}</Button></div>}
        {messages.length === 0 && !runView?.text && !runView?.thinking ? <EmptyState title="今天想先处理什么？" message="从一个具体任务开始，可随时切换会话查看后台执行。" /> : messages.map((message, index) => <article className={`message ${message.role}`} key={message.seq == null ? `${activeId}-${message.role}-pending-${index}` : `${activeId}-message-${message.seq}`}><span className="message-role">{roleLabel(message)}</span><div><MessageContent message={message} activity={index === lastAssistantIndex ? completedActivity : null} /></div></article>)}
        {liveOutput && <article className="message assistant streaming"><span className="message-role">Agent</span><div><ActivityTimeline items={runView.activity} status={running ? "running" : run?.status === "failed" ? "error" : "complete"} startedAt={runView.startedAt} finishedAt={runView.finishedAt} />{runView.progressText && <p className="attachment-progress">{runView.progressText}</p>}{runView.thinking && <ThinkingContent content={runView.thinking} open />}{runView.text && <SafeMarkdown>{runView.text}</SafeMarkdown>}</div></article>}
        {runError && <p className="inline-error" role="status">{runError}</p>}
        <div ref={bottom} />
      </div>
    </div>
    <div className="composer-dock">
      {mode === "coding" && user.role === "admin" && <div className="coding-task-options">
        <label>任务操作<select aria-label="Coding 任务操作" value={taskMode} disabled={running || disabled} onChange={(event) => editComposer(activeId, { taskMode: event.target.value as CodingTaskRequest["mode"] })}><option value="new">新建任务</option><option value="continue">继续任务</option><option value="branch">从任务分叉</option></select></label>
        {taskMode !== "new" && <label>来源任务<input aria-label="Coding 任务 ID" value={taskId} disabled={running || disabled} onChange={(event) => editComposer(activeId, { taskId: event.target.value })} placeholder="task ID" /></label>}
        <label>工作区<select aria-label="Coding 工作区方式" value={taskMode === "continue" ? "bound" : workspaceMode} disabled={running || disabled || taskMode === "continue"} onChange={(event) => editComposer(activeId, { workspaceMode: event.target.value as CodingTaskRequest["workspace_mode"] })}>{taskMode === "continue" && <option value="bound">沿用任务已绑定工作区</option>}<option value="worktree">独立 worktree</option><option value="in_place">原目录</option></select></label>
        <p>{taskMode === "continue" ? "继续使用任务已绑定的工作区，不随全局设置改变。" : workspaceMode === "worktree" ? `从 ${taskMode === "branch" ? "来源任务提交" : `源仓库 ${codingWorkspace || "默认工作区"} 的 HEAD 已提交版本`} 创建独立工作区；不包含未提交修改或未跟踪文件。` : `直接修改 ${codingWorkspace || "原目录"}；共享目录的任务不能同时写入。`}{taskMode === "branch" && " 请先保存来源任务的改动。"}</p>
      </div>}
      <div className="composer">
        {composer.attachments.length > 0 && <div className="attachment-list">{composer.attachments.map((file, index) => <span className="attachment-chip" key={`${file.name}-${file.size}-${index}`}><Paperclip aria-hidden="true" size={13} />{file.name}<button type="button" aria-label={`移除 ${file.name}`} onClick={() => editComposer(activeId, (current) => ({ ...current, attachments: current.attachments.filter((_, itemIndex) => itemIndex !== index) }))}><X aria-hidden="true" size={12} /></button></span>)}</div>}
        {composer.error && <p className="composer-error">{composer.error}</p>}
        {pending && !pending.sending && <div className="submission-retry"><p>提交结果尚未确认。重试会沿用同一请求，不重复创建任务。</p><Button onClick={() => void retrySubmission()}>重试确认提交</Button></div>}
        <textarea ref={composerInput} aria-label="消息" value={composer.draft} disabled={disabled} onChange={(event) => { editComposer(activeId, { draft: event.target.value }); event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 190)}px`; }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} placeholder={raw ? "只读会话" : running ? "可准备下一条消息，当前任务完成后发送…" : "输入问题，并可添加 PDF、Word、PPT 或图片附件……"} />
        <div><span>{sending ? "正在提交" : running ? "当前会话执行中" : raw ? "只读会话" : "附件将先经 MinerU 精准解析"}</span><div className="composer-actions">
          <label className="model-picker"><span>模型</span><select value={modelProfile} disabled={disabled} onChange={(event) => { setModelProfile(event.target.value); setThinkingEnabled(false); }} aria-label="选择模型"><option value="">自动路由</option>{models.map((option: ModelOption) => <option key={option.profile} value={option.profile}>{option.model} · {option.provider}</option>)}</select></label>
          <Button title={thinkingAvailable ? "启用模型深度思考" : "当前模型不支持思考模式"} disabled={!thinkingAvailable || disabled} className={thinkingEnabled ? "active" : ""} onClick={() => setThinkingEnabled((value) => !value)}>深度思考</Button>
          <Button aria-label="添加附件" disabled={disabled} onClick={() => attachmentInput.current?.click()}><Paperclip aria-hidden="true" size={14} />附件</Button>
          <input ref={attachmentInput} aria-label="附件文件" hidden multiple type="file" accept=".pdf,.doc,.docx,.ppt,.pptx,.png,.jpg,.jpeg,.jp2,.webp,.gif,.bmp" onChange={(event) => selectAttachments(event.target.files)} />
          {running && run ? <Button disabled={run.status === "cancelling"} onClick={() => void runs.cancel(run.run_id)}><CircleStop aria-hidden="true" size={14} />{run.status === "cancelling" ? "停止中…" : "停止"}</Button> : <Button aria-label="发送" className="primary" onClick={() => void send()} disabled={(!composer.draft.trim() && !composer.attachments.length) || disabled || awaitingSession || Boolean(composer.modeBusy)}><Send aria-hidden="true" size={14} /><span>发送</span></Button>}
        </div></div>
      </div>
    </div>
  </div>;
}

function uploadId() { return typeof crypto.randomUUID === "function" ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`; }

function MessageContent({ message, activity }: { message: MessageDto; activity?: ActivitySnapshot | null }) {
  const content = String(message.content || "");
  if (message.role === "assistant") return <>{activity && <ActivityTimeline items={activity.items} status={activity.status} startedAt={activity.startedAt} finishedAt={activity.finishedAt} />}{message.reasoning_content && <ThinkingContent content={message.reasoning_content} />}{content && <SafeMarkdown>{content}</SafeMarkdown>}{Boolean(message.tool_calls?.length) && <TechnicalOutput label={`工具调用 · ${message.tool_calls!.length}`} value={message.tool_calls} />}</>;
  if (message.role === "user") return <UserMessageContent content={content} />;
  return <TechnicalOutput label={message.name ? `${roleLabel(message)} · ${message.name}` : roleLabel(message)} value={content} />;
}

function ThinkingContent({ content, open = false }: { content: string; open?: boolean }) {
  return <details className="thinking-content" open={open}><summary>思考过程</summary><pre>{content}</pre></details>;
}

function UserMessageContent({ content }: { content: string }) {
  const [expanded, setExpanded] = useState(false);
  const lineCount = content.split("\n").length;
  const collapsible = content.length > USER_MESSAGE_PREVIEW_CHARS || lineCount > USER_MESSAGE_PREVIEW_LINES;
  const preview = collapsible && !expanded ? `${collapsedUserMessagePreview(content)}…` : content;
  return <div className={`user-message-content ${collapsible && !expanded ? "collapsed" : ""}`}><pre>{preview}</pre>{collapsible && <button type="button" className="user-message-toggle" aria-expanded={expanded} onClick={() => setExpanded((value) => !value)}>{expanded ? "收起问题" : `展开完整问题 · ${content.length.toLocaleString()} 字符`}</button>}</div>;
}

function collapsedUserMessagePreview(content: string) {
  return content
    .slice(0, USER_MESSAGE_PREVIEW_CHARS)
    .split("\n")
    .slice(0, USER_MESSAGE_PREVIEW_LINES)
    .join("\n")
    .trimEnd();
}

function roleLabel(message: MessageDto) {
  if (message.role === "user") return "你";
  if (message.role === "assistant") return "Agent";
  if (message.role === "tool") return "工具输出";
  if (message.role === "function") return "函数输出";
  return message.role || "运行输出";
}
