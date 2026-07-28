const CODING_WORKSPACE_STORAGE_KEY = "codingWorkspaceRoot";

const state = {
  sessionId: "default",
  rawSession: false,
  busy: false,
  sessions: [],
  memoryFiles: [],
  activeMemory: "MEMORY.md",
  sessionFilter: "",
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
  runs: [],
  activeRunId: "",
  runDetailCache: new Map(),
  runsBusy: false,
  runsLoaded: false,
  runsError: "",
  runStatusFilter: "all",
  logQuery: "",
  logLevel: "all",
  runtimeHealth: { status: "unknown", error: "" },
};

const els = {
  appShell: document.querySelector(".app-shell"),
  sidebarToggle: document.querySelector("#sidebarToggle"),
  sidebarOverlay: document.querySelector("#sidebarOverlay"),
  mobileSidebarOpen: document.querySelector("#mobileSidebarOpen"),
  mobileSecondaryOpen: [...document.querySelectorAll(".mobile-secondary-open")],
  sidebarTabs: [...document.querySelectorAll("[data-sidebar-tab]")],
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
  logsRefreshBtn: document.querySelector("#logsRefreshBtn"),
  logRunSelect: document.querySelector("#logRunSelect"),
  logSearch: document.querySelector("#logSearch"),
  logLevelFilter: document.querySelector("#logLevelFilter"),
  logsState: document.querySelector("#logsState"),
  logsTableBody: document.querySelector("#logsTableBody"),
  logTotalMetric: document.querySelector("#logTotalMetric"),
  logErrorMetric: document.querySelector("#logErrorMetric"),
  logWarningMetric: document.querySelector("#logWarningMetric"),
  runsRefreshBtn: document.querySelector("#runsRefreshBtn"),
  runStatusFilter: document.querySelector("#runStatusFilter"),
  runCountMetric: document.querySelector("#runCountMetric"),
  runsState: document.querySelector("#runsState"),
  runsList: document.querySelector("#runsList"),
  runDetail: document.querySelector("#runDetail"),
  settingsForm: document.querySelector("#settingsForm"),
  settingsState: document.querySelector("#settingsState"),
  runtimeHealthMetric: document.querySelector("#runtimeHealthMetric"),
  runtimeHealthMessage: document.querySelector("#runtimeHealthMessage"),
  statusRunCountMetric: document.querySelector("#statusRunCountMetric"),
  statusRefreshBtn: document.querySelector("#statusRefreshBtn"),
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

const ADMIN_VIEWS = new Set(["logs", "runs"]);

function isViewAllowed(viewName) {
  const exists = els.mainViewPanels.some(
    (panel) => panel.dataset.mainViewPanel === viewName,
  );
  if (!exists) return false;
  return !ADMIN_VIEWS.has(viewName) || state.currentUser.role === "admin";
}

function applyRoleVisibility() {
  for (const element of document.querySelectorAll("[data-required-role]")) {
    const allowed = element.dataset.requiredRole === state.currentUser.role;
    element.hidden = !allowed;
  }
  if (!isViewAllowed(state.mainView)) {
    setMainView("chat");
  }
}

function setMainView(viewName) {
  if (!isViewAllowed(viewName)) viewName = "chat";
  state.mainView = viewName;
  localStorage.setItem("mainView", viewName);

  for (const tab of els.sidebarTabs) {
    tab.classList.toggle("active", tab.dataset.mainView === viewName);
    tab.setAttribute("aria-current", tab.dataset.mainView === viewName ? "page" : "false");
  }
  for (const panel of els.mainViewPanels) {
    panel.classList.toggle("active", panel.dataset.mainViewPanel === viewName);
  }
  loadViewData(viewName);
}

function loadViewData(viewName) {
  if (viewName === "files" && state.fileEntries.length === 0) {
    loadFiles().catch(showFileError);
  } else if (viewName === "memory" && state.memoryFiles.length === 0) {
    loadMemory().catch((error) => renderViewError(els.memoryContent, error));
  } else if (viewName === "status") {
    loadRuntimeHealth();
    if (state.currentUser.role === "admin") loadRuns().catch(() => {});
  } else if (viewName === "logs" || viewName === "runs") {
    loadRuns().catch(() => {});
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

function createActivityTimeline() {
  const container = document.createElement("section");
  container.className = "activity-timeline";
  const activity = {
    container,
    startedAt: performance.now(),
    finishedAt: null,
    status: "running",
    currentStep: 0,
    maxStep: 0,
    toolCount: 0,
    tokenCount: 0,
    diffSummary: { created: 0, modified: 0, deleted: 0 },
    runId: "",
    steps: new Map(),
    stepOrder: [],
    tools: new Map(),
    toolOrder: [],
    subagents: new Map(),
    subagentOrder: [],
    timerId: null,
  };
  activity.timerId = window.setInterval(() => renderActivityTimeline(activity), 1000);
  renderActivityTimeline(activity);
  return activity;
}

function applyActivityEvent(activity, event) {
  if (!activity || !event || event.type !== "event") return;
  activity.runId = activity.runId || event.run_id || "";

  if (event.event === "reasoning.step.started" || event.event === "reasoning.step.completed") {
    const step = ensureActivityStep(activity, event.step, event);
    if (event.event === "reasoning.step.completed") {
      step.completed = true;
    }
    activity.currentStep = Math.max(activity.currentStep, step.number || 0);
    activity.maxStep = Math.max(activity.maxStep, step.number || 0);
  } else if (event.event === "tool.call.started") {
    const tool = ensureActivityTool(activity, event);
    tool.status = "running";
    tool.tool = event.tool || tool.tool;
    tool.args = event.args || tool.args;
  } else if (event.event === "tool.call.completed" || event.event === "tool.call.failed") {
    const tool = ensureActivityTool(activity, event);
    const terminalStatus = event.status || "success";
    tool.status = event.event.endsWith(".failed") || ["error", "denied"].includes(terminalStatus)
      ? "error"
      : terminalStatus;
    tool.durationMs = event.duration_ms;
    tool.preview = event.preview || tool.preview;
    tool.tool = event.tool || tool.tool;
    if (!tool.counted) {
      activity.toolCount += 1;
      tool.counted = true;
    }
  } else if (event.event === "model.call.completed") {
    activity.tokenCount += Number(event.tokens || 0);
  } else if (event.event === "subagent.started" || event.event === "subagent.completed") {
    const subagent = ensureActivitySubagent(activity, event);
    subagent.agentType = event.agent_type || subagent.agentType;
    subagent.description = event.description || subagent.description;
    if (event.event === "subagent.completed") {
      subagent.status = event.success === false ? "error" : "success";
      subagent.success = event.success;
      subagent.toolCount = Number(event.tool_count || subagent.toolCount || 0);
      subagent.reasoningSteps = Number(event.reasoning_steps || subagent.reasoningSteps || 0);
    }
  } else if (event.event === "workspace.diff.written") {
    const summary = event.summary || {};
    activity.diffSummary = {
      created: Number(summary.created || 0),
      modified: Number(summary.modified || 0),
      deleted: Number(summary.deleted || 0),
    };
  }

  renderActivityTimeline(activity);
}

function finalizeActivityTimeline(activity, status = "complete") {
  if (!activity) return;
  activity.status = status;
  activity.finishedAt = activity.finishedAt || performance.now();
  if (activity.timerId) {
    window.clearInterval(activity.timerId);
    activity.timerId = null;
  }
  renderActivityTimeline(activity, { compact: true });
}

function attachActivitySummaryToLastAssistant(activity) {
  if (!activity?.container) return;
  const bodies = [...els.messages.querySelectorAll(".message.assistant .message-body")];
  const body = bodies[bodies.length - 1];
  if (!body) return;
  body.prepend(activity.container);
  renderActivityTimeline(activity, { compact: true });
}

function ensureActivityStep(activity, number, event = {}) {
  const stepNumber = Number(number || 0);
  const spanId = event.span_id || `step:${stepNumber || activity.stepOrder.length + 1}`;
  if (!activity.steps.has(spanId)) {
    activity.steps.set(spanId, {
      spanId,
      parentSpanId: event.parent_span_id || "",
      number: stepNumber,
      completed: false,
      firstSeen: activity.stepOrder.length,
    });
    activity.stepOrder.push(spanId);
  }
  const step = activity.steps.get(spanId);
  step.number = step.number || stepNumber;
  step.parentSpanId = step.parentSpanId || event.parent_span_id || "";
  return step;
}

function ensureActivityTool(activity, event = {}) {
  const key = event.span_id || `tool:${activity.toolOrder.length + 1}`;
  if (!activity.tools.has(key)) {
    activity.tools.set(key, {
      spanId: key,
      parentSpanId: event.parent_span_id || "",
      step: Number(event.step || 0),
      tool: event.tool || "tool",
      args: event.args || "",
      preview: event.preview || "",
      durationMs: event.duration_ms,
      status: "running",
      counted: false,
      firstSeen: activity.toolOrder.length,
    });
    activity.toolOrder.push(key);
  }
  const tool = activity.tools.get(key);
  tool.parentSpanId = tool.parentSpanId || event.parent_span_id || "";
  tool.step = tool.step || Number(event.step || 0);
  return tool;
}

function ensureActivitySubagent(activity, event = {}) {
  const key = event.span_id || `subagent:${activity.subagentOrder.length + 1}`;
  if (!activity.subagents.has(key)) {
    activity.subagents.set(key, {
      spanId: key,
      parentSpanId: event.parent_span_id || "",
      agentType: event.agent_type || "subagent",
      description: event.description || "",
      status: "running",
      success: null,
      toolCount: Number(event.tool_count || 0),
      reasoningSteps: Number(event.reasoning_steps || 0),
      firstSeen: activity.subagentOrder.length,
    });
    activity.subagentOrder.push(key);
  }
  const subagent = activity.subagents.get(key);
  subagent.parentSpanId = subagent.parentSpanId || event.parent_span_id || "";
  return subagent;
}

function renderActivityTimeline(activity, options = {}) {
  const compact = Boolean(options.compact || activity.status !== "running");
  const container = activity.container;
  container.innerHTML = "";
  container.classList.toggle("activity-compact", compact);

  if (compact) {
    const details = document.createElement("details");
    details.className = "activity-disclosure";
    const summary = document.createElement("summary");
    summary.textContent = activitySummaryText(activity);
    details.append(summary, renderActivityBody(activity));
    container.append(details);
    return;
  }

  const status = document.createElement("div");
  status.className = "activity-status-line";
  status.textContent = activityStatusText(activity);
  container.append(status, renderActivityBody(activity));
}

function renderActivityBody(activity) {
  const body = document.createElement("div");
  body.className = "activity-body";

  const rootSteps = activity.stepOrder
    .map((id) => activity.steps.get(id))
    .filter((step) => step && !stepBelongsToSubagent(activity, step));
  for (const step of rootSteps) {
    body.append(renderActivityStep(activity, step));
  }

  const orphanTools = activity.toolOrder
    .map((id) => activity.tools.get(id))
    .filter((tool) => tool && !tool.parentSpanId && !tool.step);
  for (const tool of orphanTools) {
    body.append(renderActivityToolRow(activity, tool));
  }

  const orphanSubagents = activity.subagentOrder
    .map((id) => activity.subagents.get(id))
    .filter((subagent) => subagent && !subagent.parentSpanId);
  for (const subagent of orphanSubagents) {
    body.append(renderActivitySubagent(activity, subagent));
  }

  if (body.childElementCount === 0) {
    const empty = document.createElement("div");
    empty.className = "activity-empty";
    empty.textContent = "Starting run";
    body.append(empty);
  }

  const diffTotal = changedFileCount(activity.diffSummary);
  if (diffTotal > 0) {
    body.append(renderActivityDiff(activity));
  }
  return body;
}

function renderActivityStep(activity, step) {
  const details = document.createElement("details");
  details.className = "activity-step";
  details.open = activity.status === "running";

  const summary = document.createElement("summary");
  const label = document.createElement("span");
  label.textContent = `Step ${step.number || step.firstSeen + 1}`;
  const meta = document.createElement("code");
  const tools = toolsForStep(activity, step);
  meta.textContent = `${tools.length} tools`;
  summary.append(label, meta);
  details.append(summary);

  const rows = document.createElement("div");
  rows.className = "activity-step-rows";
  const usedSubagents = new Set();
  for (const tool of tools) {
    rows.append(renderActivityToolRow(activity, tool));
    for (const subagent of subagentsForParent(activity, tool.spanId)) {
      usedSubagents.add(subagent.spanId);
      rows.append(renderActivitySubagent(activity, subagent));
    }
  }
  for (const subagent of subagentsForParent(activity, step.spanId)) {
    if (!usedSubagents.has(subagent.spanId)) {
      rows.append(renderActivitySubagent(activity, subagent));
    }
  }
  details.append(rows);
  return details;
}

function renderActivityToolRow(activity, tool) {
  const row = document.createElement("div");
  row.className = `activity-tool-row ${tool.status || "running"}`;
  const icon = document.createElement("span");
  icon.className = "activity-tool-icon";
  icon.textContent = tool.status === "running" ? "●" : tool.status === "error" ? "✖" : "✔";

  const main = document.createElement("span");
  main.className = "activity-tool-main";
  main.textContent = toolLabel(tool);

  const meta = document.createElement("span");
  meta.className = "activity-tool-meta";
  meta.textContent = tool.status === "running"
    ? ""
    : [formatDurationMs(tool.durationMs), tool.status === "error" ? "error" : ""]
      .filter(Boolean)
      .join(" · ");

  row.append(icon, main, meta);
  return row;
}

function renderActivitySubagent(activity, subagent) {
  const details = document.createElement("details");
  details.className = `activity-subagent ${subagent.status || "running"}`;
  details.open = activity.status === "running" && subagent.status === "running";

  const summary = document.createElement("summary");
  const label = document.createElement("span");
  label.textContent = [subagent.agentType, subagent.description].filter(Boolean).join(" · ");
  const meta = document.createElement("code");
  const stepCount = subagent.reasoningSteps || stepsForSubagent(activity, subagent).length;
  const toolCount = subagent.toolCount || toolsForSubagent(activity, subagent).length;
  meta.textContent = `${stepCount} steps · ${toolCount} tools`;
  summary.append(label, meta);
  details.append(summary);

  const rows = document.createElement("div");
  rows.className = "activity-subagent-rows";
  for (const step of stepsForSubagent(activity, subagent)) {
    rows.append(renderActivityStep(activity, step));
  }
  if (rows.childElementCount === 0) {
    for (const tool of toolsForSubagent(activity, subagent)) {
      rows.append(renderActivityToolRow(activity, tool));
    }
  }
  details.append(rows);
  return details;
}

function renderActivityDiff(activity) {
  const wrap = document.createElement("div");
  wrap.className = "activity-diff";
  for (const [key, label] of [
    ["created", "created"],
    ["modified", "modified"],
    ["deleted", "deleted"],
  ]) {
    const count = Number(activity.diffSummary[key] || 0);
    if (!count) continue;
    const chip = document.createElement("span");
    chip.className = `activity-diff-chip ${key}`;
    chip.textContent = `${label} ${count}`;
    wrap.append(chip);
  }
  if (activity.runId) {
    const link = document.createElement("a");
    link.className = "activity-run-link";
    link.href = "#runs";
    link.textContent = `Run ${activity.runId}`;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      if (state.currentUser.role !== "admin") return;
      selectRun(activity.runId, { targetView: "runs" }).catch(() => {});
    });
    wrap.append(link);
  }
  return wrap;
}

function toolsForStep(activity, step) {
  return activity.toolOrder
    .map((id) => activity.tools.get(id))
    .filter((tool) => tool && (
      tool.parentSpanId === step.spanId
      || (!tool.parentSpanId && tool.step && tool.step === step.number)
    ));
}

function subagentsForParent(activity, parentSpanId) {
  return activity.subagentOrder
    .map((id) => activity.subagents.get(id))
    .filter((subagent) => subagent?.parentSpanId === parentSpanId);
}

function stepsForSubagent(activity, subagent) {
  return activity.stepOrder
    .map((id) => activity.steps.get(id))
    .filter((step) => step && (
      step.parentSpanId === subagent.spanId
      || step.spanId.startsWith(`${subagent.spanId}:step:`)
    ));
}

function toolsForSubagent(activity, subagent) {
  return activity.toolOrder
    .map((id) => activity.tools.get(id))
    .filter((tool) => tool && (
      tool.parentSpanId?.startsWith(`${subagent.spanId}:step:`)
      || tool.spanId.startsWith(`${subagent.spanId}:tool:`)
    ));
}

function stepBelongsToSubagent(activity, step) {
  return activity.subagentOrder.some((id) => {
    const subagent = activity.subagents.get(id);
    return subagent && (
      step.parentSpanId === subagent.spanId
      || step.spanId.startsWith(`${subagent.spanId}:step:`)
    );
  });
}

function toolLabel(tool) {
  const detail = toolDetail(tool);
  return detail ? `${tool.tool} ${detail}` : tool.tool;
}

function toolDetail(tool) {
  const args = parsePreviewArgs(tool.args);
  if (tool.tool === "bash" || tool.tool === "shell") {
    return args.command || args.cmd || tool.args || "";
  }
  if (tool.tool === "read_file") {
    return args.path || args.file || tool.args || "";
  }
  if (tool.tool === "write_file" || tool.tool === "edit_file") {
    return args.path || args.file || tool.args || "";
  }
  return args.path || args.command || args.cmd || tool.args || "";
}

function parsePreviewArgs(text) {
  const clean = String(text || "").replace(/\.\.\.\[truncated\]$/, "");
  if (!clean.trim().startsWith("{")) return {};
  try {
    const parsed = JSON.parse(clean);
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch (error) {
    return {};
  }
}

function activityStatusText(activity) {
  const parts = [];
  if (activity.currentStep) {
    parts.push(`Step ${activity.currentStep}`);
  } else {
    parts.push("Starting");
  }
  parts.push(`${activity.toolCount} tools`);
  parts.push(`${formatNumber(activity.tokenCount)} tokens`);
  parts.push(formatDurationMs(activityDurationMs(activity)));
  return parts.join(" · ");
}

function activitySummaryText(activity) {
  const files = changedFileCount(activity.diffSummary);
  return [
    `Worked ${formatDurationMs(activityDurationMs(activity))}`,
    `${activity.toolCount} tools`,
    `${files} files changed`,
  ].join(" · ");
}

function changedFileCount(summary) {
  return Number(summary.created || 0) + Number(summary.modified || 0) + Number(summary.deleted || 0);
}

function activityDurationMs(activity) {
  const end = activity.finishedAt || performance.now();
  return Math.max(0, end - activity.startedAt);
}

function formatDurationMs(ms) {
  if (ms === null || ms === undefined || ms === "") return "";
  const value = Number(ms);
  if (!Number.isFinite(value)) return "";
  if (value < 1000) return `${Math.max(1, Math.round(value))}ms`;
  return `${(value / 1000).toFixed(1)}s`;
}

function formatNumber(value) {
  const number = Number(value || 0);
  if (number >= 1000) {
    return `${Math.round(number / 100) / 10}k`;
  }
  return String(number);
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
    return !filter || `${session.title || ""} ${label} ${session.current_mode || ""}`.toLowerCase().includes(filter);
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
    name.textContent = session.title || label;

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
    applyRoleVisibility();
    updateMetrics();
    renderStatusDashboard();
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
  els.sessionTitle.textContent = session.title || state.sessionId;
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

function renderViewError(target, error) {
  if (!target) return;
  target.textContent = error?.message || String(error || "加载失败");
  target.classList.add("error");
}

async function loadRuns({ force = false } = {}) {
  if (state.currentUser.role !== "admin") return [];
  if (state.runsBusy) return state.runs;
  if (state.runsLoaded && !force) {
    renderRuns();
    if (state.activeRunId) await loadRunDetail(state.activeRunId);
    return state.runs;
  }

  state.runsBusy = true;
  state.runsError = "";
  renderRuns();
  try {
    const data = await fetchJson("/api/runs");
    state.runs = Array.isArray(data.runs) ? data.runs : [];
    state.runsLoaded = true;
    if (!state.runs.some((run) => run.run_id === state.activeRunId)) {
      state.activeRunId = state.runs[0]?.run_id || "";
    }
    if (force) state.runDetailCache.clear();
    renderRuns();
    if (state.activeRunId) await loadRunDetail(state.activeRunId, { force });
    return state.runs;
  } catch (error) {
    state.runsError = error.message;
    renderRuns();
    throw error;
  } finally {
    state.runsBusy = false;
    renderRuns();
  }
}

async function selectRun(runId, { targetView = "", force = false } = {}) {
  if (!runId) return;
  state.activeRunId = runId;
  if (targetView) setMainView(targetView);
  renderRuns();
  await loadRunDetail(runId, { force });
}

async function loadRunDetail(runId, { force = false } = {}) {
  if (!runId || state.currentUser.role !== "admin") return null;
  if (!force && state.runDetailCache.has(runId)) {
    const cached = state.runDetailCache.get(runId);
    renderRunDetail(cached);
    renderLogs(cached);
    return cached;
  }

  setRunPageState("正在加载 Trace…");
  try {
    const detail = await fetchJson(`/api/run?run_id=${encodeURIComponent(runId)}`);
    state.runDetailCache.set(runId, detail);
    renderRunDetail(detail);
    renderLogs(detail);
    return detail;
  } catch (error) {
    setRunPageState(error.message, "error");
    renderRunDetail(null, error);
    renderLogs(null, error);
    throw error;
  }
}

function setRunPageState(message = "", kind = "") {
  for (const target of [els.runsState, els.logsState]) {
    if (!target) continue;
    target.textContent = message;
    target.className = `inline-state ${kind}`.trim();
  }
}

function filteredRuns() {
  if (state.runStatusFilter === "all") return state.runs;
  return state.runs.filter((run) => run.status === state.runStatusFilter);
}

function renderRuns() {
  if (!els.runsList || !els.logRunSelect) return;
  els.runCountMetric.textContent = String(state.runs.length);
  els.statusRunCountMetric.textContent = state.currentUser.role === "admin"
    ? String(state.runs.length)
    : "无权限";

  els.logRunSelect.innerHTML = "";
  for (const run of state.runs) {
    const option = document.createElement("option");
    option.value = run.run_id || "";
    option.textContent = `${run.run_id || "unknown"} · ${run.status || "unknown"}`;
    option.selected = run.run_id === state.activeRunId;
    els.logRunSelect.append(option);
  }
  els.logRunSelect.disabled = state.runsBusy || state.runs.length === 0;

  els.runsList.innerHTML = "";
  const rows = filteredRuns();
  if (state.runsError) {
    setRunPageState(state.runsError, "error");
  } else if (state.runsBusy && state.runs.length === 0) {
    setRunPageState("正在加载 Runs…");
  } else if (rows.length === 0) {
    setRunPageState(state.runs.length ? "没有匹配状态的 Run" : "暂无 Run 数据");
  } else {
    setRunPageState("");
  }

  for (const run of rows) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `run-list-item ${run.run_id === state.activeRunId ? "active" : ""}`;
    button.setAttribute("aria-pressed", run.run_id === state.activeRunId ? "true" : "false");

    const head = document.createElement("span");
    head.className = "run-list-head";
    const id = document.createElement("strong");
    id.textContent = run.run_id || "unknown run";
    const badge = document.createElement("span");
    badge.className = `run-status ${statusClass(run.status)}`;
    badge.textContent = run.status || "unknown";
    head.append(id, badge);

    const meta = document.createElement("span");
    meta.className = "run-list-meta";
    meta.textContent = `${run.mode || "-"} · ${formatDate(run.started_at)}`;
    const counts = document.createElement("span");
    counts.className = "run-list-counts";
    counts.textContent = `${Number(run.reasoning_steps || 0)} steps · ${Number(run.model_calls || 0)} models · ${Number(run.tool_calls || 0)} tools`;
    button.append(head, meta, counts);
    button.addEventListener("click", () => selectRun(run.run_id).catch(() => {}));
    els.runsList.append(button);
  }
}

function statusClass(status) {
  const value = String(status || "unknown").toLowerCase();
  if (["completed", "success", "ready", "ok"].includes(value)) return "success";
  if (["failed", "error"].includes(value)) return "error";
  if (["stopped", "cancelled", "incomplete"].includes(value)) return "warning";
  if (value === "running") return "running";
  return "neutral";
}

function inferLogLevel(event = {}) {
  const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
  const haystack = `${event.event || ""} ${payload.status || ""} ${payload.stop_reason || ""}`.toLowerCase();
  if (/failed|error|denied|exception|fatal/.test(haystack)) return "error";
  if (/warn|retry|stopped|cancel|truncat|compress|guard|cooldown|fallback/.test(haystack)) return "warn";
  return "info";
}

function summarizeTraceEvent(event = {}) {
  const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
  const preferred = [
    payload.error_message,
    payload.error_preview,
    payload.output_preview,
    payload.content_preview,
    payload.summary_preview,
    payload.reason,
    payload.stop_reason,
    payload.description,
    payload.status,
  ].find((value) => value !== undefined && value !== null && String(value).trim());
  let text = preferred ? String(preferred) : "";
  if (!text) {
    try {
      text = Object.keys(payload).length ? JSON.stringify(payload) : String(event.event || "event");
    } catch (error) {
      text = String(event.event || "event");
    }
  }
  text = text.replace(/\s+/g, " ").trim();
  return text.length > 240 ? `${text.slice(0, 237)}…` : text;
}

function traceEventToLogRow(event = {}) {
  const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
  return {
    timestamp: String(event.timestamp || ""),
    level: inferLogLevel(event),
    source: String(payload.tool_name || payload.model || event.event || "runtime"),
    message: summarizeTraceEvent(event),
    event: String(event.event || "unknown.event"),
    step: event.step ?? payload.step ?? "",
    payload,
  };
}

function filteredLogRows(detail) {
  const rows = (Array.isArray(detail?.events) ? detail.events : []).map(traceEventToLogRow);
  const query = state.logQuery.trim().toLowerCase();
  return rows.filter((row) => {
    if (state.logLevel !== "all" && row.level !== state.logLevel) return false;
    if (!query) return true;
    return `${row.event} ${row.source} ${row.message}`.toLowerCase().includes(query);
  });
}

function renderLogs(detail, error = null) {
  if (!els.logsTableBody) return;
  els.logsTableBody.innerHTML = "";
  const allRows = (Array.isArray(detail?.events) ? detail.events : []).map(traceEventToLogRow);
  const rows = filteredLogRows(detail);
  els.logTotalMetric.textContent = String(allRows.length);
  els.logErrorMetric.textContent = String(allRows.filter((row) => row.level === "error").length);
  els.logWarningMetric.textContent = String(allRows.filter((row) => row.level === "warn").length);

  if (error) {
    setRunPageState(error.message || "日志加载失败", "error");
    return;
  }
  if (rows.length === 0) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "table-empty";
    cell.textContent = allRows.length ? "没有匹配筛选条件的事件" : "当前 Run 没有 Trace 事件";
    row.append(cell);
    els.logsTableBody.append(row);
    return;
  }

  for (const log of rows) {
    const row = document.createElement("tr");
    const time = document.createElement("td");
    time.className = "log-time";
    time.textContent = formatTraceTime(log.timestamp);
    const level = document.createElement("td");
    const levelBadge = document.createElement("span");
    levelBadge.className = `log-level ${log.level}`;
    levelBadge.textContent = log.level.toUpperCase();
    level.append(levelBadge);
    const source = document.createElement("td");
    source.className = "log-source";
    source.textContent = log.source;
    const message = document.createElement("td");
    message.className = "log-message";
    message.textContent = log.message;
    const detailCell = document.createElement("td");
    const disclosure = document.createElement("details");
    disclosure.className = "payload-disclosure";
    const summary = document.createElement("summary");
    summary.textContent = log.step === "" ? log.event : `Step ${log.step}`;
    const pre = document.createElement("pre");
    pre.textContent = safeJson(log.payload);
    disclosure.append(summary, pre);
    detailCell.append(disclosure);
    row.append(time, level, source, message, detailCell);
    els.logsTableBody.append(row);
  }
}

function formatTraceTime(value) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString("zh-CN", { hour12: false });
}

