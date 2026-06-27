(function () {
  const vscode = acquireVsCodeApi();
  const savedState = vscode.getState() || {};
  const state = {
    expanded: Boolean(savedState.expanded),
    tasks: Array.isArray(savedState.tasks) ? savedState.tasks : []
  };
  const starterTasks = [
    { title: "构建可配置测评脚本", age: "14h", starter: true },
    { title: "查明卡住原因", age: "11h", starter: true },
    { title: "检查RAG测评机制", age: "2d", starter: true }
  ];
  let runningTask = "";

  const els = {
    composer: document.getElementById("composer"),
    task: document.getElementById("task"),
    newTask: document.getElementById("newTask"),
    run: document.getElementById("run"),
    cancel: document.getElementById("cancel"),
    status: document.getElementById("status"),
    mode: document.getElementById("mode"),
    maxReasoningSteps: document.getElementById("maxReasoningSteps"),
    subagentMaxReasoningSteps: document.getElementById("subagentMaxReasoningSteps"),
    ragEnabled: document.getElementById("ragEnabled"),
    contextBudgetEnabled: document.getElementById("contextBudgetEnabled"),
    workingMemoryEnabled: document.getElementById("workingMemoryEnabled"),
    toolLoopGuardEnabled: document.getElementById("toolLoopGuardEnabled"),
    settings: document.getElementById("settings"),
    settingsPanel: document.getElementById("settingsPanel"),
    accessLabel: document.getElementById("accessLabel"),
    contextToggle: document.getElementById("contextToggle"),
    workspaceMode: document.getElementById("workspaceMode"),
    result: document.getElementById("result"),
    taskList: document.getElementById("taskList"),
    taskCount: document.getElementById("taskCount"),
    viewAll: document.getElementById("viewAll")
  };

  els.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    runCurrentTask();
  });
  els.cancel.addEventListener("click", () => {
    vscode.postMessage({ type: "cancelTask" });
  });
  els.newTask.addEventListener("click", composeTask);
  els.settings.addEventListener("click", toggleSettings);
  els.contextToggle.addEventListener("click", () => {
    els.workingMemoryEnabled.checked = !els.workingMemoryEnabled.checked;
    syncPills();
  });
  els.workspaceMode.addEventListener("click", () => {
    vscode.postMessage({ type: "openSettings" });
  });
  els.viewAll.addEventListener("click", () => {
    state.expanded = !state.expanded;
    saveState();
    renderTasks();
  });
  els.task.addEventListener("input", autoSizeTaskInput);
  els.task.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      runCurrentTask();
    }
  });

  for (const input of [
    els.mode,
    els.maxReasoningSteps,
    els.subagentMaxReasoningSteps,
    els.ragEnabled,
    els.contextBudgetEnabled,
    els.workingMemoryEnabled,
    els.toolLoopGuardEnabled
  ]) {
    input.addEventListener("change", syncPills);
  }

  window.addEventListener("message", (event) => {
    const message = event.data || {};
    if (message.type === "state") {
      applyConfig(message.config || {});
      setRunning(Boolean(message.running));
      if (message.lastResult) {
        renderResult(message.lastResult);
      } else if (!message.running) {
        renderEmptyStage();
      }
    } else if (message.type === "running") {
      runningTask = String(message.task || "").trim();
      setRunning(true);
      if (runningTask) {
        upsertTask(runningTask, { status: "running" });
      }
      renderRunning(message);
    } else if (message.type === "result") {
      setRunning(false);
      if (runningTask) {
        upsertTask(runningTask, {
          status: (message.result && message.result.status) || "done",
          durationMs: durationFromResult(message.result || {})
        });
      }
      runningTask = "";
      renderResult(message.result || {});
    } else if (message.type === "cancelled") {
      setRunning(false);
      if (runningTask) {
        upsertTask(runningTask, { status: "cancelled" });
      }
      runningTask = "";
      renderEmptyStage();
      setStatus("Cancelled");
    } else if (message.type === "compose") {
      composeTask();
    }
  });

  els.settings.setAttribute("aria-expanded", "false");
  renderTasks();
  renderEmptyStage();
  syncPills();
  autoSizeTaskInput();
  vscode.postMessage({ type: "ready" });

  function runCurrentTask() {
    const task = els.task.value.trim();
    if (!task || els.run.disabled) {
      els.task.focus();
      return;
    }
    vscode.postMessage({ type: "runTask", payload: collectPayload() });
  }

  function collectPayload() {
    return {
      task: els.task.value,
      mode: els.mode.value,
      maxReasoningSteps: numberValue(els.maxReasoningSteps, 24),
      subagentMaxReasoningSteps: numberValue(els.subagentMaxReasoningSteps, 16),
      ragEnabled: els.ragEnabled.checked,
      contextBudgetEnabled: els.contextBudgetEnabled.checked,
      workingMemoryEnabled: els.workingMemoryEnabled.checked,
      toolLoopGuardEnabled: els.toolLoopGuardEnabled.checked
    };
  }

  function applyConfig(config) {
    els.mode.value = config.mode || "coding";
    els.maxReasoningSteps.value = config.maxReasoningSteps || 24;
    els.subagentMaxReasoningSteps.value = config.subagentMaxReasoningSteps || 16;
    els.ragEnabled.checked = Boolean(config.ragEnabled);
    els.contextBudgetEnabled.checked = config.contextBudgetEnabled !== false;
    els.workingMemoryEnabled.checked = config.workingMemoryEnabled !== false;
    els.toolLoopGuardEnabled.checked = config.toolLoopGuardEnabled !== false;
    syncPills();
  }

  function setRunning(running) {
    els.run.disabled = running;
    els.cancel.disabled = !running;
    els.run.hidden = running;
    els.cancel.hidden = !running;
    setStatus(running ? "Running" : "Idle", running ? "active" : "");
  }

  function renderTasks() {
    const source = state.tasks.length ? state.tasks : starterTasks;
    const visible = state.expanded ? source : source.slice(0, 3);
    els.taskCount.textContent = String(source.length);
    els.viewAll.hidden = false;
    els.viewAll.textContent = state.expanded ? "Show less" : `View all (${source.length})`;
    els.taskList.innerHTML = visible.map((task, index) => taskItem(task, index)).join("");
    for (const button of els.taskList.querySelectorAll("[data-task-index]")) {
      button.addEventListener("click", () => {
        const task = visible[Number(button.getAttribute("data-task-index"))];
        if (!task) {
          return;
        }
        els.task.value = task.title || "";
        autoSizeTaskInput();
        els.task.focus();
      });
    }
  }

  function taskItem(task, index) {
    const meta = task.status === "running" ? "now" : task.age || formatAge(task.updatedAt);
    return `
      <button class="task-item" type="button" data-task-index="${index}">
        <span class="task-title">${escapeHtml(task.title || "Untitled task")}</span>
        <span class="task-time">${escapeHtml(meta)}</span>
      </button>
    `;
  }

  function renderEmptyStage() {
    els.result.className = "stage empty";
    els.result.innerHTML = `<div class="agent-mark" aria-hidden="true"></div>`;
  }

  function renderRunning(message) {
    const task = message.task || runningTask || "Running task";
    els.result.className = "stage busy";
    els.result.innerHTML = `
      <div class="agent-mark active-mark" aria-hidden="true"></div>
      <div class="stage-copy">
        <strong>Running</strong>
        <span>${escapeHtml(task)}</span>
      </div>
    `;
  }

  function renderResult(result) {
    const metrics = result.metrics || {};
    const diff = result.workspace_diff || {};
    const status = result.status || "unknown";
    const duration = durationFromResult(result);
    setStatus(status, status === "error" ? "error" : "");
    els.result.className = "stage result-stage";
    els.result.innerHTML = `
      <div class="result-panel">
        <div class="result-head">
          <strong>${escapeHtml(status)}</strong>
          <span>${formatMs(duration)}</span>
        </div>
        <div class="metric-line">
          <span>tokens ${value(metrics.total_tokens)}</span>
          <span>steps ${value(metrics.reasoning_steps)}</span>
          <span>tools ${value(metrics.tool_calls)}</span>
          <span>models ${value(metrics.model_calls)}</span>
        </div>
        <div class="metric-line">
          <span>created ${value(diff.created_count)}</span>
          <span>modified ${value(diff.modified_count)}</span>
          <span>deleted ${value(diff.deleted_count)}</span>
        </div>
        ${result.error ? `<pre class="error">${escapeHtml(result.error)}</pre>` : ""}
        ${result.reply ? `<pre class="reply">${escapeHtml(result.reply)}</pre>` : ""}
        <div class="links">
          ${pathButton("Trace", result.trace_summary)}
          ${pathButton("Run Dir", result.run_dir)}
        </div>
      </div>
    `;
    for (const button of els.result.querySelectorAll("[data-path]")) {
      button.addEventListener("click", () => {
        vscode.postMessage({ type: "openPath", path: button.getAttribute("data-path") });
      });
    }
  }

  function upsertTask(title, patch) {
    const cleanTitle = String(title || "").trim();
    if (!cleanTitle) {
      return;
    }
    const existing = state.tasks.find((task) => task.title === cleanTitle) || {};
    const next = {
      ...existing,
      title: cleanTitle,
      updatedAt: Date.now(),
      ...patch
    };
    state.tasks = [
      next,
      ...state.tasks.filter((task) => task.title !== cleanTitle)
    ].slice(0, 50);
    saveState();
    renderTasks();
  }

  function saveState() {
    vscode.setState({ expanded: state.expanded, tasks: state.tasks });
  }

  function composeTask() {
    els.task.value = "";
    autoSizeTaskInput();
    els.task.focus();
  }

  function toggleSettings() {
    const willOpen = els.settingsPanel.hasAttribute("hidden");
    if (willOpen) {
      els.settingsPanel.removeAttribute("hidden");
    } else {
      els.settingsPanel.setAttribute("hidden", "");
    }
    els.settings.setAttribute("aria-expanded", String(willOpen));
  }

  function syncPills() {
    const fullAccess = els.contextBudgetEnabled.checked && els.workingMemoryEnabled.checked && els.toolLoopGuardEnabled.checked;
    els.accessLabel.textContent = fullAccess ? "Full access" : "Limited";
    els.contextToggle.className = els.workingMemoryEnabled.checked ? "context-button active" : "context-button";
  }

  function setStatus(text, tone) {
    els.status.textContent = text;
    els.status.className = tone ? `status ${tone}` : "status";
  }

  function autoSizeTaskInput() {
    els.task.style.height = "auto";
    const nextHeight = Math.max(24, Math.min(96, els.task.scrollHeight));
    els.task.style.height = `${nextHeight}px`;
  }

  function pathButton(label, target) {
    if (!target) {
      return "";
    }
    return `<button class="result-link" type="button" data-path="${escapeHtml(target)}">${escapeHtml(label)}</button>`;
  }

  function durationFromResult(result) {
    const metrics = result.metrics || {};
    return result.duration_ms || metrics.run_duration_ms || 0;
  }

  function numberValue(input, fallback) {
    const parsed = Number.parseInt(input.value, 10);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
  }

  function value(item) {
    return item === undefined || item === null ? "0" : String(item);
  }

  function formatAge(timestamp) {
    const value = Number(timestamp || 0);
    if (!value) {
      return "";
    }
    const minutes = Math.max(0, Math.floor((Date.now() - value) / 60000));
    if (minutes < 1) {
      return "now";
    }
    if (minutes < 60) {
      return `${minutes}m`;
    }
    if (minutes < 1440) {
      return `${Math.floor(minutes / 60)}h`;
    }
    return `${Math.floor(minutes / 1440)}d`;
  }

  function formatMs(ms) {
    const value = Number(ms || 0);
    if (value >= 1000) {
      return `${(value / 1000).toFixed(1)}s`;
    }
    return `${Math.round(value)}ms`;
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }
})();
