import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { ChevronDown, ChevronUp, CircleStop, Send, Sparkles } from "lucide-react";
import { postJson } from "../api/client";
import type { MessageDto, SessionDto, SessionResponse } from "../api/types";
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
  const { user, codingWorkspace } = useAppContext();
  const [draft, setDraft] = useState("");
  const [optimistic, setOptimistic] = useState<MessageDto[]>([]);
  const [completedActivity, setCompletedActivity] = useState<ActivitySnapshot | null>(null);
  const [modeBusy, setModeBusy] = useState("");
  const [modeNotice, setModeNotice] = useState("");
  const bottom = useRef<HTMLDivElement>(null);
  const messageList = useRef<HTMLDivElement>(null);
  const messageScroller = useRef<HTMLDivElement>(null);
  const composerInput = useRef<HTMLTextAreaElement>(null);
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
  const activeMessageCount = active?.messages?.length || 0;

  useEffect(() => {
    if (!active && !busy) void loadSession("default").catch(() => newSession());
  }, [active, busy, loadSession, newSession]);
  useEffect(() => {
    shouldStickToBottom.current = true;
  }, [sessions.activeId]);
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

  const send = () => {
    const message = draft.trim();
    if (!message || stream.status === "streaming" || raw) return;
    shouldStickToBottom.current = true;
    setDraft("");
    setCompletedActivity(null);
    setOptimistic([{ role: "user", content: message }]);
    if (composerInput.current) composerInput.current.style.height = "auto";
    void stream.send(sessions.activeId, message, user.role === "admin" ? codingWorkspace : "");
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
  const mode = String(active?.current_mode || "hybrid");

  return <div className="page chat-page">
    <header className="chat-header">
      <div><span className="eyebrow"><Sparkles aria-hidden="true" size={12} /> 当前会话</span><h2>{active?.title || sessions.activeId}</h2><span className={`mode-notice ${modeNotice.startsWith("切换失败") ? "error" : ""}`} aria-live="polite">{modeNotice}</span></div>
      <div className="chat-controls"><div className="output-controls" aria-label="技术输出显示"><Button onClick={() => setAllOutputs(true)}><ChevronDown aria-hidden="true" size={14} />展开输出</Button><Button onClick={() => setAllOutputs(false)}><ChevronUp aria-hidden="true" size={14} />收起输出</Button></div><div className="mode-switch">{MODES.map(([command, label, value]) => user.role !== "admin" && value === "coding" ? null : <Button key={command} disabled={Boolean(modeBusy) || stream.status === "streaming"} className={mode === value ? "active" : ""} onClick={() => void changeMode(command, label)}>{modeBusy === command ? "切换中…" : label}</Button>)}</div></div>
    </header>
    <div className="message-scroll" ref={messageScroller} onScroll={handleMessageScroll}>
      <div className="message-list" ref={messageList}>
        {active?.message_page?.has_more && <div className="message-history-loader"><Button disabled={loadingHistory} onClick={() => handleMessageScroll()}>{loadingHistory ? "正在加载更早消息…" : "向上滚动加载更早消息"}</Button></div>}
        {messages.length === 0 && !stream.text ? <EmptyState title="今天想先处理什么？" message="从一个具体任务开始，TaleClaw 会持续展示执行过程。" /> : messages.map((message, index) => <article className={`message ${message.role}`} key={message.seq == null ? `${message.role}-pending-${index}` : `message-${message.seq}`}><span className="message-role">{roleLabel(message)}</span><div><MessageContent message={message} activity={index === lastAssistantIndex && completedActivity?.sessionId === sessions.activeId ? completedActivity : null} /></div></article>)}
        {(stream.status === "streaming" || stream.status === "stopping" || stream.text || stream.error) && <article className={`message assistant streaming ${stream.error ? "error" : ""}`}><span className="message-role">Agent</span><div><ActivityTimeline items={stream.activity} status={stream.status === "error" ? "error" : "running"} startedAt={stream.startedAt} finishedAt={stream.finishedAt} />{stream.text && <SafeMarkdown>{stream.text}</SafeMarkdown>}{stream.error && <p className="inline-error">{stream.error}</p>}</div></article>}
        <div ref={bottom} />
      </div>
    </div>
    <div className="composer-dock"><div className="composer"><textarea ref={composerInput} value={draft} disabled={raw || stream.status === "streaming"} onChange={(event) => { setDraft(event.target.value); event.currentTarget.style.height = "auto"; event.currentTarget.style.height = `${Math.min(event.currentTarget.scrollHeight, 190)}px`; }} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) { event.preventDefault(); send(); } }} placeholder={raw ? "只读会话" : "让 taleclaw 编写代码、诊断问题或分析系统……"} /><div><span>{stream.status === "streaming" ? "思考与执行中" : stream.status === "stopping" ? "正在停止" : raw ? "只读会话" : "Enter 发送 · Shift+Enter 换行"}</span>{stream.status === "streaming" ? <Button onClick={() => void stream.stop(sessions.activeId)}><CircleStop aria-hidden="true" size={14} />停止</Button> : <Button className="primary" onClick={send} disabled={!draft.trim() || raw}><Send aria-hidden="true" size={14} />发送</Button>}</div></div></div>
  </div>;
}

function MessageContent({ message, activity }: { message: MessageDto; activity?: ActivitySnapshot | null }) {
  const content = String(message.content || "");
  if (message.role === "assistant") return <>{activity && <ActivityTimeline items={activity.items} status={activity.status} startedAt={activity.startedAt} finishedAt={activity.finishedAt} />}{content && <SafeMarkdown>{content}</SafeMarkdown>}{Boolean(message.tool_calls?.length) && <TechnicalOutput label={`工具调用 · ${message.tool_calls!.length}`} value={message.tool_calls} />}</>;
  if (message.role === "user") return <UserMessageContent content={content} />;
  return <TechnicalOutput label={message.name ? `${roleLabel(message)} · ${message.name}` : roleLabel(message)} value={content} />;
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
