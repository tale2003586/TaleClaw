const vscode = require("vscode");
const fs = require("fs");
const os = require("os");
const path = require("path");
const crypto = require("crypto");
const { spawn } = require("child_process");

let provider;
let activeChild = null;
let lastResult = null;
let output;

function activate(context) {
  output = vscode.window.createOutputChannel("TaleClaw");
  provider = new TaleClawViewProvider(context.extensionUri);
  context.subscriptions.push(output);
  context.subscriptions.push(
    vscode.window.registerWebviewViewProvider("taleclaw.agentView", provider, {
      webviewOptions: { retainContextWhenHidden: true }
    })
  );
  context.subscriptions.push(
    vscode.commands.registerCommand("taleclaw.runTask", runTaskFromCommand),
    vscode.commands.registerCommand("taleclaw.cancelTask", cancelTask),
    vscode.commands.registerCommand("taleclaw.openLastTrace", openLastTrace),
    vscode.commands.registerCommand("taleclaw.refreshView", refreshView),
    vscode.commands.registerCommand("taleclaw.openSettings", openSettings),
    vscode.commands.registerCommand("taleclaw.composeTask", composeTask)
  );
}

function deactivate() {
  cancelTask();
}

class TaleClawViewProvider {
  constructor(extensionUri) {
    this.extensionUri = extensionUri;
    this.view = null;
  }

  resolveWebviewView(webviewView) {
    this.view = webviewView;
    const webview = webviewView.webview;
    webview.options = {
      enableScripts: true,
      localResourceRoots: [this.extensionUri]
    };
    webview.html = renderHtml(webview, this.extensionUri);
    webview.onDidReceiveMessage(async (message) => {
      if (!message || typeof message !== "object") {
        return;
      }
      if (message.type === "ready") {
        this.postState();
      } else if (message.type === "runTask") {
        await runTask(message.payload || {});
      } else if (message.type === "cancelTask") {
        cancelTask();
      } else if (message.type === "openPath") {
        openPath(message.path);
      } else if (message.type === "openSettings") {
        openSettings();
      }
    });
  }

  post(message) {
    if (this.view) {
      this.view.webview.postMessage(message);
    }
  }

  postState() {
    this.post({
      type: "state",
      running: Boolean(activeChild),
      config: getUiConfig(),
      workspace: getWorkspaceRoot() || "",
      lastResult
    });
  }
}

async function runTaskFromCommand() {
  const task = await vscode.window.showInputBox({
    title: "TaleClaw Coding Agent",
    prompt: "输入要交给 coding agent 的任务",
    ignoreFocusOut: true
  });
  if (!task || !task.trim()) {
    return;
  }
  await runTask({ task: task.trim() });
}

