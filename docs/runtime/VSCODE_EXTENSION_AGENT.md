# TaleClaw VS Code 插件方案与技术文档

这份文档说明如何把 TaleClaw coding agent 作为 VS Code 插件使用。当前版本是 MVP：插件提供 VS Code 侧边栏前端，后端复用本仓库的 Python runtime，通过一次性 bridge 脚本运行任务并读取结构化结果。

## 目标

- 在 VS Code 里直接输入 coding task，不需要打开 Web UI。
- 复用现有 TaleClaw runtime、tool hooks、trace、workspace diff 和 metrics。
- 支持按任务切换关键功能：RAG、context budget、working memory、tool loop guard、reasoning step。
- 任务完成后在 VS Code 中查看最终回复、token、耗时、tool count、trace summary 和 run 目录。

## 当前实现

新增文件：

```text
vscode-extension/
  package.json
  extension.js
  media/
    main.js
    style.css
    taleclaw.svg

scripts/run_vscode_agent_task.py
docs/runtime/VSCODE_EXTENSION_AGENT.md
```

同时 `runtime/bootstrap.py` 新增了 `TOOL_LOOP_GUARD_ENABLED` 环境变量开关，默认开启。

## 架构

```text
VS Code WebviewView
        |
        | vscode.postMessage({ task, feature flags })
        v
VS Code Extension Host
        |
        | child_process.spawn(python scripts/run_vscode_agent_task.py ...)
        v
TaleClaw Python runtime
        |
        | trace / metrics / workspace_diff / final reply
        v
result.json + .runs/<run_id> artifacts
        |
        v
VS Code side panel + OutputChannel
```

插件不直接解析 agent 的 stdout。stdout/stderr 只显示在 `TaleClaw` OutputChannel；结构化结果由 bridge 写入临时 `result.json`，插件再读取。

## 使用方式

### 1. 准备 Python runtime

在 TaleClaw 仓库根目录准备好 `.env` 和 Python 环境：

```bash
cd /path/to/TaleClaw
python -m pip install -r requirements.txt
```

如果你暂时不部署 RAG，保持：

```env
RAG_ENABLED=0
```

插件运行任务时默认也会传入 `--rag-enabled 0`。

### 2. 永久安装到 VS Code

`F5` 只是开发调试方式；日常使用不需要每次启动 Extension Development Host。

当前插件没有 npm build 依赖，可以直接用软链接安装到本机 VS Code：

```bash
cd /path/to/TaleClaw
bash scripts/install_vscode_extension.sh
```

然后重启或 reload VS Code：

```text
Developer: Reload Window
```

之后每次正常打开 VS Code，左侧 Activity Bar 都会有 `TaleClaw` 面板。

如果你使用的是 VS Code Insiders 或其他兼容编辑器，可以指定扩展目录：

```bash
VSCODE_EXTENSIONS_DIR="$HOME/.vscode-insiders/extensions" \
  bash scripts/install_vscode_extension.sh
```

### 3. 启动插件开发宿主

推荐直接在 VS Code 中打开 TaleClaw 仓库根目录：

```text
/path/to/TaleClaw
```

然后在 Run and Debug 面板选择：

```text
Run TaleClaw VS Code Extension
```

按 `F5` 启动 Extension Development Host。

也可以只打开插件目录：

```text
/path/to/TaleClaw/vscode-extension
```

这两种打开方式都已经带了 `.vscode/launch.json`。

如果按 `F5` 没反应，先检查左侧 Run and Debug 是否选中了 `Run TaleClaw VS Code Extension`，或者用命令面板执行：

```text
Debug: Select and Start Debugging
```

如果你打开的是业务项目而不是 TaleClaw 仓库，需要在 VS Code 设置里配置：

```json
{
  "taleclaw.runtimeRoot": "/path/to/TaleClaw",
  "taleclaw.workspaceRoot": "/path/to/project-to-edit",
  "taleclaw.pythonPath": "/path/to/TaleClaw/.venv/bin/python"
}
```

### 4. 运行任务

在 VS Code 左侧 Activity Bar 打开 `TaleClaw` 面板：

1. 输入任务。
2. 选择模式和功能开关。
3. 点击 `Run`。
4. 在 `TaleClaw` OutputChannel 查看实时日志。
5. 结束后在面板里打开 `Trace` 或 `Run Dir`。

也可以用命令面板：

