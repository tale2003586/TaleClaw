# 当前代码目录总结

本文档总结当前目录 `/home/tale/kaggle/mytry` 下的主要代码结构、运行链路和模块职责。

## 项目定位

这是一个 Python 实现的多入口 Agent 平台项目。它不是单一聊天脚本，而是一套围绕 LLM 对话、工具调用、多用户会话、长期记忆、插件扩展、Web/Telegram/飞书网关和定时任务搭建的运行框架。

核心能力包括：

- CLI、本地 Web、Telegram、飞书四类入口。
- 普通聊天、混合模式、Coding 任务模式。
- OpenAI-compatible 模型调用，支持 DeepSeek、MiMo、OpenAI 等 provider。
- 按用途路由模型，例如 `chat`、`coding`、`summary`、`hybrid`、`scheduled_agent`。
- 工具调用，包括文件读写、bash、memory、storage、sandbox、artifact、scheduler、PDF、web search。
- 多用户隔离，不同用户拥有独立的 session、memory、storage。
- 长期记忆、近期上下文、历史摘要和任务会话记忆提升。
- 定时任务和自动 Agent 任务，可将报告投递到 Telegram/飞书。

## 顶层入口

### `cli.py`

命令行入口。启动 `build_runtime()`，订阅 CLI 输出，然后循环读取用户输入。每次输入会提交给 runtime，再执行一轮 agent loop。

### `web/server.py`

本地 Web 服务入口。使用 Python 标准库 `ThreadingHTTPServer`，包装异步 Agent runtime，提供聊天、登录注册、session 管理、memory 查看、storage 文件浏览/预览/上传等接口。

### `telegram_worker.py`

Telegram bot worker 入口。加载 `.env` 后创建 Telegram gateway，并通过长轮询接收 Telegram 私聊消息。

### `feishu_worker.py`

飞书 bot worker 入口。启动飞书 HTTP callback 服务，接收飞书事件回调并转发到 Agent runtime。

### `scheduler_worker.py`

定时任务 worker。基于 APScheduler 周期性 reconcile schedule 配置，到点后执行搜索报告、workflow 或 autonomous agent schedule，并将结果写入报告或推送到网关 outbox。

### `config.py`

全局配置加载。负责读取 `.env`、设置本地代理、构建模型池、定义工作目录、记忆/任务路径、系统提示词和 compact 阈值。

## 核心运行链路

主要代码位于 `core/`。

整体链路如下：

```text
入口层
  -> build_runtime()
  -> AppRuntime
  -> MessageBus
  -> AgentLoop
  -> ModeRouter
  -> Pipeline 或 TaskSessionRunner
  -> AgentRunner
  -> ReasoningLoop
  -> ModelProvider / ToolExecutor
  -> SessionStore / MemoryLifecycle
```

### `core/bootstrap.py`

项目的运行时装配中心。它创建并连接：

- `MessageBus`
- `SessionManager`
- `ToolRegistry`
- `ModelPool`
- `ModeRouter`
- `ContextBuilder`
- `MemoryLifecycle`
- `PluginManager`
- `ToolExecutor`
- `Pipeline`
- `TaskSessionRunner`
- `ScheduledAgentRunner`
- `AgentLoop`
- `AppRuntime`

大部分模块之间的依赖关系都在这里被集中组装。

### `core/runtime.py`

定义 `AppRuntime`。它负责：

- 启动 outbound message dispatch。
- 接收用户消息并发布到 inbound bus。
- 执行一轮 `AgentLoop.run_once()`。
- 停止 runtime。

### `core/agent_loop.py`

Agent 的外层事件循环。每轮处理一个 inbound message：

1. 从 bus 取消息。
2. 获取或创建 session。
3. 写入用户身份信息。
4. 运行插件的 `before_turn`。
5. 通过 `ModeRouter` 判断当前应该走聊天、coding task 还是直接回复。
6. 调用 `Pipeline` 或 `TaskSessionRunner`。
7. 运行插件的 `after_turn`。
8. 保存 session。
9. 发布 outbound reply。

### `core/pipeline.py`

单轮对话的主流程。它负责 turn 级别的上下文准备和收尾：

- 重置工具状态。
- 对消息做 micro compact。
- 超过阈值时自动 compact。
- 构建上下文，包括 memory、inbox、background task 结果。
- 创建 `AgentSpec`。
- 调用 `AgentRunner`。
- 在 turn 结束后执行 `MemoryLifecycle.after_turn()`。