async function runTask(payload) {
  if (activeChild) {
    vscode.window.showWarningMessage("TaleClaw is already running a task.");
    return;
  }
  const task = String(payload.task || "").trim();
  if (!task) {
    vscode.window.showWarningMessage("Please enter a task first.");
    return;
  }
  const workspace = getWorkspaceRoot();
  if (!workspace) {
    vscode.window.showErrorMessage("Open a workspace folder before running TaleClaw.");
    return;
  }
  const runtimeRoot = getRuntimeRoot();
  const scriptPath = path.join(runtimeRoot, "scripts", "run_vscode_agent_task.py");
  if (!fs.existsSync(scriptPath)) {
    vscode.window.showErrorMessage(`TaleClaw runtime script not found: ${scriptPath}`);
    output.show(true);
    output.appendLine(`Missing script: ${scriptPath}`);
    output.appendLine("Set taleclaw.runtimeRoot to the TaleClaw repository path.");
    return;
  }

  const config = { ...getUiConfig(), ...payload };
  const resultPath = path.join(
    os.tmpdir(),
    `taleclaw-vscode-${Date.now()}-${crypto.randomBytes(4).toString("hex")}.json`
  );
  const sessionId = workspaceSessionId(workspace);
  const args = [
    scriptPath,
    "--workspace", workspace,
    "--task", task,
    "--result-json", resultPath,
    "--session-id", sessionId,
    "--mode", String(config.mode || "coding"),
    "--max-reasoning-steps", String(toPositiveInt(config.maxReasoningSteps, 24)),
    "--subagent-max-reasoning-steps", String(toPositiveInt(config.subagentMaxReasoningSteps, 16)),
    "--rag-enabled", boolArg(config.ragEnabled),
    "--context-budget-enabled", boolArg(config.contextBudgetEnabled),
    "--tool-loop-guard-enabled", boolArg(config.toolLoopGuardEnabled)
  ];

  output.clear();
  output.show(true);
  output.appendLine(`Runtime: ${runtimeRoot}`);
  output.appendLine(`Workspace: ${workspace}`);
  output.appendLine(`Task: ${task}`);
  output.appendLine("");

  provider && provider.post({ type: "running", task, workspace, config });
  const pythonPath = vscode.workspace.getConfiguration("taleclaw").get("pythonPath", "python");
  const child = spawn(pythonPath, args, {
    cwd: runtimeRoot,
    env: { ...process.env, PYTHONUNBUFFERED: "1" }
  });
  activeChild = child;

  child.stdout.on("data", (chunk) => output.append(chunk.toString()));
  child.stderr.on("data", (chunk) => output.append(chunk.toString()));
  child.on("error", (err) => {
    output.appendLine(`\nFailed to start TaleClaw: ${err.message}`);
  });
  child.on("close", (code) => {
    activeChild = null;
    const result = readResult(resultPath, code);
    lastResult = result;
    provider && provider.post({ type: "result", result });
    provider && provider.postState();
    if (result.status === "error" || code !== 0) {
      vscode.window.showErrorMessage(`TaleClaw task failed: ${result.error || `exit ${code}`}`);
    } else {
      vscode.window.showInformationMessage(`TaleClaw task ${result.status}.`);
    }
  });
}

function cancelTask() {
  if (!activeChild) {
    return;
  }
  output.appendLine("\nCancelling TaleClaw task...");
  activeChild.kill("SIGTERM");
  activeChild = null;
  provider && provider.post({ type: "cancelled" });
  provider && provider.postState();
}

async function openLastTrace() {
  if (!lastResult || !lastResult.trace_summary) {
    vscode.window.showInformationMessage("No TaleClaw trace summary is available yet.");
    return;
  }
  await openPath(lastResult.trace_summary);
}

function refreshView() {
  provider && provider.postState();
}

function openSettings() {
  vscode.commands.executeCommand("workbench.action.openSettings", "taleclaw");
}

function composeTask() {
  if (provider && provider.view && typeof provider.view.show === "function") {
    provider.view.show(false);
  }
  provider && provider.post({ type: "compose" });
}

async function openPath(target) {
  if (!target) {
    return;
  }
  const uri = vscode.Uri.file(String(target));
  try {
    const stat = await vscode.workspace.fs.stat(uri);
    if (stat.type === vscode.FileType.Directory) {
      await vscode.commands.executeCommand("revealFileInOS", uri);
    } else {
      await vscode.window.showTextDocument(uri, { preview: false });
    }
  } catch (err) {
    vscode.window.showErrorMessage(`Cannot open path: ${target}`);
  }
}

function getUiConfig() {
  const config = vscode.workspace.getConfiguration("taleclaw");
  return {
    mode: config.get("mode", "coding"),
    maxReasoningSteps: config.get("maxReasoningSteps", 24),
    subagentMaxReasoningSteps: config.get("subagentMaxReasoningSteps", 16),
    ragEnabled: config.get("ragEnabled", false),
    contextBudgetEnabled: config.get("contextBudgetEnabled", true),
    toolLoopGuardEnabled: config.get("toolLoopGuardEnabled", true)
  };
}

function getWorkspaceRoot() {
  const configured = String(vscode.workspace.getConfiguration("taleclaw").get("workspaceRoot", "") || "").trim();
  if (configured) {
    return configured;
  }
  const active = vscode.window.activeTextEditor && vscode.window.activeTextEditor.document.uri;
  const folder = active ? vscode.workspace.getWorkspaceFolder(active) : undefined;
  const root = folder || (vscode.workspace.workspaceFolders || [])[0];
  return root ? root.uri.fsPath : "";
}