function safeJson(value) {
  try {
    return JSON.stringify(value ?? {}, null, 2);
  } catch (error) {
    return String(value ?? "");
  }
}

function groupTraceSteps(detail) {
  const groups = new Map();
  const runEvents = [];
  for (const event of Array.isArray(detail?.events) ? detail.events : []) {
    if (String(event.session_id || "").startsWith("subtask:")) continue;
    const payload = event.payload && typeof event.payload === "object" ? event.payload : {};
    const number = Number(event.step ?? payload.step ?? 0);
    if (!number) {
      runEvents.push(event);
      continue;
    }
    if (!groups.has(number)) groups.set(number, { number, events: [] });
    groups.get(number).events.push(event);
  }
  return { steps: [...groups.values()].sort((a, b) => a.number - b.number), runEvents };
}

function renderRunDetail(detail, error = null) {
  if (!els.runDetail) return;
  els.runDetail.innerHTML = "";
  if (error || !detail) {
    els.runDetail.append(makeEmptyPanel(error ? "Trace 加载失败" : "选择一个 Run", error?.message || "查看真实执行指标与时间线。"));
    return;
  }

  const runState = detail.run_state && typeof detail.run_state === "object" ? detail.run_state : {};
  const metrics = detail.metrics && typeof detail.metrics === "object" ? detail.metrics : {};
  const header = document.createElement("header");
  header.className = "run-detail-header";
  const titleWrap = document.createElement("div");
  const title = document.createElement("h3");
  title.textContent = detail.run_id || state.activeRunId || "Run";
  const meta = document.createElement("p");
  meta.textContent = `${runState.mode || "-"} · ${formatTraceTime(runState.started_at)}`;
  titleWrap.append(title, meta);
  const status = document.createElement("span");
  status.className = `run-status large ${statusClass(runState.status)}`;
  status.textContent = runState.status || "unknown";
  header.append(titleWrap, status);

  const metricsGrid = document.createElement("div");
  metricsGrid.className = "trace-metrics";
  const metricItems = [
    ["步骤", runState.reasoning_steps ?? metrics.reasoning_steps],
    ["模型调用", metrics.model_calls],
    ["工具调用", metrics.tool_calls],
    ["Tokens", metrics.total_tokens],
    ["耗时", formatDurationMs(metrics.run_duration_ms)],
    ["Subagents", Array.isArray(detail.subagents) ? detail.subagents.length : 0],
  ];
  for (const [label, rawValue] of metricItems) {
    const card = document.createElement("div");
    const caption = document.createElement("span");
    caption.textContent = label;
    const value = document.createElement("strong");
    value.textContent = rawValue === undefined || rawValue === null || rawValue === ""
      ? "不适用"
      : String(rawValue);
    card.append(caption, value);
    metricsGrid.append(card);
  }

  const requestEvent = (detail.events || []).find((event) => event.event === "inbound_received");
  const request = requestEvent?.payload?.content_preview || runState.metadata?.request_preview || "未记录请求摘要";
  const requestPanel = makeTraceSection("用户请求");
  const requestText = document.createElement("p");
  requestText.className = "request-preview";
  requestText.textContent = request;
  requestPanel.append(requestText);

  const timelineSection = makeTraceSection("执行时间线");
  const timeline = document.createElement("div");
  timeline.className = "trace-timeline";
  const grouped = groupTraceSteps(detail);
  if (grouped.steps.length === 0) {
    timeline.append(makeEmptyPanel("没有 Step 事件", "Run 事件仍可在下方查看。"));
  }
  for (const step of grouped.steps) timeline.append(renderTraceStep(step));
  timelineSection.append(timeline);

  const subagents = makeTraceSection("Subagents");
  subagents.append(renderSubagents(detail.subagents));

  const runEventsSection = makeTraceSection("Run 事件");
  const runEvents = document.createElement("div");
  runEvents.className = "trace-event-list";
  for (const event of grouped.runEvents) runEvents.append(renderTraceEvent(event));
  if (!grouped.runEvents.length) runEvents.append(makeEmptyPanel("没有独立 Run 事件", "所有事件均已归入步骤。"));
  runEventsSection.append(runEvents);

  const stateSection = makeTraceSection("Run State");
  const stateDetails = document.createElement("details");
  stateDetails.className = "json-panel";
  const stateSummary = document.createElement("summary");
  stateSummary.textContent = "查看完整状态 JSON";
  const statePre = document.createElement("pre");
  statePre.textContent = safeJson(runState);
  stateDetails.append(stateSummary, statePre);
  stateSection.append(stateDetails);

  const answerSection = makeTraceSection("最终结果");
  const answer = document.createElement("pre");
  answer.className = "final-answer-preview";
  answer.textContent = runState.final_answer || runState.error || runState.stop_reason || "不适用";
  answerSection.append(answer);
  els.runDetail.append(header, metricsGrid, requestPanel, timelineSection, subagents, runEventsSection, stateSection, answerSection);
}

