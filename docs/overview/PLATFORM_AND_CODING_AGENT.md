# taleclaw Runtime Platform 与 Coding Agent

日期：2026-06-09

## 项目定位

taleclaw 的核心定位是 Agent Runtime Platform，而不是单一聊天机器人。

平台层负责承载通用 Agent 运行能力：

- 多入口：Web、CLI、飞书、Telegram。
- 多用户：用户身份、角色、会话隔离、私有 storage/memory。
- 模型路由：按用途路由 chat、coding、hybrid、summary、reflection 等模型。
- 工具治理：工具注册、工具 schema、执行器、hooks、可见性策略、延迟解锁。
- 记忆系统：ScopedMemoryStore、MemoryLifecycle、归档、TaskSession 局部记忆。
- 插件系统：状态命令、搜索、Markdown PDF 等。
- 运行证据链：RunState、TraceStore、trace.jsonl、report.json。

Coding Agent 是平台上的垂直应用。它复用平台能力，面向仓库代码任务提供隔离执行、工具调用、测试运行、文件编辑和任务结论沉淀。

## 当前分层

```text
taleclaw Agent Runtime Platform
  AppRuntime
  MessageBus
  AgentLoop
  ModeRouter / IntentClassifier / ExecutionPlanner
  Pipeline / AgentRunner / ReasoningLoop
  ModelPool
  ToolRegistry / ToolPolicy / ToolExecutor
  ScopedMemoryStore / MemoryLifecycle
  PluginManager
  TraceStore

taleclaw Coding Agent
  Coding Mode
  TaskSessionRunner
  TaskSessionFactory
  Task-local MemoryStore
  TaskArtifactWriter
  TaskConclusionExtractor
  TaskMemoryPromoter
```

## 一次用户请求的主链路

```text
Gateway/Web/CLI
  -> AppRuntime.submit_user_message()
  -> MessageBus
  -> AgentLoop.run_once()
  -> RunState + TraceStore
  -> ModeRouter
      -> IntentClassifier
      -> ExecutionPlanner
  -> Pipeline 或 TaskSessionRunner
  -> AgentRunner
  -> ReasoningLoop
      -> ModelPool routed provider
      -> ToolRegistry schemas_for_turn
      -> ToolExecutor hooks
      -> ToolPolicy execution check
  -> session save
  -> outbound reply
  -> .runs/<run_id>/run_state.json / trace.jsonl / report.json
```

## 面试表达

可以这样介绍：

> 这个项目不是把 OpenAI API 包一层做聊天，而是实现了一个小型 Agent Runtime。Web、CLI、飞书、Telegram 都通过同一个 MessageBus 和 AgentLoop 进入系统。bootstrap 作为 composition root 装配平台依赖；ModeRouter 把用户请求规划为普通 bot 或 coding task 路径；ReasoningLoop 统一执行模型和工具循环；ToolPolicy 管理当前用户、模式和 run 下的工具可见性；TraceStore 给每轮执行生成 run_state、trace 和 report。Coding Agent 是建立在这个平台上的仓库级垂直 Agent。

## 当前价值

- 架构可解释：入口、运行、工具、记忆、插件、trace 有清楚边界。
- Debug 可复盘：每轮 run 有结构化运行状态和事件流。
- 能扩展应用：Coding Agent 不需要复制一套 agent loop。
- 面向面试有亮点：展示的不只是功能，而是平台化抽象和工程收束。