function getRuntimeRoot() {
  const configured = String(vscode.workspace.getConfiguration("taleclaw").get("runtimeRoot", "") || "").trim();
  if (configured) {
    return configured;
  }
  const workspaceRoot = getWorkspaceRoot();
  if (scriptExists(workspaceRoot)) {
    return workspaceRoot;
  }
  const parent = path.dirname(workspaceRoot);
  if (scriptExists(parent)) {
    return parent;
  }
  return workspaceRoot;
}

function scriptExists(root) {
  return Boolean(root) && fs.existsSync(path.join(root, "scripts", "run_vscode_agent_task.py"));
}

function readResult(resultPath, exitCode) {
  try {
    const raw = fs.readFileSync(resultPath, "utf8");
    return JSON.parse(raw);
  } catch (err) {
    return {
      status: "error",
      error: `No structured result was written. Exit code: ${exitCode}`,
      result_path: resultPath
    };
  }
}

function workspaceSessionId(workspace) {
  const digest = crypto.createHash("sha1").update(workspace).digest("hex").slice(0, 12);
  return `workspace-${digest}`;
}

function toPositiveInt(value, fallback) {
  const parsed = Number.parseInt(value, 10);
  return Number.isFinite(parsed) && parsed > 0 ? parsed : fallback;
}

function boolArg(value) {
  return value ? "1" : "0";
}

function renderHtml(webview, extensionUri) {
  const nonce = crypto.randomBytes(16).toString("base64");
  const styleUri = webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, "media", "style.css"));
  const scriptUri = webview.asWebviewUri(vscode.Uri.joinPath(extensionUri, "media", "main.js"));
  return `<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src ${webview.cspSource}; script-src 'nonce-${nonce}';">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <link href="${styleUri}" rel="stylesheet">
  <title>TaleClaw</title>
</head>
<body>
  <main class="shell">
    <section class="task-strip" aria-label="Recent tasks">
      <div id="taskList" class="task-list"></div>
      <button id="viewAll" class="view-all" type="button">View all (<span id="taskCount">0</span>)</button>
    </section>

    <section id="result" class="stage empty" aria-live="polite">
      <div class="agent-mark" aria-hidden="true"></div>
    </section>

    <section id="settingsPanel" class="settings-panel" hidden>
      <label>
        Mode
        <select id="mode">
          <option value="coding">coding</option>
          <option value="hybrid">hybrid</option>
          <option value="bot">bot</option>
        </select>
      </label>
      <label>
        Steps
        <input id="maxReasoningSteps" type="number" min="1" value="24">
      </label>
      <label>
        Sub Steps
        <input id="subagentMaxReasoningSteps" type="number" min="1" value="16">
      </label>
      <label class="check"><input id="ragEnabled" type="checkbox"> RAG</label>
      <label class="check"><input id="contextBudgetEnabled" type="checkbox"> Context Budget</label>
      <label class="check"><input id="toolLoopGuardEnabled" type="checkbox"> Loop Guard</label>
    </section>

    <form id="composer" class="composer">
      <textarea id="task" rows="1" placeholder="Do anything"></textarea>
      <div class="composer-bar">
        <button id="newTask" class="icon-button" type="button" title="New task" aria-label="New task">+</button>
        <button id="settings" class="access-button" type="button" title="Task access and settings">
          <span class="access-dot" aria-hidden="true"></span>
          <span id="accessLabel">Full access</span>
          <span class="chevron" aria-hidden="true">⌄</span>
        </button>
        <span class="spacer"></span>
        <button id="contextToggle" class="context-button" type="button">IDE context</button>
        <button id="cancel" class="send-button stop" type="button" title="Cancel task" aria-label="Cancel task" hidden>■</button>
        <button id="run" class="send-button" type="submit" title="Run task" aria-label="Run task">↑</button>
      </div>
    </form>

    <footer class="footer">
      <button id="workspaceMode" class="local-button" type="button">▱ Work locally <span aria-hidden="true">⌄</span></button>
      <span id="status" class="status">Idle</span>
    </footer>
  </main>
  <script nonce="${nonce}" src="${scriptUri}"></script>
</body>
</html>`;
}

module.exports = {
  activate,
  deactivate
};