function makeTraceSection(titleText) {
  const section = document.createElement("section");
  section.className = "trace-section";
  const title = document.createElement("h4");
  title.textContent = titleText;
  section.append(title);
  return section;
}

function renderTraceStep(step) {
  const wrapper = document.createElement("section");
  wrapper.className = "trace-step";
  const marker = document.createElement("span");
  marker.className = "trace-step-marker";
  marker.textContent = String(step.number);
  const card = document.createElement("div");
  card.className = "trace-step-card";
  const heading = document.createElement("h5");
  heading.textContent = `Step ${step.number} · Context / Model / Tools`;
  const events = document.createElement("div");
  events.className = "trace-event-list";
  for (const event of step.events) events.append(renderTraceEvent(event));
  card.append(heading, events);
  wrapper.append(marker, card);
  return wrapper;
}

function renderTraceEvent(event) {
  const details = document.createElement("details");
  details.className = `trace-event ${inferLogLevel(event)}`;
  const summary = document.createElement("summary");
  const name = document.createElement("strong");
  name.textContent = event.event || "unknown.event";
  const preview = document.createElement("span");
  preview.textContent = summarizeTraceEvent(event);
  const time = document.createElement("small");
  time.textContent = formatDurationMs(event.payload?.duration_ms) || formatTraceTime(event.timestamp);
  summary.append(name, preview, time);
  const pre = document.createElement("pre");
  pre.textContent = safeJson(event.payload);
  details.append(summary, pre);
  return details;
}