### `core/agent_runner.py`

`AgentRunner` 是 `AgentSpec` 到 `ReasoningLoop` 的适配层。它把当前 agent 的模型用途、profile、工具执行器、上下文构造器等参数交给共享推理循环。

### `core/reasoning_loop.py`

真正的模型/工具循环：

```text
构建上下文
  -> 调用模型
  -> 记录 assistant message
  -> 如果没有 tool call，则结束
  -> 执行工具
  -> 记录 tool result
  -> 必要时 compact / reflection / 循环保护
  -> 下一轮模型调用
```

它还包含多类保护：

- 推理步数上限。
- 重复工具调用保护。
- 不可用工具重复请求保护。
- scheduled agent 的 runtime budget。
- optional reflection agent。

### `core/provider.py`

OpenAI-compatible provider 封装。支持：

- 普通 `chat()`。
- 流式 `stream_chat()`。
- tool call 参数解析。
- OpenAI SDK response 到项目内部 `LLMResponse` 的转换。

### `core/model_pool.py`

模型池与路由系统。支持多个 provider profile，并按用途选择模型。默认 provider 包括：

- `deepseek`
- `mimo`
- `openai`

支持的 route 包括：

- `chat`
- `coding`
- `summary`
- `hybrid`
- `compact`
- `scheduled_agent`
- `teammate`
- `reflection`
- `scheduler_plan`
- `scheduler_analyze`
- `task_conclusion`

每条 route 可以配置 fallback chain，主模型失败时自动尝试备用模型。

## 模式系统

主要位于 `modes/`。

### `modes/router.py`

负责根据 session mode 和用户输入选择执行方式。

支持显式命令：

- `/coding`：进入编程模式。
- `/chat` 或 `/bot`：回到聊天模式。
- `/hybrid`：进入混合模式。

默认模式是 `hybrid`。在 hybrid 中，路由器会先用规则判断请求是否像 coding、scheduler、storage、memory 查询，再必要时调用 `HybridModeClassifier` 进一步判断。

Coding 模式有权限控制：只有 `user_role=admin` 的用户可以进入或使用 coding task。

### `modes/base.py`

定义 `ModeProfile`，描述一个模式的名称、系统提示和工具模式。

### `modes/bot.py` 与 `modes/coding.py`

分别定义普通聊天模式和 coding 模式的 profile。

## 工具系统

主要位于 `tools/`。

### `tools/tool_registry.py`

工具注册与可见性控制中心。

设计上分为：

- always-on tools：如 `recall_memory`、`memorize`、`tool_search`。
- mode preloaded tools：不同模式默认可见工具不同。
- deferred tools：高风险或不常用工具默认隐藏，需要先通过 `tool_search select:<tool_name>` 解锁。
- session scoped tools：按 session/user 边界执行。
- admin only tools：需要管理员身份。

这套机制避免模型每轮都看到全部工具，也降低高风险工具被误调用的概率。

### `tools/handlers.py`

实际工具实现。主要包括：

- `read_file` / `write_file` / `edit_file`
- `bash`
- `storage_list_files` / `storage_read_file` / `storage_write_file`
- `sandbox_list_files` / `sandbox_read_file` / `sandbox_write_file`
- `publish_artifact`
- `memorize` / `recall_memory`
- task/team/inbox 相关工具

它还实现了 storage 和 sandbox 的路径安全校验、文件大小限制、隐藏文件限制和 artifact 记录。

### `tools/executor.py`

统一工具执行器。执行流程为：

```text
ToolExecutionRequest
  -> pre hooks
  -> invoker
  -> post hooks
  -> ToolExecutionResult
```

### `tools/hooks.py`

工具 hook 实现：

- `FileWriteScopeHook`：限制写文件不能逃逸 workspace。
- `ToolLoopGuardHook`：阻止重复调用同一工具进入死循环。
- `ToolTraceHook`：记录工具调用 trace。
- `ShellSafetyHook`：阻止明显危险 shell 命令。

## 插件系统

主要位于 `plugins/`。

### `plugins/base.py`

定义插件接口：

- `setup()`
- `tools()`
- `tool_hooks()`
- `before_turn()`
- `after_turn()`

### `plugins/plugin_manager.py`

插件管理器。负责注册插件，将插件提供的工具和 hook 注入主工具系统，并在每轮 turn 前后调用插件生命周期。

### 当前插件