```text
TaleClaw: Run Coding Agent Task
```

## 配置项

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `taleclaw.runtimeRoot` | 当前 workspace | TaleClaw runtime 仓库路径 |
| `taleclaw.workspaceRoot` | 当前 workspace | 目标 coding workspace |
| `taleclaw.pythonPath` | `python` | 用来运行 bridge 的 Python |
| `taleclaw.mode` | `coding` | 运行前 pin 到 `coding` / `hybrid` / `bot` |
| `taleclaw.maxReasoningSteps` | `24` | lead agent reasoning step 上限 |
| `taleclaw.subagentMaxReasoningSteps` | `16` | subagent reasoning step 上限 |
| `taleclaw.ragEnabled` | `false` | 是否启用 RAG / security RAG / vector memory |
| `taleclaw.contextBudgetEnabled` | `true` | 是否启用 context section budget |
| `taleclaw.workingMemoryEnabled` | `true` | 是否启用 coding working memory checkpoint |
| `taleclaw.toolLoopGuardEnabled` | `true` | 是否启用重复工具调用保护 |

## Bridge 协议

插件调用：

```bash
python scripts/run_vscode_agent_task.py \
  --workspace /path/to/workspace \
  --task "修复这个测试失败" \
  --result-json /tmp/taleclaw-result.json \
  --session-id workspace-xxxx \
  --mode coding \
  --max-reasoning-steps 24 \
  --subagent-max-reasoning-steps 16 \
  --rag-enabled 0 \
  --context-budget-enabled 1 \
  --working-memory-enabled 1 \
  --tool-loop-guard-enabled 1
```

输出 JSON 示例：

```json
{
  "status": "completed",
  "stop_reason": null,
  "reply": "...",
  "workspace": "/path/to/workspace",
  "session_id": "vscode:workspace-xxxx",
  "run_id": "run_...",
  "run_dir": "/path/to/TaleClaw/.runs/run_...",
  "trace_summary": "/path/to/TaleClaw/.runs/run_.../trace_summary.md",
  "metrics": {
    "reasoning_steps": 8,
    "model_calls": 4,
    "tool_calls": 12,
    "total_tokens": 32000
  },
  "workspace_diff": {
    "created_count": 0,
    "modified_count": 2,
    "deleted_count": 0
  }
}
```

## 技术细节

### VS Code 前端

- `WebviewViewProvider` 注册侧边栏面板。
- `media/main.js` 负责收集任务和功能开关。
- `extension.js` 用 `child_process.spawn` 启动 Python bridge。
- `OutputChannel("TaleClaw")` 显示 stdout/stderr。
- `Trace` 按钮打开 `trace_summary.md`。
- `Run Dir` 按钮在系统文件管理器中定位 run 目录。

### Python bridge

- 先设置环境变量，再导入 `runtime.bootstrap`，确保 `config.py` 读到插件传入的功能开关。
- 注入：
  - `WORKSPACE_ROOTS`
  - `DEFAULT_CODING_WORKSPACE`
  - `RAG_ENABLED`
  - `CONTEXT_ENABLE_SECTION_BUDGET`
  - `WORKING_MEMORY_CHECKPOINT_ENABLED`
  - `TOOL_LOOP_GUARD_ENABLED`
- 通过 `runtime.run_message("/coding")` pin 模式。
- 再运行真实任务，并从 `.runs/<run_id>/metrics.json`、`report.json`、`run_state.json` 汇总结果。

## 后续路线

优先级建议：

1. **Patch Review UI**
   - 读取 `workspace_diff.json`。
   - 在 VS Code 中提供 per-file diff。
   - 支持 accept / revert 单个文件。

2. **流式事件**
   - bridge 改为 JSONL event stream。
   - 面板实时展示 reasoning step、tool calls、tokens。

3. **任务历史**
   - 读取 `.runs` index。
   - 面板展示最近任务列表。

4. **Eval 面板**
   - 接入 `scripts/run_coding_agent_matrix.py`。
   - 接入 `scripts/run_swebench_verified_matrix.py`。
   - 输出矩阵 report 链接。

5. **VSIX 打包**
   - 加入 `vsce package`。
   - 生成可安装的 `.vsix`。

这个 MVP 的关键点是先把“VS Code 内可用”打通，保留现有 runtime 的完整能力和工件体系；后续 UI 可以逐步变细，而不需要重写 agent 执行层。