function renderSubagents(items) {
  const container = document.createElement("div");
  container.className = "subagent-grid";
  if (!Array.isArray(items) || items.length === 0) {
    container.append(makeEmptyPanel("没有 Subagent", "此 Run 未记录子任务执行。"));
    return container;
  }
  for (const item of items) {
    const details = document.createElement("details");
    details.className = "subagent-card";
    const summary = document.createElement("summary");
    const title = document.createElement("strong");
    title.textContent = item.description || item.agent_type || item.session_id || "Subagent";
    const status = document.createElement("span");
    status.className = `run-status ${item.success === true ? "success" : item.success === false ? "error" : "warning"}`;
    status.textContent = item.success === true ? "success" : item.success === false ? "failed" : "incomplete";
    summary.append(title, status);
    const pre = document.createElement("pre");
    pre.textContent = safeJson(item);
    details.append(summary, pre);
    container.append(details);
  }
  return container;
}

function makeEmptyPanel(titleText, bodyText) {
  const panel = document.createElement("div");
  panel.className = "run-detail-empty compact";
  const title = document.createElement("h3");
  title.textContent = titleText;
  const body = document.createElement("p");
  body.textContent = bodyText;
  panel.append(title, body);
  return panel;
}

async function loadRuntimeHealth() {
  state.runtimeHealth = { status: "loading", error: "" };
  renderStatusDashboard();
  try {
    const data = await fetchJson("/api/runtime-health");
    state.runtimeHealth = {
      status: data.ok ? "ready" : "error",
      error: data.error || "",
    };
  } catch (error) {
    state.runtimeHealth = { status: "error", error: error.message };
  }
  renderStatusDashboard();
}