- `status_commands`：状态命令。
- `shell_safety`：shell 安全 hook。
- `web_search`：Tavily 搜索能力。
- `markdown_pdf`：Markdown 转 PDF。
- `scheduler`：定时任务、workflow、autonomous agent schedule。

## 会话系统

主要位于 `sessions/`。

### `sessions/session.py`

定义 `Session` 和 `SessionManager`。

`Session` 保存：

- `id`
- `messages`
- `current_mode`
- `created_at`
- `updated_at`
- `last_compacted`
- `metadata`

`SessionManager` 负责内存缓存和 SQLite 存储之间的协调。

### `sessions/session_store.py`

SQLite 持久化实现。包含两个表：

- `sessions`
- `messages`

每次保存 session 时会更新 session 元数据，并重写该 session 的消息列表。

## 记忆系统

主要位于 `memory/`。

### `memory/store.py`

Markdown 文件记忆存储。核心文件：

- `SELF.md`
- `MEMORY.md`
- `NOW.md`
- `PENDING.md`
- `HISTORY.md`
- `RECENT_CONTEXT.md`
- `RECENT_CONTEXT.json`

提供读取全部记忆、追加记忆、追加 pending memory、写入 history、写入 recent context 等能力。

### `memory/lifecycle.py`

每轮对话结束后的记忆生命周期：

1. 提取用户显式要求记住的内容。
2. 对偏好、项目约定等内容生成 pending candidate。
3. 摘要 assistant 回复。
4. 写入 `HISTORY.md`。
5. 更新 `RECENT_CONTEXT.md` 和 `RECENT_CONTEXT.json`。
6. 超出 recent limit 的 turn 可归档到 archive store。

### `memory/scoped_store.py`

多用户 memory 隔离。根据 session 的 user id 选择对应用户的 memory root。

## Coding 任务会话

主要位于 `tasksessions/`。

Coding 请求不会直接污染主会话，而是创建隔离任务会话：

```text
parent session
  -> task:<task_id>
  -> task-local memory
  -> isolated pipeline
  -> task conclusion extraction
  -> useful memory promotion
  -> artifact logs
```

### `tasksessions/session.py`

创建 `task:<task_id>` session，并为每个 task 建立独立 memory root。

### `tasksessions/runner.py`

执行 coding task 的主逻辑：

1. 创建 task session。
2. 初始化 task-local memory。
3. 注入全局 memory snapshot。
4. 构建独立 pipeline。
5. 执行 coding profile。
6. 提取任务结论。
7. 将有价值结论提升到全局 `PENDING.md`。
8. 写 task log 和 conclusions artifact。
9. 把结果返回 parent session。

### `tasksessions/promotion.py`

负责过滤和提升 task memory。会去重、过滤低置信度/噪声/过长内容，只把高价值结论写入全局 pending memory。

## Web 应用

主要位于 `web/`。

### `web/server.py`

Web 后端。提供：

- 静态文件服务。
- 登录、注册、登出。
- Cookie session。
- 聊天 API。
- 流式聊天 API。
- session 列表、读取、删除。
- memory 文件读取。
- storage 文件列表、预览、上传、下载。

Web 服务内部通过 `AgentService` 在后台线程中启动异步 runtime，并用锁保证同一时刻只处理一个 turn。

### `web/auth_store.py`

Web 用户认证存储。支持：

- 环境变量配置用户。
- SQLite 用户表。
- 密码 hash。
- session token。
- 注册开关。
- 匿名访问开关。

### `web/static/`

Web 前端静态资源：

- `index.html`
- `app.js`
- `styles.css`
- `login.html`
- `login.js`
- `auth.css`

## Telegram 网关

主要位于 `gateway/telegram/`。

### `gateway/telegram/adapter.py`

Telegram 长轮询适配器。只处理私聊文字消息。

支持命令：

- `/start`
- `/new`
- `/status`
- `/help`
- `/files`
- `/ls`
- `/cat`
- `/download`

普通文本消息会被提交到 Agent runtime，channel 为 `telegram`。

### `gateway/telegram/identity.py`

根据环境变量解析 Telegram 用户授权和用户角色。

### `gateway/telegram/store.py`

存储 Telegram offset、conversation id、outbox message/document。

### `gateway/telegram/client.py`

Telegram Bot API 客户端。

## 飞书网关

主要位于 `gateway/feishu/`。

### `gateway/feishu/adapter.py`

