# Project Structure

当前目录结构按“Agent Runtime Platform + 垂直 Agent 应用”来组织。

核心原则：

```text
runtime/ 承载通用 agent 执行链路
models/ 承载模型 provider 与模型路由
agents/ 承载平台上的垂直 agent 应用
tools/memory/sessions/bus/plugins 保持领域分层
旧兼容 shim 归档到 legacy/compat/
```

## 当前主结构

```text
runtime/
  bootstrap.py
  app_runtime.py
  agent_loop.py
  pipeline.py
  agent_runner.py
  reasoning_loop.py
  context.py
  compact.py
  reflection.py
  agent_spec.py
  trace/
    run_state.py
    trace_store.py
  routing/
    router.py
    intent.py
    execution_plan.py

models/
  provider.py
  model_pool.py
  model_task_runner.py

agents/
  coding/
    runner.py
    session.py
    artifacts.py
    conclusions.py
    promotion.py
    memory_lifecycle.py

bus/
  events.py
  user_bus.py
  team_bus.py

modes/
  base.py
  bot.py
  coding.py
  hybrid_classifier.py

tools/
  schema.py
  tool_registry.py
  policy.py
  executor.py
  hooks.py
  handlers.py

memory/
  store.py
  scoped_store.py
  lifecycle.py
  archive_store.py
  history_summary.py

sessions/
  session.py
  session_store.py

plugins/
  base.py
  plugin_manager.py
  markdown_pdf/
  shell_safety/
  status_commands/
  web_search/

gateway/
  base.py
  feishu/
  telegram/

web/
  server.py
  auth_store.py
  static/

legacy/
  compat/
    core/
    tasksessions/
    modes/
```

## Runtime Platform

`runtime/` 是系统主干，负责一次用户请求从入口到回复的完整执行。

```text
MessageBus
  -> AgentLoop
  -> RunState / TraceStore
  -> routing.ModeRouter
  -> Pipeline 或 agents.coding.TaskSessionRunner
  -> AgentRunner
  -> ReasoningLoop
  -> outbound reply
```

重要边界：

- `runtime/kernel.py` 集中持有运行时依赖。
- `runtime/app_runtime.py` 管理 runtime 生命周期。
- `runtime/trace/` 记录 `.runs/<run_id>/run_state.json`、`trace.jsonl`、`report.json`。
- `runtime/routing/` 把用户输入拆成 intent，并规划 execution path。

## Models

`models/` 承载模型相关能力：

- `provider.py`：OpenAI compatible / responses API 等 provider 封装。
- `model_pool.py`：按用途路由模型，支持 fallback。
- `model_task_runner.py`：一次性模型任务，比如总结、反思、结论提取。

## Coding Agent

`agents/coding/` 是 Runtime Platform 上的垂直应用。

它负责把仓库级代码任务隔离到 task session 中执行：

```text
TaskSessionRunner
  -> TaskSessionFactory
  -> task-local MemoryStore
  -> Coding Pipeline
  -> TaskArtifactWriter
  -> TASK_LOG.md / CONCLUSIONS.json
  -> TaskMemoryPromoter
```

这样主会话只保留最终摘要和高价值结论，工具调用、测试输出和中间过程留在 task session 与 trace 中。

## Legacy Compatibility

旧路径 shim 不再放在主目录中，已经归档到：

```text
legacy/compat/core/
legacy/compat/tasksessions/
legacy/compat/modes/
```

新代码应优先使用新路径：

```text
runtime.*
runtime.trace.*
runtime.routing.*
models.*
agents.coding.*
```

当前仓库代码和测试不再依赖 `core.*`、`tasksessions.*`、`modes.router`、`modes.intent`、`modes.execution_plan` 这些旧路径。