function renderStatusDashboard() {
  if (!els.runtimeHealthMetric) return;
  const labels = { loading: "检查中", ready: "就绪", error: "异常", unknown: "未知" };
  const status = state.runtimeHealth.status || "unknown";
  els.runtimeHealthMetric.textContent = labels[status] || "未知";
  els.runtimeHealthMetric.className = status;
  els.runtimeHealthMessage.textContent = state.runtimeHealth.error
    || (status === "ready" ? "Runtime 已完成初始化并可接受请求。" : "尚未完成 Runtime 健康检查。");
  els.statusRunCountMetric.textContent = state.currentUser.role === "admin"
    ? String(state.runs.length)
    : "无权限";
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
        streamingMessage.content.textContent += event.text || "";
        scrollMessagesToBottom();
        return;
      }
      if (event.type === "event") {
        applyActivityEvent(streamingMessage.activity, event);
        scrollMessagesToBottom();
        return;
      }
      if (event.type === "error") {
        finalizeActivityTimeline(streamingMessage.activity, "error");
        throw new Error(event.error || "流式请求失败");
      }
      if (event.type === "complete") {
        data = event;
        finalizeActivityTimeline(streamingMessage.activity, "complete");
      }
    });
    if (!data) {
      throw new Error("流式响应意外结束");
    }
    const savedMessages = data.session?.messages || [];
    state.currentMode = data.session?.current_mode || state.currentMode;
    els.sessionTitle.textContent = data.session?.title || state.sessionId;
    renderMessages(savedMessages.length > 0
      ? savedMessages
      : [
        { role: "user", content: message },
        { role: "assistant", content: data.reply },
      ]);
    attachActivitySummaryToLastAssistant(streamingMessage.activity);
    updateMetrics(data.session || {});
    await loadSessions();
  } catch (error) {
    finalizeActivityTimeline(streamingMessage.activity, "error");
    const partial = streamingMessage.content.textContent.trim();
    streamingMessage.item.classList.remove("streaming");
    streamingMessage.item.classList.add("error");
    streamingMessage.content.textContent = partial
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
  const activity = createActivityTimeline();
  const content = document.createElement("div");
  content.className = "stream-content";
  body.append(activity.container, content);

  item.append(role, body);
  els.messages.append(item);
  scrollMessagesToBottom(true);
  return { item, body, content, activity };
}