飞书 HTTP callback 适配器。支持：

- URL verification。
- verification token 校验。
- 事件去重。
- 私聊消息处理。
- 群消息按配置或 at 机器人触发。
- open_id 身份授权。
- storage 文件命令。
- outbox 消息/文件投递。

普通文本消息会被提交到 Agent runtime，channel 为 `feishu`。

### `gateway/feishu/identity.py`

根据 `FEISHU_ALLOWED_OPEN_IDS` 或 `FEISHU_USER_MAP` 做飞书用户授权。

### `gateway/feishu/store.py`

存储飞书事件去重信息、conversation id、outbox message/document。

### `gateway/feishu/client.py`

飞书开放平台 API 客户端，负责获取 token、发送文本和文档。

## 定时任务系统

主要位于 `plugins/scheduler/` 和 `scheduler_worker.py`。

### `plugins/scheduler/plugin.py`

向 Agent 暴露 schedule 相关工具，例如：

- `schedule_create`
- `schedule_create_workflow`
- `schedule_list`
- `schedule_run_now`
- `schedule_results`
- `schedule_create_agent_draft`
- `schedule_approve_agent`
- `schedule_reject_agent`
- `schedule_pending_approvals`

### `plugins/scheduler/store.py`

SQLite 存储 schedule、run 记录、审批状态、workflow 配置等。

### `plugins/scheduler/workflow.py`

受控 workflow 执行器，支持 web search、LLM analyze、write report 等步骤。

### `plugins/scheduler/agent_runner.py`

执行 autonomous scheduled agent。会创建 scheduled agent session，应用 approved capabilities 和 runtime budget，并记录工具 trace 和报告。

### `plugins/scheduler/planning.py`

自动任务规划和 capability 审计。它会判断哪些工具低风险可自动通过，哪些高风险能力需要用户明确批准。

## Skill 系统

主要位于 `skill_runtime/` 和 `skills/`。

### `skill_runtime/loader.py`

读取 `skills/**/SKILL.md`，提供 skill 描述和内容加载。

### `skills/`

当前包含：

- `agent-builder`
- `mcp-builder`
- `code-review`
- `pdf`

这些 skill 通过 `load_skill` 工具供 Agent 在需要特定知识时读取。

## 多用户隔离

### `user_scope.py`

统一处理 user id、user role、web session id、storage root、memory root。

不同入口最终都会把用户身份放入 session metadata：

- `user_id`
- `user_role`
- gateway-specific id

然后 memory/storage/session 访问会根据用户身份隔离。

## 测试覆盖

`tests/` 覆盖范围较广，包括：

- 模型池路由。
- hybrid mode routing。
- Web 登录注册。
- Web session 删除。
- Web streaming。
- 多用户隔离。
- Telegram gateway。
- 飞书 gateway。
- scheduler plugin。
- scheduler planning。
- markdown PDF plugin。
- task memory promotion。
- tool loop guard。
- agent runner reflection。

测试文件说明该项目已经在围绕实际平台能力做回归保护，而不是只验证单个函数。

## 当前工作区状态观察

当前 Git 工作区存在较多未提交改动和新增文件，主要集中在：

- runtime 解耦：`core/agent_runner.py`、`core/reasoning_loop.py`、`core/model_pool.py`、`core/reflection.py`。
- 飞书网关：`feishu_worker.py`、`gateway/feishu/`、`tests/test_feishu_gateway.py`。
- 模型路由：`docs/runtime/MODEL_PROVIDER_POOL_ROUTING.md`、相关测试。
- scheduler autonomous agent：`plugins/scheduler/*`、`scheduler_worker.py`。
- Web 和部署文档更新。

同时也有若干 `__pycache__/*.pyc` 被 Git 标记为修改，这类文件通常不建议纳入版本控制。

## 一句话总结

这个仓库是一个正在从本地 Agent demo 演进为多入口、多用户、可执行工具、可调度、可部署的 Agent 平台。核心架构可以概括为：

```text
入口适配层
  -> Runtime / Bus
  -> AgentLoop
  -> ModeRouter
  -> Pipeline / TaskSessionRunner
  -> ReasoningLoop
  -> ModelPool + ToolExecutor
  -> SessionStore + MemoryLifecycle + PluginManager
```

整体设计重点在于把对话、工具、记忆、用户隔离、插件和网关解耦，使同一套 Agent runtime 可以被 CLI、Web、Telegram、飞书和定时任务共同复用。
