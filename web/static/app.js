const CODING_WORKSPACE_STORAGE_KEY = "codingWorkspaceRoot";

const state = {
  sessionId: "default",
  rawSession: false,
  busy: false,
  sessions: [],
  memoryFiles: [],
  activeMemory: "MEMORY.md",
  sessionFilter: "",
  sidebarPanel: localStorage.getItem("sidebarPanel") || "sessions",
  mainView: localStorage.getItem("mainView") || "chat",
  sidebarCollapsed: localStorage.getItem("sidebarCollapsed") === "1",
  mobileSidebarOpen: false,
  currentMode: "hybrid",
  filePath: "",
  fileParent: "",
  fileEntries: [],
  analysisBusy: false,
  currentUser: { id: "local", role: "admin" },
  defaultCodingWorkspaceRoot: "",
  codingWorkspaceRoot: localStorage.getItem(CODING_WORKSPACE_STORAGE_KEY) || "",
};

const els = {
  appShell: document.querySelector(".app-shell"),
  sidebarToggle: document.querySelector("#sidebarToggle"),
  sidebarOverlay: document.querySelector("#sidebarOverlay"),
  mobileSidebarOpen: document.querySelector("#mobileSidebarOpen"),
  mobileSecondaryOpen: [...document.querySelectorAll(".mobile-secondary-open")],
  sidebarTabs: [...document.querySelectorAll("[data-sidebar-tab]")],
  sidebarPanels: [...document.querySelectorAll("[data-sidebar-panel]")],
  mainViewPanels: [...document.querySelectorAll("[data-main-view-panel]")],
  workspaceLabel: document.querySelector("#workspaceLabel"),
  workspacePath: document.querySelector("#workspacePath"),
  workspaceForm: document.querySelector("#workspaceForm"),
  workspaceInput: document.querySelector("#workspaceInput"),
  workspaceSaveBtn: document.querySelector("#workspaceSaveBtn"),
  workspaceResetBtn: document.querySelector("#workspaceResetBtn"),
  codingWorkspacePath: document.querySelector("#codingWorkspacePath"),
  statusBadge: document.querySelector("#statusBadge"),
  sessionsList: document.querySelector("#sessionsList"),
  sessionSearch: document.querySelector("#sessionSearch"),
  newSessionBtn: document.querySelector("#newSessionBtn"),
  refreshMemoryBtn: document.querySelector("#refreshMemoryBtn"),
  memoryTabs: document.querySelector("#memoryTabs"),
  memoryContent: document.querySelector("#memoryContent"),
  sessionTitle: document.querySelector("#sessionTitle"),
  chatScroll: document.querySelector("#chatScroll"),
  messages: document.querySelector("#messages"),
  composer: document.querySelector("#composer"),
  input: document.querySelector("#messageInput"),
  sendBtn: document.querySelector("#sendBtn"),
  stopBtn: document.querySelector("#stopBtn"),
  composerState: document.querySelector("#composerState"),
  modeActions: document.querySelector(".mode-actions"),
  activeSessionMetric: document.querySelector("#activeSessionMetric"),
  modeMetric: document.querySelector("#modeMetric"),
  sessionCountMetric: document.querySelector("#sessionCountMetric"),
  memoryCountMetric: document.querySelector("#memoryCountMetric"),
  currentUserMetric: document.querySelector("#currentUserMetric"),
  sidebarUserLabel: document.querySelector("#sidebarUserLabel"),
  logoutBtn: document.querySelector("#logoutBtn"),
  refreshFilesBtn: document.querySelector("#refreshFilesBtn"),
  filePathLabel: document.querySelector("#filePathLabel"),
  fileCountMetric: document.querySelector("#fileCountMetric"),
  fileUpBtn: document.querySelector("#fileUpBtn"),
  fileUploadBtn: document.querySelector("#fileUploadBtn"),
  fileMkdirBtn: document.querySelector("#fileMkdirBtn"),
  fileUploadInput: document.querySelector("#fileUploadInput"),
  fileBreadcrumb: document.querySelector("#fileBreadcrumb"),
  fileList: document.querySelector("#fileList"),
  filePreviewModal: document.querySelector("#filePreviewModal"),
  filePreviewClose: document.querySelector("#filePreviewClose"),
  filePreview: document.querySelector("#filePreview"),
  analysisRecordPath: document.querySelector("#analysisRecordPath"),
  analysisDownloadLink: document.querySelector("#analysisDownloadLink"),
  analysisForm: document.querySelector("#analysisForm"),
  analysisInput: document.querySelector("#analysisInput"),
  analysisState: document.querySelector("#analysisState"),
  analysisSubmitBtn: document.querySelector("#analysisSubmitBtn"),
  analysisOutput: document.querySelector("#analysisOutput"),
};

