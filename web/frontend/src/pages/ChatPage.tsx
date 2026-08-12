import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, CircleStop, Paperclip, Send, Sparkles, X } from "lucide-react";
import { postJson, uploadFormData } from "../api/client";
import type { FileEntry, FilesResponse, MessageDto, ModelOption, SessionDto, SessionResponse } from "../api/types";
import { useAppContext, useSessionsContext } from "../app/contexts";
import { Button, EmptyState } from "../components/ui";
import { SafeMarkdown } from "../components/chat/SafeMarkdown";
import { ActivityTimeline } from "../components/chat/ActivityTimeline";
import { useChatStream, type ActivitySnapshot } from "../hooks/useChatStream";
import { TechnicalOutput } from "../components/chat/TechnicalOutput";

const MODES = [["/hybrid", "Hybrid", "hybrid"], ["/chat", "Chat Only", "bot"], ["/coding", "Coding", "coding"]] as const;
const USER_MESSAGE_PREVIEW_CHARS = 480;
const USER_MESSAGE_PREVIEW_LINES = 8;

export default function ChatPage() {
  const sessions = useSessionsContext();
  const { user, codingWorkspace, health } = useAppContext();
  const [draft, setDraft] = useState("");
  const [optimistic, setOptimistic] = useState<MessageDto[]>([]);
  const [completedActivity, setCompletedActivity] = useState<ActivitySnapshot | null>(null);
  const [modeBusy, setModeBusy] = useState("");
  const [modeNotice, setModeNotice] = useState("");
  const [attachments, setAttachments] = useState<File[]>([]);
  const [attachmentBusy, setAttachmentBusy] = useState(false);
  const [attachmentError, setAttachmentError] = useState("");
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
  const { active, busy, loadSession, loadOlderMessages, loadingHistory, newSession, reload, setActive } = sessions;

  const onComplete = useCallback((session: SessionDto | null, reply: string, activity: ActivitySnapshot) => {
    if (session) setActive((current) => mergeCompletedSession(current, session));
    else setActive((current) => ({ ...(current || {}), messages: [...(current?.messages || []), ...optimistic, { role: "assistant", content: reply }] }));
    setCompletedActivity(activity);
    setOptimistic([]);
    void reload().catch(() => undefined);
  }, [optimistic, reload, setActive]);
  const stream = useChatStream(onComplete);
  const models = useMemo(() => health.models || [], [health.models]);
  const selectedModel = models.find((item) => item.profile === modelProfile);
  const automaticModel = models.find((item) => item.profile === health.default_model_profile);
  const thinkingAvailable = selectedModel
    ? selectedModel.supports_thinking
    : Boolean(automaticModel?.supports_thinking);
  const activeMessageCount = active?.messages?.length || 0;

  useEffect(() => {
    if (!active && !busy) void loadSession("default").catch(() => newSession());
  }, [active, busy, loadSession, newSession]);
  useEffect(() => {
    shouldStickToBottom.current = true;
  }, [sessions.activeId]);
  useEffect(() => {
    if (!modelProfile || models.some((item) => item.profile === modelProfile)) return;
    setModelProfile("");
  }, [modelProfile, models]);
  useEffect(() => {
    try {
      if (modelProfile) window.localStorage.setItem("taleclaw.model_profile", modelProfile);
      else window.localStorage.removeItem("taleclaw.model_profile");
    } catch { /* storage can be unavailable in private browsing */ }
  }, [modelProfile]);
  useEffect(() => {
    if (!shouldStickToBottom.current) return;
    const frame = window.requestAnimationFrame(() => bottom.current?.scrollIntoView?.({ behavior: stream.status === "streaming" ? "auto" : "smooth", block: "end" }));
    return () => window.cancelAnimationFrame(frame);
  }, [activeMessageCount, optimistic, stream.status, stream.text]);

  const raw = Boolean(active?.channel && active.channel !== "web");
  const messages = useMemo(
    () => [...(active?.messages || []).filter((message) => message.role !== "system"), ...optimistic],
    [active, optimistic],
  );
  const lastAssistantIndex = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index].role === "assistant") return index;
    }
    return -1;
  }, [messages]);

  const handleMessageScroll = useCallback(() => {
    const node = messageScroller.current;
    if (!node) return;
    shouldStickToBottom.current = node.scrollHeight - node.scrollTop - node.clientHeight < 140;
    if (node.scrollTop > 120 || loadingHistory || !active?.message_page?.has_more) return;
    const previousHeight = node.scrollHeight;
    void loadOlderMessages().then((added) => {
      if (!added) return;
      window.requestAnimationFrame(() => window.requestAnimationFrame(() => {
        node.scrollTop += node.scrollHeight - previousHeight;
      }));
    });
  }, [active?.message_page?.has_more, loadOlderMessages, loadingHistory]);

  const send = async () => {
    const selectedFiles = [...attachments];
    const message = draft.trim() || (selectedFiles.length ? "请分析附件内容。" : "");
    if (!message || stream.status === "streaming" || attachmentBusy || raw) return;
    shouldStickToBottom.current = true;
    setDraft("");
    setAttachments([]);
    setAttachmentError("");
    setCompletedActivity(null);
    const display = selectedFiles.length ? `${message}\n\n附件：${selectedFiles.map((file) => file.name).join("、")}` : message;
    setOptimistic([{ role: "user", content: display }]);
    if (composerInput.current) composerInput.current.style.height = "auto";
    try {
      let paths: string[] = [];
      if (selectedFiles.length) {
        setAttachmentBusy(true);
        const form = new FormData();
        form.append("path", `chat-attachments/${uploadId()}`);
        selectedFiles.forEach((file) => form.append("file", file));
        const response = await uploadFormData<{ saved: FileEntry[]; files: FilesResponse }>("/api/files/upload", form);
        paths = (response.saved || []).map((entry) => entry.path);
        if (paths.length !== selectedFiles.length) throw new Error("部分附件上传失败");
      }
      await stream.send(sessions.activeId, message, user.role === "admin" ? codingWorkspace : "", paths, thinkingEnabled, modelProfile);
    } catch (reason) {
      setOptimistic([]);
      setAttachmentError(reason instanceof Error ? reason.message : String(reason));
    } finally {
      setAttachmentBusy(false);
    }
  };

  const selectAttachments = (files: FileList | null) => {
    const incoming = Array.from(files || []);
    setAttachments((current) => [...current, ...incoming].slice(0, 5));
    setAttachmentError(incoming.length + attachments.length > 5 ? "每条消息最多添加 5 个附件。" : "");
    if (attachmentInput.current) attachmentInput.current.value = "";
  };

  const changeMode = async (command: string, label: string) => {
    if (raw || stream.status === "streaming") return;
    setModeBusy(command);
    setModeNotice("");
    try {
      const response = await postJson<SessionResponse & { session?: SessionDto }>("/api/chat", {
        session_id: sessions.activeId,
        message: command,
        ...(user.role === "admin" && codingWorkspace ? { workspace_root: codingWorkspace } : {}),
      });
      if (response.session) setActive((current) => mergeCompletedSession(current, response.session!));
      setModeNotice(`已切换到 ${label}`);
      void reload().catch(() => undefined);
    } catch (reason) {
      setModeNotice(`切换失败：${reason instanceof Error ? reason.message : String(reason)}`);
    } finally {
      setModeBusy("");
    }
  };
  const setAllOutputs = (open: boolean) => messageList.current?.querySelectorAll<HTMLDetailsElement>("details[data-technical-output]").forEach((item) => { item.open = open; });
  const mode = String(active?.active_agent || "hybrid");

  return <div className="page chat-page">
    <header className="chat-header">
      <div><span className="eyebrow"><Sparkles aria-hidden="true" size={12} /> 当前会话</span><h2>{active?.title || sessions.activeId}</h2><span className={`mode-notice ${modeNotice.startsWith("切换失败") ? "error" : ""}`} aria-live="polite">{modeNotice}</span></div>
      <div className="chat-controls"><div className="output-controls" aria-label="技术输出显示"><Button onClick={() => setAllOutputs(true)}><ChevronDown aria-hidden="true" size={14} />展开输出</Button><Button onClick={() => setAllOutputs(false)}><ChevronUp aria-hidden="true" size={14} />收起输出</Button></div><div className="mode-switch">{MODES.map(([command, label, value]) => user.role !== "admin" && value === "coding" ? null : <Button key={command} disabled={Boolean(modeBusy) || stream.status === "streaming"} className={mode === value ? "active" : ""} onClick={() => void changeMode(command, label)}>{modeBusy === command ? "切换中…" : label}</Button>)}</div></div>
    </header>
    <div className="message-scroll" ref={messageScroller} onScroll={handleMessageScroll}>
      <div className="message-list" ref={messageList}>
        {active?.message_page?.has_more && <div className="message-history-loader"><Button disabled={loadingHistory} onClick={() => handleMessageScroll()}>{loadingHistory ? "正在加载更早消息…" : "向上滚动加载更早消息"}</Button></div>}
        {messages.length === 0 && !stream.text && !stream.thinking ? <EmptyState title="今天想先处理什么？" message="从一个具体任务开始，TaleClaw 会持续展示执行过程。" /> : messages.map((message, index) => <article className={`message ${message.role}`} key={message.seq == null ? `${message.role}-pending-${index}` : `message-${message.seq}`}><span className="message-role">{roleLabel(message)}</span><div><MessageContent message={message} activity={index === lastAssistantIndex && completedActivity?.sessionId === sessions.activeId ? completedActivity : null} /></div></article>)}
        {(stream.status === "streaming" || stream.status === "stopping" || stream.text || stream.thinking || stream.error) && <article className={`message assistant streaming ${stream.error ? "error" : ""}`}><span className="message-role">Agent</span><div><ActivityTimeline items={stream.activity} status={stream.status === "error" ? "error" : "running"} startedAt={stream.startedAt} finishedAt={stream.finishedAt} />{stream.progressText && <p className="attachment-progress">{stream.progressText}</p>}{stream.thinking && <ThinkingContent content={stream.thinking} open />}{stream.text && <SafeMarkdown>{stream.text}</SafeMarkdown>}{stream.error && <p className="inline-error">{stream.error}</p>}</div></article>}
        <div ref={bottom} />
      </div>
    </div>
    <div className="composer-dock"><div className="composer">{attachments.length > 0 && <div className="attachment-list">{attachments.map((file, index) => <span className="attachment-chip" key={`${file.name}-${file.size}-${index}`}><Paperclip aria-hidden="true" size={13} />{file.name}<button type="button" aria-label={`移除 ${file.name}`} onClick={() => setAttachments((current) => current.filter((_, itemIndex) => itemIndex !== index))}><X aria-hidden="true" size={12} /></button></span>)}</div>}{attachmentError && <p className="composer-error">{attachmentError}</p>}<textarea ref={composerInput} value={draft} disabled={raw || stream.status === "streaming" || attachmentBusy} onChange={(event) => { setDraft(event.target.value); event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 190)}px`; }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); void send(); } }} placeholder={raw ? "只读会话" : "输入问题，并可添加 PDF、Word、PPT 或图片附件……"} /><div><span>{attachmentBusy ? "正在上传附件" : stream.status === "streaming" ? "思考与执行中" : stream.status === "stopping" ? "正在停止" : raw ? "只读会话" : "附件将先经 MinerU 精准解析"}</span>{stream.status === "streaming" ? <Button onClick={() => void stream.stop(sessions.activeId)}><CircleStop aria-hidden="true" size={14} />停止</Button> : <div className="composer-actions"><label className="model-picker"><span>模型</span><select value={modelProfile} disabled={raw || attachmentBusy} onChange={(event) => { setModelProfile(event.target.value); setThinkingEnabled(false); }} aria-label="选择模型">{models.length === 0 && <option value="">自动路由</option>}{models.length > 0 && <option value="">自动路由</option>}{models.map((option: ModelOption) => <option key={option.profile} value={option.profile}>{option.model} · {option.provider}</option>)}</select></label><Button title={thinkingAvailable ? "启用模型深度思考" : "当前模型不支持思考模式"} disabled={!thinkingAvailable || raw || attachmentBusy} className={thinkingEnabled ? "active" : ""} onClick={() => setThinkingEnabled((value) => !value)}>深度思考</Button><Button aria-label="添加附件" disabled={raw || attachmentBusy} onClick={() => attachmentInput.current?.click()}><Paperclip aria-hidden="true" size={14} />附件</Button><input ref={attachmentInput} hidden multiple type="file" accept=".pdf,.doc,.docx,.ppt,.pptx,.png,.jpg,.jpeg,.jp2,.webp,.gif,.bmp" onChange={(event) => selectAttachments(event.target.files)} /><Button className="primary" onClick={() => void send()} disabled={(!draft.trim() && !attachments.length) || raw || attachmentBusy}><Send aria-hidden="true" size={14} /><span>发送</span></Button></div>}</div></div></div>
  </div>;
}

function uploadId() {
  return typeof crypto !== "undefined" && "randomUUID" in crypto ? crypto.randomUUID() : `${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

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

function mergeCompletedSession(current: SessionDto | null, incoming: SessionDto) {
  if (!current) return incoming;
  const currentMessages = current.messages || [];
  const incomingMessages = incoming.messages || [];
  const hasSequence = [...currentMessages, ...incomingMessages].some((message) => message.seq != null);
  if (!hasSequence) return incoming;
  const bySequence = new Map<number, MessageDto>();
  for (const message of [...currentMessages, ...incomingMessages]) {
    if (message.seq != null) bySequence.set(message.seq, message);
  }
  const messages = [...bySequence.entries()].sort(([left], [right]) => left - right).map(([, message]) => message);
  const currentHasOlderPage = currentMessages.length > incomingMessages.length;
  return {
    ...current,
    ...incoming,
    messages,
    message_page: currentHasOlderPage ? current.message_page : incoming.message_page,
  };
}

function roleLabel(message: MessageDto) {
  if (message.role === "user") return "你";
  if (message.role === "assistant") return "Agent";
  if (message.role === "tool") return "工具输出";
  if (message.role === "function") return "函数输出";
  return message.role || "运行输出";
}