function newSession() {
  state.sessionId = `web-${Date.now().toString(36)}`;
  state.rawSession = false;
  state.currentMode = "hybrid";
  els.sessionTitle.textContent = state.sessionId;
  renderMessages([]);
  updateMetrics({ current_mode: "hybrid" });
  setBusy(false);
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

els.logsRefreshBtn?.addEventListener("click", () => {
  loadRuns({ force: true }).catch(() => {});
});
els.logRunSelect?.addEventListener("change", () => {
  selectRun(els.logRunSelect.value).catch(() => {});
});
els.logSearch?.addEventListener("input", () => {
  state.logQuery = els.logSearch.value;
  renderLogs(state.runDetailCache.get(state.activeRunId));
});
els.logLevelFilter?.addEventListener("change", () => {
  state.logLevel = els.logLevelFilter.value;
  renderLogs(state.runDetailCache.get(state.activeRunId));
});
els.runStatusFilter?.addEventListener("change", () => {
  state.runStatusFilter = els.runStatusFilter.value;
  renderRuns();
});
els.runsRefreshBtn?.addEventListener("click", () => {
  loadRuns({ force: true }).catch(() => {});
});
els.statusRefreshBtn?.addEventListener("click", async () => {
  await Promise.allSettled([
    loadHealth(),
    loadRuntimeHealth(),
    state.currentUser.role === "admin" ? loadRuns({ force: true }) : Promise.resolve(),
  ]);
});
els.settingsForm?.addEventListener("submit", (event) => {
  event.preventDefault();
  els.settingsState.textContent = "预览模式：尚未接入配置保存，未发送任何请求。";
});

async function init() {
  setSidebarCollapsed(state.sidebarCollapsed);
  syncResponsiveSidebar();
  await loadHealth();
  setMainView(state.mainView);
  await Promise.all([loadSessions(), loadMemory(), loadFiles()]);
  await loadSession("default", false);
}

init().catch((error) => {
  setStatus("异常", "error");
  els.workspaceLabel.textContent = error.message;
  els.workspacePath.textContent = error.message;
});