async function fetchJson(url, options = {}) {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, {
    ...options,
    headers,
  });
  const contentType = response.headers.get("Content-Type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : { error: await response.text() };
  if (response.status === 401) {
    redirectToLogin();
    throw new Error("登录状态已失效");
  }
  if (!response.ok) {
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  return data;
}

async function fetchJsonStream(url, options = {}, onEvent = () => {}) {
  const headers = new Headers(options.headers || {});
  if (!(options.body instanceof FormData) && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  const response = await fetch(url, {
    ...options,
    headers,
  });
  if (response.status === 401) {
    redirectToLogin();
    throw new Error("登录状态已失效");
  }
  if (!response.ok) {
    const contentType = response.headers.get("Content-Type") || "";
    const data = contentType.includes("application/json")
      ? await response.json()
      : { error: await response.text() };
    throw new Error(data.error || `HTTP ${response.status}`);
  }
  if (!response.body) {
    throw new Error("当前浏览器不支持流式响应");
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
    const lines = buffer.split("\n");
    buffer = lines.pop() || "";
    for (const line of lines) {
      if (!line.trim()) continue;
      onEvent(JSON.parse(line));
    }
    if (done) break;
  }
  if (buffer.trim()) {
    onEvent(JSON.parse(buffer));
  }
}

function setStatus(text, kind = "") {
  els.statusBadge.textContent = text;
  els.statusBadge.className = `status-badge ${kind}`.trim();
}

function activeCodingWorkspaceRoot() {
  if (state.currentUser.role !== "admin") return "";
  return (state.codingWorkspaceRoot || state.defaultCodingWorkspaceRoot || "").trim();
}

function setCodingWorkspaceRoot(value, { persist = true } = {}) {
  state.codingWorkspaceRoot = (value || "").trim();
  if (persist) {
    if (state.codingWorkspaceRoot) {
      localStorage.setItem(CODING_WORKSPACE_STORAGE_KEY, state.codingWorkspaceRoot);
    } else {
      localStorage.removeItem(CODING_WORKSPACE_STORAGE_KEY);
    }
  }
  renderCodingWorkspace();
}

function resetCodingWorkspaceRoot() {
  localStorage.removeItem(CODING_WORKSPACE_STORAGE_KEY);
  state.codingWorkspaceRoot = "";
  renderCodingWorkspace();
}

function renderCodingWorkspace() {
  const admin = state.currentUser.role === "admin";
  const active = activeCodingWorkspaceRoot();
  if (els.workspaceForm) {
    els.workspaceForm.hidden = !admin;
  }
  if (els.workspaceInput) {
    els.workspaceInput.disabled = !admin || state.busy || state.rawSession;
    els.workspaceInput.value = state.codingWorkspaceRoot || state.defaultCodingWorkspaceRoot || "";
  }
  if (els.workspaceSaveBtn) {
    els.workspaceSaveBtn.disabled = !admin || state.busy || state.rawSession;
  }
  if (els.workspaceResetBtn) {
    els.workspaceResetBtn.disabled = !admin || state.busy || state.rawSession || !state.defaultCodingWorkspaceRoot;
  }
  if (els.codingWorkspacePath) {
    els.codingWorkspacePath.textContent = active || "-";
  }
}

function workspacePayload() {
  const workspaceRoot = activeCodingWorkspaceRoot();
  return workspaceRoot ? { workspace_root: workspaceRoot } : {};
}

function redirectToLogin() {
  const next = `${window.location.pathname}${window.location.search}`;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

function setBusy(value) {
  state.busy = value;
  els.sendBtn.disabled = value || state.rawSession;
  els.sendBtn.hidden = value;
  els.stopBtn.hidden = !value;
  els.stopBtn.disabled = !value || state.rawSession;
  els.input.disabled = value || state.rawSession;
  for (const button of els.sessionsList.querySelectorAll(".session-delete")) {
    button.disabled = value;
  }
  els.composerState.textContent = value ? "思考中" : state.rawSession ? "只读会话" : "";
  renderCodingWorkspace();
}

async function stopCurrentTurn() {
  if (!state.busy || state.rawSession) return;
  els.stopBtn.disabled = true;
  els.composerState.textContent = "正在停止...";
  try {
    const data = await fetchJson("/api/chat/stop", {
      method: "POST",
      body: JSON.stringify({ session_id: state.sessionId }),
    });
    els.composerState.textContent = data.requested
      ? "停止请求已发送，当前步骤结束后会停下"
      : "当前会话没有运行中的任务";
  } catch (error) {
    els.stopBtn.disabled = false;
    els.composerState.textContent = error.message;
    setStatus("异常", "error");
  }
}

function setSidebarPanel(panelName) {
  if (!els.sidebarPanels.some((panel) => panel.dataset.sidebarPanel === panelName)) {
    panelName = "sessions";
  }
  state.sidebarPanel = panelName;
  localStorage.setItem("sidebarPanel", panelName);

  for (const tab of els.sidebarTabs) {
    tab.classList.toggle("active", tab.dataset.sidebarTab === panelName);
  }
  for (const panel of els.sidebarPanels) {
    panel.classList.toggle("active", panel.dataset.sidebarPanel === panelName);
  }
}

function setMainView(viewName) {
  if (!els.mainViewPanels.some((panel) => panel.dataset.mainViewPanel === viewName)) {
    viewName = "chat";
  }
  state.mainView = viewName;
  localStorage.setItem("mainView", viewName);

  for (const panel of els.mainViewPanels) {
    panel.classList.toggle("active", panel.dataset.mainViewPanel === viewName);
  }

  if (viewName === "files" && state.fileEntries.length === 0) {
    loadFiles().catch(showFileError);
  }
}

function isMobileViewport() {
  return window.matchMedia("(max-width: 860px)").matches;
}

function setMobileSidebarOpen(value) {
  state.mobileSidebarOpen = value;
  els.appShell.classList.toggle("is-sidebar-open", value);
  document.body.classList.toggle("no-scroll", value);
  els.mobileSidebarOpen.setAttribute("aria-expanded", value ? "true" : "false");
  els.sidebarToggle.textContent = value && isMobileViewport() ? "×" : "‹";
  if (isMobileViewport()) {
    els.sidebarToggle.title = value ? "关闭侧栏" : "折叠侧栏";
    els.sidebarToggle.setAttribute("aria-label", value ? "关闭侧栏" : "折叠侧栏");
  }
}

function setSidebarCollapsed(value) {
  state.sidebarCollapsed = value;
  localStorage.setItem("sidebarCollapsed", value ? "1" : "0");
  els.appShell.classList.toggle("is-collapsed", !isMobileViewport() && value);
  els.sidebarToggle.title = value ? "展开侧栏" : "折叠侧栏";
  els.sidebarToggle.setAttribute("aria-label", value ? "展开侧栏" : "折叠侧栏");
}

function syncResponsiveSidebar() {
  if (isMobileViewport()) {
    els.appShell.classList.remove("is-collapsed");
    setMobileSidebarOpen(false);
    return;
  }
  setMobileSidebarOpen(false);
  els.appShell.classList.toggle("is-collapsed", state.sidebarCollapsed);
}

function updateMetrics(session = {}) {
  const mode = session.current_mode || state.currentMode || "hybrid";
  state.currentMode = mode;
  els.activeSessionMetric.textContent = state.sessionId || "default";
  els.modeMetric.textContent = mode;
  els.sessionCountMetric.textContent = String(state.sessions.length);
  els.memoryCountMetric.textContent = String(state.memoryFiles.length);
  els.currentUserMetric.textContent = `${state.currentUser.id} · ${state.currentUser.role}`;
  const activeCommand = {
    hybrid: "/hybrid",
    bot: "/chat",
    coding: "/coding",
  }[mode];
  for (const button of els.modeActions.querySelectorAll("[data-command]")) {
    const active = button.dataset.command === activeCommand;
    button.classList.toggle("active", active);
    button.setAttribute("aria-pressed", active ? "true" : "false");
  }
  renderCodingWorkspace();
}

function messageText(message) {
  const content = message?.content ?? "";
  if (typeof content === "string") return content;
  if (Array.isArray(content)) {
    return content
      .map((part) => {
        if (typeof part === "string") return part;
        return part?.text || JSON.stringify(part);
      })
      .join("");
  }
  return JSON.stringify(content, null, 2);
}

function roleLabel(role) {
  const labels = {
    user: "你",
    assistant: "Agent",
    tool: "Tool",
    system: "System",
  };
  return labels[role] || role || "message";
}

function toolCallName(call) {
  return call?.function?.name || call?.name || "unknown";
}

function renderToolDisclosure(message, toolNamesByCallId) {
  const isRequest = message.role === "assistant";
  const calls = Array.isArray(message.tool_calls) ? message.tool_calls : [];
  const names = isRequest
    ? calls.map(toolCallName)
    : [toolNamesByCallId.get(message.tool_call_id) || message.name || "unknown"];
  const label = isRequest ? "工具请求" : "工具结果";

  const details = document.createElement("details");
  details.className = "tool-disclosure";

  const summary = document.createElement("summary");
  const summaryLabel = document.createElement("span");
  summaryLabel.textContent = label;
  const summaryTools = document.createElement("code");
  summaryTools.textContent = [...new Set(names)].join(", ");
  summary.append(summaryLabel, summaryTools);

  const content = document.createElement("pre");
  content.textContent = isRequest
    ? JSON.stringify(calls, null, 2)
    : messageText(message);

  details.append(summary, content);
  return details;
}

function scrollMessagesToBottom(force = false) {
  const { scrollTop, scrollHeight, clientHeight } = els.chatScroll;
  const isNearBottom = scrollHeight - scrollTop - clientHeight < 150;
  if (force || isNearBottom) {
    els.chatScroll.scrollTop = els.chatScroll.scrollHeight;
  }
}

function renderMessages(messages, forceScroll = false) {
  const { scrollTop, scrollHeight, clientHeight } = els.chatScroll;
  const isNearBottom = scrollHeight === 0 || (scrollHeight - scrollTop - clientHeight < 150);

  els.messages.innerHTML = "";
  const visibleMessages = (messages || []).filter((message) => message.role !== "system");
  if (visibleMessages.length === 0) {
    renderChatEmptyState();
    return;
  }

  const toolNamesByCallId = new Map();
  for (const message of visibleMessages) {
    for (const call of message.tool_calls || []) {
      if (call?.id) toolNamesByCallId.set(call.id, toolCallName(call));
    }
  }

  for (const message of visibleMessages) {
    const item = document.createElement("article");
    item.className = `message ${message.role || "assistant"}`;

    const role = document.createElement("div");
    role.className = "message-role";
    role.textContent = roleLabel(message.role);

    const body = document.createElement("div");
    body.className = "message-body";
    const hasToolRequest = message.role === "assistant"
      && Array.isArray(message.tool_calls)
      && message.tool_calls.length > 0;
    if (message.role === "tool") {
      body.append(renderToolDisclosure(message, toolNamesByCallId));
    } else if (message.role === "assistant" && message.display_html) {
      body.classList.add("markdown-body");
      body.innerHTML = message.display_html;

      body.querySelectorAll("pre").forEach((pre) => {
        const wrapper = document.createElement("div");
        wrapper.className = "code-block-wrapper";
        pre.parentNode.insertBefore(wrapper, pre);
        wrapper.appendChild(pre);

        const copyBtn = document.createElement("button");
        copyBtn.type = "button";
        copyBtn.className = "text-button copy-code-btn";
        copyBtn.textContent = "复制";
        copyBtn.addEventListener("click", async () => {
          const text = pre.querySelector("code")?.textContent || pre.textContent;
          try {
            await navigator.clipboard.writeText(text);
            copyBtn.textContent = "已复制!";
            copyBtn.classList.add("copied");
            setTimeout(() => {
              copyBtn.textContent = "复制";
              copyBtn.classList.remove("copied");
            }, 2000);
          } catch (err) {
            copyBtn.textContent = "失败";
            setTimeout(() => (copyBtn.textContent = "复制"), 2000);
          }
        });
        wrapper.appendChild(copyBtn);
      });

      body.querySelectorAll("table").forEach((table) => {
        if (table.parentElement?.classList.contains("table-block-wrapper")) return;
        const wrapper = document.createElement("div");
        wrapper.className = "table-block-wrapper";
        table.parentNode.insertBefore(wrapper, table);
        wrapper.appendChild(table);
      });
    } else {
      body.textContent = messageText(message);
    }
    if (hasToolRequest) {
      body.append(renderToolDisclosure(message, toolNamesByCallId));
    }

    item.append(role, body);
    els.messages.append(item);
  }
  if (forceScroll || isNearBottom) {
    scrollMessagesToBottom(true);
  } else {
    els.chatScroll.scrollTop = scrollTop;
  }
}

function renderChatEmptyState() {
  const empty = document.createElement("section");
  empty.className = "empty-state chat-empty";

  const mark = document.createElement("div");
  mark.className = "empty-mark";
  mark.setAttribute("aria-hidden", "true");

  const title = document.createElement("h3");
  title.textContent = "今天想先处理什么？";

  const description = document.createElement("p");
  description.textContent = "可以从一个具体任务开始。";

  const suggestions = document.createElement("div");
  suggestions.className = "empty-suggestions";
  const prompts = [
    ["整理当前项目", "请总结当前项目状态，并列出最值得先处理的三件事。"],
    ["搜索 AI 动态", "请搜索今天值得关注的 AI 动态，并给出简短分析。"],
    ["查看近期记忆", "请结合近期记忆，告诉我目前有哪些待处理事项。"],
  ];
  for (const [label, prompt] of prompts) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = label;
    button.addEventListener("click", () => {
      els.input.value = prompt;
      els.input.focus();
    });
    suggestions.append(button);
  }

  empty.append(mark, title, description, suggestions);
  els.messages.append(empty);
}

function renderSessions(sessions = state.sessions) {
  els.sessionsList.innerHTML = "";
  const filter = state.sessionFilter.trim().toLowerCase();
  const rows = (sessions || []).filter((session) => {
    const label = session.channel === "web" ? session.chat_id : session.id;
    return !filter || `${label} ${session.current_mode || ""}`.toLowerCase().includes(filter);
  });

  if (rows.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = filter ? "没有匹配会话" : "暂无会话";
    els.sessionsList.append(empty);
    updateMetrics();
    return;
  }

  for (const session of rows) {
    const row = document.createElement("div");
    row.className = "session-row";

    const button = document.createElement("button");
    const isWeb = session.channel === "web";
    const label = isWeb ? session.chat_id : session.id;
    button.type = "button";
    button.className = `session-item ${label === state.sessionId ? "active" : ""}`;

    const name = document.createElement("span");
    name.className = "session-name";
    name.textContent = label;

    const meta = document.createElement("span");
    meta.className = "session-meta";
    meta.textContent = `${session.current_mode || "hybrid"} · ${formatDate(session.updated_at)}`;

    button.append(name, meta);
    button.addEventListener("click", () => {
      state.rawSession = !isWeb;
      state.sessionId = label;
      setMainView("chat");
      loadSession(label, !isWeb);
      if (isMobileViewport()) {
        setMobileSidebarOpen(false);
      }
    });

    row.append(button);
    if (isWeb) {
      const deleteButton = document.createElement("button");
      deleteButton.type = "button";
      deleteButton.className = "session-delete";
      deleteButton.textContent = "×";
      deleteButton.title = `删除会话 ${label}`;
      deleteButton.setAttribute("aria-label", `删除会话 ${label}`);
      deleteButton.disabled = state.busy;
      deleteButton.addEventListener("click", () => {
        deleteSession(session).catch((error) => {
          setStatus("删除失败", "error");
          els.composerState.textContent = error.message;
        });
      });
      row.append(deleteButton);
    }
    els.sessionsList.append(row);
  }
  updateMetrics();
}

async function deleteSession(session) {
  const label = session.channel === "web" ? session.chat_id : "";
  if (!label || state.busy) return;
  if (!window.confirm(`删除会话 ${label}？此操作不能撤销。`)) return;

  const data = await fetchJson("/api/session", {
    method: "DELETE",
    body: JSON.stringify({ session_id: label }),
  });
  state.sessions = data.sessions || [];
  setStatus("已删除", "ready");

  if (label !== state.sessionId || state.rawSession) {
    renderSessions();
    return;
  }

  const next = state.sessions.find((item) => item.channel === "web");
  if (next) {
    state.sessionId = next.chat_id;
    state.rawSession = false;
    await loadSession(next.chat_id, false);
    return;
  }
  newSession();
}

function renderMemory() {
  els.memoryTabs.innerHTML = "";
  for (const file of state.memoryFiles) {
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = file.name.replace(".md", "");
    button.className = file.name === state.activeMemory ? "active" : "";
    button.addEventListener("click", () => {
      state.activeMemory = file.name;
      renderMemory();
    });
    els.memoryTabs.append(button);
  }

  const active = state.memoryFiles.find((file) => file.name === state.activeMemory)
    || state.memoryFiles[0];
  els.memoryContent.textContent = active?.content || "";
  updateMetrics();
}

function formatBytes(value = 0) {
  if (!value) return "0 B";
  const units = ["B", "KB", "MB", "GB"];
  let size = Number(value);
  let unitIndex = 0;
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024;
    unitIndex += 1;
  }
  const digits = unitIndex === 0 ? 0 : 1;
  return `${size.toFixed(digits)} ${units[unitIndex]}`;
}

function storageDisplayPath(path = state.filePath) {
  return path ? `/${path}` : "/";
}

function downloadUrl(path) {
  return `/api/files/download?path=${encodeURIComponent(path)}`;
}

function renderFileBreadcrumb() {
  els.fileBreadcrumb.innerHTML = "";

  const root = document.createElement("button");
  root.type = "button";
  root.textContent = "storage";
  root.addEventListener("click", () => loadFiles("").catch(showFileError));
  els.fileBreadcrumb.append(root);

  const parts = state.filePath.split("/").filter(Boolean);
  let current = "";
  for (const part of parts) {
    current = current ? `${current}/${part}` : part;
    const target = current;
    const button = document.createElement("button");
    button.type = "button";
    button.textContent = part;
    button.addEventListener("click", () => loadFiles(target).catch(showFileError));
    els.fileBreadcrumb.append(button);
  }
}

function openFilePreviewModal() {
  if (!els.filePreviewModal.open) {
    els.filePreviewModal.showModal();
  }
}

function closeFilePreviewModal() {
  if (els.filePreviewModal.open) {
    els.filePreviewModal.close();
  }
}

function renderFilePreviewEmpty(text = "这个文件不能直接预览，可以下载查看", file = null) {
  els.filePreview.innerHTML = "";
  if (file?.path) {
    const title = document.createElement("div");
    title.className = "preview-title";

    const name = document.createElement("strong");
    name.textContent = file.name || file.path;

    const link = document.createElement("a");
    link.href = downloadUrl(file.path);
    link.textContent = "下载";

    title.append(name, link);
    els.filePreview.append(title);
  }
  const empty = document.createElement("div");
  empty.className = "preview-empty";
  empty.textContent = text;
  els.filePreview.append(empty);
  openFilePreviewModal();
}

function renderFilePreviewContent(file, content) {
  els.filePreview.innerHTML = "";
  const title = document.createElement("div");
  title.className = "preview-title";

  const name = document.createElement("strong");
  name.textContent = file.name || file.path;

  const link = document.createElement("a");
  link.href = downloadUrl(file.path);
  link.textContent = "下载";

  title.append(name, link);

  const pre = document.createElement("pre");
  pre.textContent = content;
  els.filePreview.append(title, pre);
  openFilePreviewModal();
}

function renderFiles() {
  els.filePathLabel.textContent = storageDisplayPath();
  els.fileCountMetric.textContent = String(state.fileEntries.length);
  els.fileUpBtn.disabled = !state.filePath;
  renderFileBreadcrumb();

  els.fileList.innerHTML = "";
  if (state.fileEntries.length === 0) {
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = "这个目录是空的";
    els.fileList.append(empty);
    return;
  }

  for (const entry of state.fileEntries) {
    const row = document.createElement("article");
    row.className = "file-row";

    const main = document.createElement("button");
    main.type = "button";
    main.className = "file-main";

    const name = document.createElement("span");
    name.className = "file-name";
    name.textContent = `${entry.is_dir ? "目录" : "文件"} · ${entry.name}`;

    const meta = document.createElement("span");
    meta.className = "file-meta";
    const modified = formatDate(entry.modified);
    meta.textContent = entry.is_dir
      ? `文件夹 · ${modified}`
      : `${formatBytes(entry.size)} · ${entry.mime || "file"} · ${modified}`;

    main.append(name, meta);
    main.addEventListener("click", () => {
      if (entry.is_dir) {
        loadFiles(entry.path).catch(showFileError);
        return;
      }
      previewFile(entry).catch(showFileError);
    });

    const actions = document.createElement("div");
    actions.className = "file-row-actions";

    const openButton = document.createElement("button");
    openButton.type = "button";
    openButton.textContent = entry.is_dir ? "打开" : "预览";
    openButton.addEventListener("click", () => {
      if (entry.is_dir) {
        loadFiles(entry.path).catch(showFileError);
      } else {
        previewFile(entry).catch(showFileError);
      }
    });
    actions.append(openButton);

    if (!entry.is_dir) {
      const download = document.createElement("a");
      download.href = downloadUrl(entry.path);
      download.textContent = "下载";
      actions.append(download);
    }

    const renameButton = document.createElement("button");
    renameButton.type = "button";
    renameButton.textContent = "重命名";
    renameButton.addEventListener("click", () => renameEntry(entry));
    actions.append(renameButton);

    const deleteButton = document.createElement("button");
    deleteButton.type = "button";
    deleteButton.textContent = "删除";
    deleteButton.addEventListener("click", () => deleteEntry(entry));
    actions.append(deleteButton);

    row.append(main, actions);
    els.fileList.append(row);
  }
}

function showFileError(error) {
  renderFilePreviewEmpty(error.message || String(error));
  setStatus("异常", "error");
}

async function loadFiles(path = state.filePath) {
  const data = await fetchJson(`/api/files?path=${encodeURIComponent(path || "")}`);
  const files = data.files || {};
  state.filePath = files.path || "";
  state.fileParent = files.parent || "";
  state.fileEntries = files.entries || [];
  if (files.record_path) {
    els.analysisRecordPath.textContent = files.record_path;
    els.analysisDownloadLink.href = downloadUrl(files.record_path);
  }
  renderFiles();
  closeFilePreviewModal();
  return files;
}

async function previewFile(entry) {
  if (!entry.previewable) {
    renderFilePreviewEmpty("这个文件不能直接预览，可以下载查看", entry);
    return;
  }
  const data = await fetchJson(`/api/files/preview?path=${encodeURIComponent(entry.path)}`);
  renderFilePreviewContent(data, data.content || "");
}

async function makeDirectory() {
  const name = window.prompt("文件夹名称");
  if (!name) return;
  const data = await fetchJson("/api/files/mkdir", {
    method: "POST",
    body: JSON.stringify({ path: state.filePath, name }),
  });
  state.fileEntries = data.files?.entries || [];
  renderFiles();
}

async function renameEntry(entry) {
  const name = window.prompt("新的名称", entry.name);
  if (!name || name === entry.name) return;
  const data = await fetchJson("/api/files/rename", {
    method: "POST",
    body: JSON.stringify({ path: entry.path, name }),
  });
  state.fileEntries = data.files?.entries || [];
  renderFiles();
  closeFilePreviewModal();
}

async function deleteEntry(entry) {
  if (!window.confirm(`删除 ${entry.name}？`)) return;
  const data = await fetchJson("/api/files/delete", {
    method: "POST",
    body: JSON.stringify({ path: entry.path }),
  });
  state.fileEntries = data.files?.entries || [];
  renderFiles();
  closeFilePreviewModal();
}

async function uploadFiles() {
  const files = [...els.fileUploadInput.files];
  if (files.length === 0) return;
  const form = new FormData();
  form.append("path", state.filePath);
  for (const file of files) {
    form.append("file", file);
  }
  try {
    const data = await fetchJson("/api/files/upload", {
      method: "POST",
      body: form,
    });
    state.fileEntries = data.files?.entries || [];
    renderFiles();
    closeFilePreviewModal();
    setStatus(`${files.length} 个文件已上传`, "ready");
  } catch (error) {
    showFileError(error);
  } finally {
    els.fileUploadInput.value = "";
  }
}

function setAnalysisBusy(value) {
  state.analysisBusy = value;
  els.analysisSubmitBtn.disabled = value;
  els.analysisInput.disabled = value;
  els.analysisState.textContent = value ? "分析中" : "";
}

async function submitAnalysis() {
  const text = els.analysisInput.value.trim();
  if (!text || state.analysisBusy) return;
  setAnalysisBusy(true);
  els.analysisOutput.textContent = "";
  try {
    const data = await fetchJson("/api/analyze", {
      method: "POST",
      body: JSON.stringify({
        text,
        session_id: "analysis",
      }),
    });
    els.analysisOutput.textContent = data.reply || "";
    els.analysisRecordPath.textContent = data.record_path || "records/analysis.txt";
    els.analysisDownloadLink.href = data.record_download_url || downloadUrl("records/analysis.txt");
    els.analysisState.textContent = `已保存到 ${data.record_path || "records/analysis.txt"}`;
    if (state.fileEntries.length > 0) {
      await loadFiles(state.filePath);
    }
  } catch (error) {
    els.analysisOutput.textContent = error.message;
    els.analysisState.textContent = "保存失败";
    setStatus("异常", "error");
  } finally {
    state.analysisBusy = false;
    els.analysisSubmitBtn.disabled = false;
    els.analysisInput.disabled = false;
  }
}

function formatDate(value) {
  if (!value) return "未保存";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

async function loadHealth() {
  try {
    const data = await fetchJson("/api/health");
    state.currentUser = data.user || state.currentUser;
    state.defaultCodingWorkspaceRoot = data.coding_workspace || "";
    els.workspaceLabel.textContent = `${state.currentUser.id} · ${state.currentUser.role}`;
    els.sidebarUserLabel.textContent = state.currentUser.id;
    els.workspacePath.textContent = data.workspace;
    for (const button of els.modeActions.querySelectorAll("[data-required-role]")) {
      button.hidden = button.dataset.requiredRole !== state.currentUser.role;
    }
    updateMetrics();
    setStatus("就绪", "ready");
  } catch (error) {
    setStatus("异常", "error");
    els.workspaceLabel.textContent = error.message;
    els.workspacePath.textContent = error.message;
  }
}

async function logout() {
  try {
    await fetchJson("/api/auth/logout", {
      method: "POST",
      body: JSON.stringify({}),
    });
  } finally {
    window.location.assign("/login");
  }
}

async function loadSessions() {
  const data = await fetchJson("/api/sessions");
  state.sessions = data.sessions || [];
  renderSessions();
}

async function loadSession(sessionId = state.sessionId, raw = state.rawSession) {
  const data = await fetchJson(
    `/api/session?session_id=${encodeURIComponent(sessionId)}&raw=${raw ? "1" : "0"}`,
  );
  const session = data.session || {};
  const channel = session.channel || (session.id?.startsWith("web:") ? "web" : "");
  state.sessionId = session.chat_id || sessionId;
  state.rawSession = raw || (channel !== "web" && channel !== "");
  state.currentMode = session.current_mode || "hybrid";
  els.sessionTitle.textContent = state.sessionId;
  updateMetrics(session);
  setBusy(false);
  renderMessages(session.messages || [], true);
  await loadSessions();
}

async function loadMemory() {
  const data = await fetchJson("/api/memory");
  state.memoryFiles = data.files || [];
  if (!state.memoryFiles.some((file) => file.name === state.activeMemory)) {
    state.activeMemory = state.memoryFiles[0]?.name || "";
  }
  renderMemory();
}

async function sendMessage(message) {
  setBusy(true);
  if (!els.messages.querySelector(".message") || els.messages.querySelector(".empty-state")) {
    els.messages.innerHTML = "";
  }
  renderOptimisticUserMessage(message);
  const streamingMessage = renderStreamingAssistantMessage();

  try {
    let data = null;
    await fetchJsonStream("/api/chat/stream", {
      method: "POST",
      body: JSON.stringify({
        session_id: state.sessionId,
        message,
        ...workspacePayload(),
      }),
    }, (event) => {
      if (event.type === "delta") {
        streamingMessage.body.textContent += event.text || "";
        scrollMessagesToBottom();
        return;
      }
      if (event.type === "error") {
        throw new Error(event.error || "流式请求失败");
      }
      if (event.type === "complete") {
        data = event;
      }
    });
    if (!data) {
      throw new Error("流式响应意外结束");
    }
    const savedMessages = data.session?.messages || [];
    state.currentMode = data.session?.current_mode || state.currentMode;
    renderMessages(savedMessages.length > 0
      ? savedMessages
      : [
        { role: "user", content: message },
        { role: "assistant", content: data.reply },
      ]);
    updateMetrics(data.session || {});
    await loadSessions();
  } catch (error) {
    const partial = streamingMessage.body.textContent.trim();
    streamingMessage.item.classList.remove("streaming");
    streamingMessage.item.classList.add("error");
    streamingMessage.body.textContent = partial
      ? `${partial}\n\n[请求中断：${error.message}]`
      : error.message;
    setStatus("异常", "error");
  } finally {
    streamingMessage.item.classList.remove("streaming");
    setBusy(false);
    await loadMemory();
  }
}

function submitComposer() {
  const message = els.input.value.trim();
  if (!message || state.busy || state.rawSession) return;
  els.input.value = "";
  els.input.style.height = "auto";
  sendMessage(message);
}

function renderOptimisticUserMessage(message) {
  const item = document.createElement("article");
  item.className = "message user";

  const role = document.createElement("div");
  role.className = "message-role";
  role.textContent = "你";

  const body = document.createElement("div");
  body.className = "message-body";
  body.textContent = message;

  item.append(role, body);
  els.messages.append(item);
  scrollMessagesToBottom(true);
}

function renderStreamingAssistantMessage() {
  const item = document.createElement("article");
  item.className = "message assistant streaming";

  const role = document.createElement("div");
  role.className = "message-role";
  role.textContent = "Agent";

  const body = document.createElement("div");
  body.className = "message-body";

  item.append(role, body);
  els.messages.append(item);
  scrollMessagesToBottom(true);
  return { item, body };
}

function newSession() {
  state.sessionId = `web-${Date.now().toString(36)}`;
  state.rawSession = false;
  state.currentMode = "hybrid";
  els.sessionTitle.textContent = state.sessionId;
  renderMessages([]);
  updateMetrics({ current_mode: "hybrid" });
  setBusy(false);
  setSidebarPanel("sessions");
  setMainView("chat");
  loadSessions();
  els.input.focus();
}

els.composer.addEventListener("submit", (event) => {
  event.preventDefault();
  submitComposer();
});

els.input.addEventListener("keydown", (event) => {
  if (event.isComposing) return;
  if (event.key !== "Enter") return;
  if (event.shiftKey) return;

  event.preventDefault();
  submitComposer();
});

els.input.addEventListener("input", function () {
  this.style.height = "auto";
  this.style.height = Math.min(this.scrollHeight, 180) + "px";
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && els.filePreviewModal.open) {
    event.preventDefault();
    closeFilePreviewModal();
    return;
  }

  if (event.key === "Escape" && state.mobileSidebarOpen) {
    event.preventDefault();
    setMobileSidebarOpen(false);
    return;
  }

  const modifier = event.ctrlKey || event.metaKey;
  if (!modifier || event.altKey || event.shiftKey) return;

  if (event.key.toLowerCase() === "k") {
    event.preventDefault();
    newSession();
    return;
  }

  if (event.key.toLowerCase() === "b") {
    event.preventDefault();
    if (isMobileViewport()) {
      setMobileSidebarOpen(!state.mobileSidebarOpen);
    } else {
      setSidebarCollapsed(!state.sidebarCollapsed);
    }
    return;
  }

  const modeCommands = {
    "1": "/hybrid",
    "2": "/chat",
    "3": "/coding",
  };
  const command = modeCommands[event.key];
  if (!command || state.busy || state.rawSession) return;
  if (command === "/coding" && state.currentUser.role !== "admin") return;
  event.preventDefault();
  sendMessage(command);
});

els.modeActions.addEventListener("click", (event) => {
  const command = event.target?.dataset?.command;
  if (!command || state.busy || state.rawSession) return;
  sendMessage(command);
});

els.sidebarTabs.forEach((tab) => {
  tab.addEventListener("click", () => {
    setSidebarPanel(tab.dataset.sidebarTab);
    setMainView(tab.dataset.mainView || "chat");
    if (isMobileViewport()) {
      setMobileSidebarOpen(false);
    }
  });
});

els.sessionSearch.addEventListener("input", () => {
  state.sessionFilter = els.sessionSearch.value;
  renderSessions();
});

els.sidebarToggle.addEventListener("click", () => {
  if (isMobileViewport()) {
    setMobileSidebarOpen(false);
  } else {
    setSidebarCollapsed(!state.sidebarCollapsed);
  }
});

els.mobileSidebarOpen.addEventListener("click", () => {
  setMobileSidebarOpen(true);
});

els.mobileSecondaryOpen.forEach((button) => {
  button.addEventListener("click", () => {
    setMobileSidebarOpen(true);
  });
});

els.sidebarOverlay.addEventListener("click", () => {
  setMobileSidebarOpen(false);
});

window.addEventListener("resize", syncResponsiveSidebar);

els.newSessionBtn.addEventListener("click", newSession);
els.stopBtn.addEventListener("click", () => {
  stopCurrentTurn();
});
els.logoutBtn.addEventListener("click", logout);
els.refreshMemoryBtn.addEventListener("click", loadMemory);
els.refreshFilesBtn.addEventListener("click", () => loadFiles().catch(showFileError));
els.fileUpBtn.addEventListener("click", () => loadFiles(state.fileParent).catch(showFileError));
els.fileUploadBtn.addEventListener("click", () => els.fileUploadInput.click());
els.fileUploadInput.addEventListener("change", uploadFiles);
els.fileMkdirBtn.addEventListener("click", () => makeDirectory().catch(showFileError));
els.filePreviewClose.addEventListener("click", closeFilePreviewModal);
els.filePreviewModal.addEventListener("click", (event) => {
  if (event.target === els.filePreviewModal) {
    closeFilePreviewModal();
  }
});
els.filePreviewModal.addEventListener("cancel", (event) => {
  event.preventDefault();
  closeFilePreviewModal();
});
els.analysisForm.addEventListener("submit", (event) => {
  event.preventDefault();
  submitAnalysis();
});
els.workspaceForm.addEventListener("submit", (event) => {
  event.preventDefault();
  setCodingWorkspaceRoot(els.workspaceInput.value);
  els.composerState.textContent = "工作路径已保存";
});
els.workspaceResetBtn.addEventListener("click", () => {
  resetCodingWorkspaceRoot();
  els.composerState.textContent = "已恢复默认工作路径";
});

async function init() {
  setSidebarPanel(state.sidebarPanel);
  setMainView(state.mainView);
  setSidebarCollapsed(state.sidebarCollapsed);
  syncResponsiveSidebar();
  await loadHealth();
  await Promise.all([loadSessions(), loadMemory(), loadFiles()]);
  await loadSession("default", false);
}

init().catch((error) => {
  setStatus("异常", "error");
  els.workspaceLabel.textContent = error.message;
  els.workspacePath.textContent = error.message;
});
