# taleclaw 课堂代码演示详稿

## 0. 演示目标

这份文档是给课堂演示用的。建议你把它当成“讲稿 + 代码导航图”：

- 先用 1 分钟讲清楚项目是什么。
- 再按主流程讲消息如何从入口进入 agent。
- 最后按功能模块讲：模式路由、工具调用、记忆、文件、插件、定时任务、多用户、模型路由。

核心表达：

> taleclaw 是一个可部署的个人 AI Agent 控制台。它不是简单地把用户消息发给大模型，而是围绕大模型搭了一套运行时：多入口接入、会话管理、模式路由、工具权限、记忆生命周期、插件扩展、定时任务、多用户隔离和模型路由。

## 1. 一句话总架构

完整消息链路：

```text
用户输入
  -> Web / CLI / Telegram
  -> MessageBus 入站队列
  -> AgentLoop 取出一条消息
  -> SessionManager 读取/创建会话
  -> PluginManager.before_turn 前置插件处理
  -> ModeRouter 判断 Bot / Code / Hybrid
  -> Pipeline 或 TaskSessionRunner
  -> ContextBuilder 拼 system prompt + history + memory
  -> ModelPool 选择模型 provider
  -> OpenAICompatibleProvider 调用模型
  -> ToolRegistry 给模型暴露可见工具
  -> ToolExecutor 执行工具并跑安全 hooks
  -> MemoryLifecycle 更新长期记忆
  -> MessageBus 出站队列
  -> Web / CLI / Telegram 返回用户
```

最重要的代码入口：

- `core/bootstrap.py`：装配所有核心对象。
- `core/runtime.py`：运行时外壳，提供 `submit_user_message()` 和 `run_once()`。
- `core/agent_loop.py`：一轮消息的主流程。
- `core/pipeline.py`：大模型推理和工具调用循环。

## 2. 目录地图

### 2.1 核心运行时

```text
core/
  bootstrap.py      组装 runtime
  runtime.py        AppRuntime，管理 bus 出站分发
  agent_loop.py     单轮消息主流程
  pipeline.py       LLM 推理循环 + 工具执行
  context.py        构造 system prompt / memory / inbox / background context
  provider.py       OpenAI-compatible 调用封装
  model_pool.py     多 provider 池和模型用途路由
  compact.py        长上下文压缩
```

### 2.2 模式系统

```text
modes/
  base.py              ModeProfile 数据结构
  bot.py               Bot 模式 profile
  coding.py            Coding 模式 profile
  automation.py        定时 agent 自动执行 profile
  router.py            Bot / Coding / Hybrid 路由
  hybrid_classifier.py Hybrid 候选请求的 LLM 二次判断
```

### 2.3 工具系统

```text
tools/
  schema.py         工具 schema
  handlers.py       工具实际实现
  tool_registry.py  工具注册、可见性、tool_search 解锁
  executor.py       执行工具并运行 hooks
  hooks.py          安全钩子、循环保护、trace
```

### 2.4 记忆系统

```text
memory/
  store.py            Markdown/JSON/SQLite 记忆读写
  lifecycle.py        每轮对话后的记忆生命周期
  history_summary.py  助手回复摘要
  scoped_store.py     多用户记忆隔离
  archive_store.py    RECENT_CONTEXT 淘汰内容归档
  dedup.py            记忆去重
```

### 2.5 Web / Telegram / 定时任务

```text
web/
  server.py        Python 标准库 HTTP server
  auth_store.py    登录、注册、Cookie session
  static/          前端 HTML/CSS/JS

gateway/telegram/
  adapter.py       Telegram long polling adapter
  client.py        Telegram Bot API client
  identity.py      Telegram 用户映射
  storage.py       Telegram storage 文件命令
  store.py         Telegram outbox / offset / conversation DB

plugins/scheduler/
  plugin.py        注册 schedule 工具
  store.py         定时任务 SQLite 存储
  workflow.py      搜索 + LLM 分析 workflow
  reports.py       生成报告文件
  planning.py      自动任务工具规划和权限审计
  agent_runner.py  定时 agent 执行器
```

## 3. 启动流程

### 3.1 Web 启动流程

启动命令：

```bash
python web/server.py --host 127.0.0.1 --port 8000
```

代码流程：

```text
web/server.py
  -> ThreadingHTTPServer 启动 HTTP 服务
  -> AgentService.ensure_started()
  -> AgentService._run_loop()
  -> AgentService._start_async()
  -> from runtime.bootstrap import build_runtime
  -> build_runtime()
  -> runtime.bus.subscribe_outbound("web", self._handle_outbound)
  -> runtime.start()
```

讲解点：

- Web 后端没有用 FastAPI/Flask，而是使用 Python 标准库 `http.server`。
- HTTP server 是同步的，但 agent runtime 是 asyncio 的，所以 `AgentService` 单独开一个线程跑事件循环。
- Web 请求进来后，`ask_stream()` 会用 `asyncio.run_coroutine_threadsafe()` 把消息投递到 runtime。

重要接口：

- `GET /api/health`：检查 HTTP 服务。
- `GET /api/runtime-health`：检查 agent runtime 是否能启动。
- `POST /api/chat`：普通非流式聊天。
- `POST /api/chat/stream`：流式聊天。
- `GET /api/sessions`：列出 Web 会话。
- `DELETE /api/session`：删除 Web 会话。
- `GET /api/memory`：读取记忆文件。
- `GET /api/files`：列出 storage 文件。
- `POST /api/files/upload`：上传文件。
- `GET /api/files/preview`：预览文件。
- `GET /api/files/download`：下载文件。

### 3.2 CLI 启动流程

启动命令：

```bash
python cli.py
```

代码流程：

```text
cli.py main_async()
  -> build_runtime()
  -> runtime.bus.subscribe_outbound("cli", print_cli_message)
  -> runtime.start()
  -> input() 读取用户输入
  -> runtime.submit_user_message(query)
  -> runtime.run_once()
```

讲解点：

- CLI 和 Web 复用同一个 `build_runtime()`。
- 区别只是入口不同，CLI 直接调用 `runtime.submit_user_message()`。
- 出站消息通过 `bus.subscribe_outbound("cli", handler)` 打印到终端。

### 3.3 Telegram 启动流程

启动命令：

```bash
python telegram_worker.py
```

代码流程：

```text
telegram_worker.py
  -> build_runtime()
  -> TelegramGateway.from_env(runtime)
  -> TelegramGateway.run_forever()
  -> runtime.bus.subscribe_outbound("telegram", self.send)
  -> client.get_updates()
  -> handle_update()
  -> runtime.submit_user_message(... channel="telegram" ...)
  -> runtime.run_once()
  -> outbound -> send_message()
```

讲解点：

- Telegram 使用 long polling，不需要公网 webhook。
- `TelegramIdentityResolver` 根据 `TELEGRAM_ALLOWED_USER_IDS` 或 `TELEGRAM_USER_MAP` 把 Telegram 用户映射到系统用户。
- Telegram 除了聊天，还支持文件命令：`/files`、`/cat`、`/download`。
- 定时任务推送不是直接调用 Telegram API，而是先写入 `.gateway/telegram.db` outbox，再由 Telegram worker `flush_outbox()` 发送。

### 3.4 Scheduler Worker 启动流程

启动命令：

```bash
python scheduler_worker.py
```

代码流程：

```text
scheduler_worker.py
  -> SchedulerWorker()
  -> ScheduleStore() 读取 .scheduler/schedules.db
  -> ScheduledReportService()
  -> APScheduler BlockingScheduler
  -> reconcile()
     -> list_schedules(enabled_only=True)
     -> add_job(run_schedule)
  -> 到时间 run_schedule(schedule_id)
     -> workflow schedule: reports.run()
     -> agent schedule: agent_runner.run()
     -> TelegramScheduleNotifier.notify()
```

讲解点：

- `scheduler_worker.py` 是独立进程，适合 Docker 单独跑。
- 它只负责定时触发；任务配置来自 `.scheduler/schedules.db`。
- 定时任务完成后可以写 report 到 `storage/reports/`，也可以推送到 Telegram outbox。

## 4. build_runtime 组装流程

重点文件：`core/bootstrap.py`

代码中的核心顺序：

```text
build_runtime()
  1. cleanup_expired_sandboxes()
  2. bus = MessageBus()
  3. sessions = SessionManager()
  4. tools = build_lead_tool_registry()
  5. provider = MODEL_POOL.routed_provider("chat")
  6. router = ModeRouter(hybrid_classifier=...)
  7. memory_store = ScopedMemoryStore(...)
  8. context_builder = ContextBuilder(memory_store=...)
  9. memory_lifecycle = MemoryLifecycle(...)
 10. scheduler_plugin = SchedulerPlugin()
 11. plugin_manager = PluginManager([...])
 12. executor = ToolExecutor([...hooks...])
 13. pipeline = Pipeline(...)
 14. task_session_runner = TaskSessionRunner(...)
 15. scheduler_plugin.bind_agent_runner(...)
 16. loop = AgentLoop(...)
 17. return AppRuntime(bus=bus, loop=loop)
```

你可以这样讲：

> `bootstrap.py` 是依赖注入的地方。这里没有让各个模块自己偷偷初始化全局对象，而是统一创建 bus、session、tools、router、memory、plugins、pipeline，然后传给 AgentLoop。这样 Web、CLI、Telegram 都能复用同一套 runtime。

关键对象职责：

- `MessageBus`：把入口和 agent loop 解耦。
- `SessionManager`：会话状态和消息历史持久化。
- `ToolRegistry`：知道有哪些工具、哪些模式能用、当前回合哪些可见。
- `ModeRouter`：判断这句话应该走聊天还是代码任务。
- `ContextBuilder`：把 system prompt、记忆、历史消息拼成模型上下文。
- `ModelPool`：按用途选模型。
- `ToolExecutor`：执行工具并跑安全 hook。
- `MemoryLifecycle`：每轮结束后更新长期记忆。
- `PluginManager`：加载插件工具和插件 hook。

## 5. 一轮消息的完整流程

重点文件：`core/agent_loop.py`

伪代码：

```text
AgentLoop.run_once()
  inbound = bus.consume_inbound()
  session = sessions.get_or_create(inbound.session_key)
  _apply_inbound_identity(session, inbound)

  plugin_result = plugin_manager.before_turn(inbound, session)
  if plugin_result.abort:
      save session
      publish outbound
      return

  route = router.route(session, inbound.content)

  if route.switched:
      session.add_message("user", ...)
      session.add_message("assistant", switch_reply)
      save session
      publish outbound
      return

  session.add_message("user", inbound.content)

  if route.profile.tool_mode == "coding":
      reply = task_session_runner.run_coding_task(...)
      session.add_message("assistant", reply)
  else:
      reply = pipeline.run(session, route.profile)

  plugin_manager.after_turn(inbound, session, reply)
  sessions.save(session)
  bus.publish_outbound(reply)
```

流程解释：

1. 从 bus 的入站队列取一条消息。
2. 用 `session_key` 找到对应会话，没有就创建。
3. 把用户身份写进 session metadata，用于多用户隔离和权限判断。
4. 插件可以在模型调用前拦截，比如状态命令。
5. `ModeRouter` 判断是否切换模式或选择执行路径。
6. 如果只是 `/coding`、`/chat`、`/hybrid`，直接返回模式切换消息。
7. 如果是 coding 请求，创建隔离的 TaskSession。
8. 否则进入普通 Pipeline。
9. 结束后插件做 after_turn，例如定时任务可能记录一些状态。
10. 保存 session 并发出出站消息。

## 6. 模式系统流程

重点文件：

- `modes/router.py`
- `modes/hybrid_classifier.py`
- `modes/bot.py`
- `modes/coding.py`

### 6.1 三种模式

```text
Bot 模式
  - 面向日常聊天、文件区、记忆、轻量工具
  - 不允许危险项目级写入

Coding 模式
  - 面向代码阅读、修改、测试、终端命令
  - 只允许 admin 用户进入
  - 会创建 TaskSession 隔离运行

Hybrid 模式
  - 默认模式
  - 普通问题留在 Bot
  - 代码候选请求先规则匹配，再交给 LLM 判断是否进入 Coding
```

### 6.2 显式切换流程

用户输入：

```text
/coding
```

代码流程：

```text
ModeRouter.route()
  -> text in {"/coding", "进入编程模式", "编程模式"}
  -> _coding_allowed(session)
  -> admin: session.set_mode("coding")
  -> return RouteResult(switched=True, profile=CODING_PROFILE)
```

如果普通用户输入 `/coding`：

```text
_coding_allowed(session) == False
  -> session.set_mode("bot")
  -> reply: 当前账号没有 Coding 模式权限，已保持聊天模式。
```

### 6.3 Hybrid 自动路由流程

Hybrid 的设计不是“看到代码关键词就切 Code”，而是两层判断：

```text
用户输入
  -> _candidate_for_hybrid()
     -> 强代码请求 / 定时任务 / storage 文件 / memory 查询 / 弱代码请求
  -> 如果候选不是 coding
     -> 留在 Bot
  -> 如果候选是 coding
     -> HybridModeClassifier.should_use_coding()
     -> LLM 返回 {"mode":"coding|bot","reason":"..."}
  -> coding: TaskSession
  -> bot: 普通 Pipeline
```

为什么这样设计：

- 纯正则容易误判，例如“解释一下 Git 是什么”不一定需要 Code 模式。
- 纯 LLM 每次判断成本高，也慢。
- 混合方式：先用规则筛选候选，再用 LLM 只判断模糊情况。

可以演示：

```text
/hybrid
```

普通问题：

```text
帮我总结一下这个项目的功能
```

代码问题：

```text
请帮我检查 core/pipeline.py 里工具循环保护的逻辑
```

### 6.4 路由结果记录

`ModeRouter._record()` 会把最近一次路由写入：

```text
session.metadata["last_route"]
```

里面包含：

- `intent`
- `execution`
- `profile`
- `tool_mode`
- `confidence`
- `reason`
- `switched`

这个设计方便后续调试：如果模式走错，可以看 metadata 里为什么这么路由。

## 7. Pipeline 推理循环

重点文件：`core/pipeline.py`

Pipeline 是 agent 真正“思考 + 调工具”的地方。

### 7.1 单次 Pipeline 的完整流程

```text
Pipeline.run()
  -> _run_turn()
     -> _before_turn()
        -> reset_turn_unlocks()
        -> reset tool hooks
        -> micro_compact()
        -> auto_compact if too long

     while True:
       -> reasoning_steps += 1
       -> 检查 max_reasoning_steps
       -> 检查 scheduled_agent 自动任务预算
       -> _before_reasoning()
          -> micro_compact()
          -> drain background results
          -> read team inbox
          -> context_builder.build()

       -> _reasoning_step()
          -> _provider_and_model()
          -> provider.chat() 或 provider.stream_chat()
          -> tools.schemas_for_turn()

       -> _after_reasoning_step()
          -> assistant message append to session

       -> if no tool_calls:
          -> _after_turn()
          -> return

       -> _execute_tool_calls()
          -> ToolExecutor.execute()
          -> append tool result to session

       -> 如果工具循环保护触发，停止
       -> 如果不可见工具反复调用，停止
       -> 如果 manual compact，压缩 messages
```

### 7.2 为什么是循环

大模型一次回复可能不是最终答案，而是工具调用，例如：

```text
assistant -> tool_call: read_file("core/pipeline.py")
tool      -> 返回文件内容
assistant -> 根据文件内容继续分析
```

所以 Pipeline 要循环：

```text
模型输出工具调用
  -> 执行工具
  -> 工具结果放回上下文
  -> 再问模型
  -> 直到模型不再调用工具
```

### 7.3 循环保护

保护点：

- `DEFAULT_MAX_REASONING_STEPS = 24`
- `MAX_UNAVAILABLE_TOOL_ATTEMPTS = 2`
- `ToolLoopGuardHook(repeat_limit=3)`

作用：

- 模型一直调用工具不会无限跑。
- 模型一直调用当前不可见工具会被停止。
- 模型重复同样工具和参数会被 hook 拦截。

对应测试：

- `tests/test_pipeline_tool_loop_guard.py`
- `docs/runtime/AGENT_LOOP_UNAVAILABLE_TOOL_GUARD.md`

## 8. ContextBuilder 上下文构造

重点文件：`core/context.py`

构造出的 messages 结构：

```text
[
  {"role": "system", "content": system_prompt},
  ...session.messages,
  {"role": "user", "content": context_frame}
]
```

system prompt 包含：

- 当前 mode profile 的系统提示。
- 什么时候使用 `recall_memory`。
- 什么时候使用 `memorize`。
- 延迟工具需要先用 `tool_search`。

context frame 可能包含：

```text
<memory>
长期记忆内容
</memory>

<inbox>
团队 inbox 消息
</inbox>

<background-results>
后台任务结果
</background-results>
```

讲解点：

> 这里不是把所有东西都塞进 prompt，而是按需拼上下文。普通聊天只拼记忆；coding task 可能额外拼团队 inbox 和后台任务结果。

## 9. 模型调用和模型池

重点文件：

- `core/provider.py`
- `core/model_pool.py`
- `config.py`

### 9.1 Provider 封装

`OpenAICompatibleProvider` 做了两件事：

1. 把项目内部统一的 `messages/tools/model/max_tokens` 转成 OpenAI-compatible 请求。
2. 把模型返回的 `tool_calls` 解析成项目内部的 `ToolCall`。

支持：

- `chat()`：普通非流式。
- `stream_chat()`：流式输出，并能重组流式 tool call 参数。

### 9.2 ModelPool 用途路由

用途：

```text
chat                普通聊天
coding              代码模式
summary             记忆总结
hybrid              Hybrid 模式 LLM 判断
compact             长上下文压缩
scheduled_agent     定时 agent 执行
scheduler_plan      定时任务工具规划
scheduler_analyze   定时报告分析
task_conclusion     TaskSession 结论抽取
```

示例配置：

```env
LLM_PROVIDER=deepseek
DEEPSEEK_API_KEY=...
MIMO_API_KEY=...

LLM_ROUTE_CHAT=deepseek
LLM_ROUTE_CODING=deepseek
LLM_ROUTE_SUMMARY=mimo,deepseek
LLM_ROUTE_SCHEDULER_ANALYZE=mimo,deepseek
```

解释：

- 普通聊天走 DeepSeek。
- 总结和报告分析优先走 MiMo。
- MiMo 失败时 fallback 到 DeepSeek。

### 9.3 Pipeline 如何选择模型

`Pipeline._model_purpose()`：

```text
if session.metadata["kind"] == "scheduled_agent":
    purpose = "scheduled_agent"
elif profile.tool_mode == "coding":
    purpose = "coding"
else:
    purpose = "chat"
```

然后：

```text
MODEL_POOL.routed_provider(purpose)
MODEL_POOL.model_for(purpose)
```

## 10. 工具系统

重点文件：

- `tools/schema.py`
- `tools/handlers.py`
- `tools/tool_registry.py`
- `tools/executor.py`
- `tools/hooks.py`

### 10.1 工具注册流程

```text
build_lead_tool_registry()
  -> registry = ToolRegistry()
  -> handlers = make_lead_handlers(TEAM)
  -> for schema in LEAD_TOOLS:
       registry.register(
         schema,
         handler,
         risk=_risk_for_tool(name),
         enabled_modes=_modes_for_tool(name),
         source="lead",
       )
```

工具有这些属性：

- `name`
- `schema`
- `handler`
- `risk`
- `enabled_modes`
- `source`
- `always_on`
- `session_scoped`
- `admin_only`

### 10.2 工具可见性

工具不是全部直接暴露给模型。

始终可见：

```text
recall_memory
memorize
tool_search
```

Bot 预加载：

```text
load_skill
storage_list_files
storage_read_file
storage_write_file
sandbox_list_files
sandbox_read_file
sandbox_write_file
publish_artifact
```

Coding 预加载：

```text
read_file
load_skill
task_create
task_update
task_list
task_get
claim_task
check_background
read_inbox
compact
```

延迟工具：

```text
bash
write_file
edit_file
background_run
spawn_teammate
broadcast
...
```

为什么这样设计：

- 降低模型误调用高风险工具的概率。
- Prompt 工具列表更短，模型更容易选对工具。
- 需要高风险工具时，模型必须先用 `tool_search` 解锁。

### 10.3 tool_search 流程

模型想用不可见工具时：

```text
assistant -> tool_search(query="bash")
tool      -> 返回可解锁工具列表
assistant -> tool_search(query="select:bash")
tool      -> session.metadata["unlocked_tools"].append("bash")
assistant -> 下一步可以调用 bash
```

如果模型不按流程，直接调用不可见工具：

```text
ToolRegistry.execution_error_for_turn()
  -> "Tool 'bash' is not visible in this turn. Call tool_search ..."
```

如果连续两次还是调用不可见工具：

```text
Pipeline -> unavailable_tool_loop -> 停止本轮
```

### 10.4 ToolExecutor 和 hooks

执行流程：

```text
ToolExecutor.execute()
  -> 遍历 hooks
  -> hook.matches(request)
  -> hook.before(request)
     -> 可能修改参数
     -> 可能 deny
  -> invoker(tool_name, arguments)
  -> hook.after(request, result)
  -> 返回 ToolExecutionResult
```

当前 hooks：

- `FileWriteScopeHook`：限制 `write_file/edit_file` 不得逃出 workspace。
- `ToolLoopGuardHook`：阻止重复工具调用。
- `ToolTraceHook`：记录工具调用 trace。
- `ShellSafetyHook`：由 shell safety 插件提供，阻止危险 shell 命令。
- `ToolApprovalPolicyHook`：定时 agent 使用，检查已批准能力。

## 11. TaskSession / Code 模式流程

重点文件：

- `tasksessions/runner.py`
- `tasksessions/session.py`
- `tasksessions/artifacts.py`
- `tasksessions/conclusions.py`
- `tasksessions/promotion.py`

### 11.1 为什么需要 TaskSession

Code 模式通常会读文件、跑命令、修改代码。它不应该污染主聊天上下文，也不应该把所有临时工具结果写进全局记忆。

所以设计成：

```text
主会话
  -> 判断需要 coding
  -> 创建 task session
  -> task session 独立运行
  -> 提取结论
  -> 只把总结返回主会话
```

### 11.2 Code 请求执行流程

```text
AgentLoop
  -> route.profile.tool_mode == "coding"
  -> TaskSessionRunner.run_coding_task()
     -> factory.create()
     -> 创建 .task_sessions/<task_id>/
     -> 创建 task local memory
     -> 写入任务说明和全局记忆快照
     -> _build_task_pipeline()
     -> task_pipeline.run()
     -> get_last_assistant_text()
     -> TaskConclusionExtractor.extract()
     -> TaskMemoryPromoter.promote()
     -> TaskArtifactWriter.write()
     -> 返回主会话摘要
```

### 11.3 Task 记忆提升流程

TaskSession 里可能产生很多临时信息，例如工具日志、执行过程、失败尝试。

项目做了一个过滤：

```text
task reply / tool trace
  -> TaskConclusionExtractor 用 LLM 抽取可复用结论
  -> TaskMemoryPromoter 去重和过滤噪音
  -> 有价值内容提升到主会话 PENDING
  -> 详细过程写 task log
```

对应文档：

- `docs/sessions-memory/TASK_SESSION_MEMORY_SCOPE_FIX.md`
- `docs/sessions-memory/TASK_MEMORY_HYBRID_PROMOTION.md`

## 12. 记忆系统流程

重点文件：

- `memory/lifecycle.py`
- `memory/store.py`
- `memory/scoped_store.py`
- `memory/history_summary.py`
- `memory/archive_store.py`

### 12.1 每轮结束后做什么

`MemoryLifecycle.after_turn(session)`：

```text
1. 根据 session 找到当前用户的 MemoryStore
2. 取最后一条 user text 和 assistant text
3. 如果用户明确说“记住...”
   -> 直接写 MEMORY
4. 否则如果检测到偏好/项目约定候选
   -> 写 PENDING
5. 助手回复交给 HistorySummarizer 总结
6. user 原文 + assistant_summary 写 HISTORY
7. 最新轮次写 RECENT_CONTEXT
8. RECENT_CONTEXT 超出 recent_limit
   -> 淘汰旧轮次
   -> 写入 archive_store
```

### 12.2 为什么用户原文完整保存、助手回复总结

设计原因：

- 用户原文通常短，并且偏好/需求需要保真。
- 助手回复可能很长，完整保存会让 history 快速膨胀。
- 总结助手回复可以保留决策、文件路径、命令、未解决事项。

### 12.3 多用户记忆隔离

`ScopedMemoryStore.for_session(session)`：

```text
session.metadata["user_id"]
  -> memory_root_for_session()
  -> .users/<user_id>/memory
```

没有 user_id 的旧本地模式：

```text
memory/
```

这样公网部署时不同用户不会共享 memory。

### 12.4 可演示文本

```text
请记住，我喜欢回答直接一点，少写客套话。
```

然后问：

```text
你记得我的回答偏好吗？
```

可以打开 memory 页面或文件看变化。

## 13. Storage / Sandbox / 文件功能

重点文件：

- `web/server.py`
- `user_scope.py`
- `tools/handlers.py`
- `gateway/telegram/storage.py`

### 13.1 Web storage 流程

前端操作：

```text
上传 / 新建目录 / 重命名 / 删除 / 预览 / 下载
```

后端 API：

```text
GET  /api/files
GET  /api/files/preview
GET  /api/files/download
POST /api/files/upload
POST /api/files/mkdir
POST /api/files/rename
POST /api/files/delete
```

路径安全：

```text
_safe_storage_path()
  -> storage_dir = storage_root_for_user(ROOT, user_id)
  -> candidate = (storage_dir / raw).resolve()
  -> candidate 必须在 storage_dir 里面
```

### 13.2 Bot 写文件流程

Bot 模式不能随意写项目文件，只能写：

```text
.users/<user_id>/storage/
.task_sandbox/
```

典型请求：

```text
帮我生成一份项目介绍，保存成 markdown 文件
```

模型会调用：

```text
storage_write_file
```

生成文件一般落到：

```text
storage/generated/
```

### 13.3 Sandbox 流程

Sandbox 是临时工作区：

```text
bot session
  -> sandbox_write_file
  -> sandbox_read_file
  -> sandbox_list_files
  -> publish_artifact
  -> storage/generated/
```

作用：

- 让 Bot 可以生成中间文件。
- 最终只把用户需要的成果发布到 storage。
- 避免 Bot 直接写项目代码。

## 14. 多用户隔离流程

重点文件：

- `web/auth_store.py`
- `user_scope.py`
- `core/agent_loop.py`
- `memory/scoped_store.py`

### 14.1 登录注册流程

Web 登录：

```text
POST /api/auth/login
  -> WebAuthStore 校验用户名密码
  -> 创建 auth session
  -> Set-Cookie: taleclaw_session=...
```

Web 注册：

```text
POST /api/auth/register
  -> 如果 WEB_ALLOW_REGISTRATION=1
  -> 创建普通 user 账号
  -> 设置 cookie
```

### 14.2 用户身份进入 agent

Web 调用聊天时：

```text
RequestHandler._handle_chat_stream()
  -> current_user()
  -> AgentService.ask_stream(user_id=..., user_role=...)
  -> runtime.submit_user_message(metadata={
       "user_id": user_id,
       "user_role": user_role
     })
```

AgentLoop：

```text
_apply_inbound_identity()
  -> session.metadata["user_id"] = inbound.metadata["user_id"]
  -> session.metadata["user_role"] = inbound.metadata["user_role"]
```

之后所有 scoped 工具、memory、storage 都根据 session metadata 判断归属。

### 14.3 admin 和 user 权限

admin：

- 可以进入 Coding。
- 可以使用服务器级定时任务。
- 可以访问代码相关高风险工具。

user：

- 只能用受限 Bot/Hybrid 能力。
- 有自己的 storage 和 memory。
- 不能获得 bash、项目写入等能力。

## 15. 插件系统流程

重点文件：

- `plugins/base.py`
- `plugins/plugin_manager.py`
- `plugins/web_search/plugin.py`
- `plugins/markdown_pdf/plugin.py`
- `plugins/scheduler/plugin.py`

### 15.1 插件接口

插件可以提供：

```text
setup(context)
tools()
tool_hooks()
before_turn(context)
after_turn(context, reply)
```

`PluginManager.register(plugin)`：

```text
plugin.setup(context)
for tool in plugin.tools():
    tool_registry.register(...)
self._tool_hooks.extend(plugin.tool_hooks())
```

### 15.2 当前插件

```text
ShellSafetyPlugin
  -> 提供 shell 安全 hook

StatusCommandsPlugin
  -> /status 等状态命令

WebSearchPlugin
  -> web_search 工具

MarkdownPdfPlugin
  -> markdown_to_pdf 工具

SchedulerPlugin
  -> schedule_create / schedule_list / schedule_run_now 等工具
```

## 16. Web Search 流程

重点文件：

- `plugins/web_search/plugin.py`
- `plugins/web_search/client.py`

流程：

```text
用户：帮我搜索最新 AI 新闻
  -> Bot mode
  -> tools.schemas_for_turn() 包含 web_search
  -> 模型选择 web_search
  -> TavilySearchClient.search()
  -> 返回搜索结果
  -> 模型整理成回答
```

数据来源：

```env
TAVILY_API_KEY=...
```

讲解点：

- 搜索是插件，不是写死在 Pipeline。
- Bot 模式也能看到 web search。
- 定时任务 workflow 也复用同一个搜索 client。

## 17. Markdown 转 PDF 流程

重点文件：

- `plugins/markdown_pdf/plugin.py`
- `plugins/markdown_pdf/renderer.py`

流程：

```text
用户：把 storage/generated/report.md 转成 PDF
  -> 模型调用 markdown_to_pdf
  -> plugin 校验源文件路径和大小
  -> renderer 渲染 PDF
  -> 输出到 storage
```

安全点：

- 源文件不能逃出 workspace/storage 允许范围。
- 限制最大输入大小：`MARKDOWN_PDF_MAX_BYTES`。
- 远程图片会被跳过，避免隐式网络请求。

## 18. 定时任务流程

重点文件：

- `plugins/scheduler/plugin.py`
- `plugins/scheduler/store.py`
- `plugins/scheduler/workflow.py`
- `plugins/scheduler/reports.py`
- `scheduler_worker.py`

### 18.1 普通搜索日报

用户请求：

```text
每天早上 8 点搜索最新 AI 资讯，生成一份日报
```

代码流程：

```text
模型调用 schedule_create 或 schedule_create_workflow
  -> SchedulerPlugin
  -> ScheduleStore 写 .scheduler/schedules.db
  -> scheduler_worker.reconcile()
  -> APScheduler add_job()
  -> 到时间 run_schedule()
  -> ScheduledReportService.run()
  -> WorkflowExecutor.execute()
     -> web_search
     -> optional llm_analyze
     -> write_report
  -> report 写入 storage/reports/
  -> TelegramScheduleNotifier.notify()
```

### 18.2 带分析的 workflow

workflow 示例：

```json
[
  {"type": "web_search", "query": "latest AI news", "topic": "news"},
  {"type": "llm_analyze", "prompt": "分析趋势和影响"},
  {"type": "write_report", "title": "Daily AI Digest"}
]
```

执行时：

```text
web_search
  -> TavilySearchClient
llm_analyze
  -> LLMAnalysisClient
  -> MODEL_POOL route: scheduler_analyze
write_report
  -> ScheduledReportService 写 markdown
```

### 18.3 Autonomous Scheduled Agent

用户请求：

```text
每天 8 点自动完成一个 AI 资讯分析报告，并保存文件
```

规划流程：

```text
schedule_create_agent_draft
  -> ScheduledTaskPlanner
  -> LLMTaskPlanningClient
  -> MODEL_POOL route: scheduler_plan
  -> 返回 requested_tools / limits / rationale
  -> ToolCapabilityAuditor
     -> low risk 自动通过
     -> high risk 要用户批准 scope
     -> forbidden/unknown 阻止
  -> ScheduleStore 保存 draft
```

批准流程：

```text
schedule_approve_agent(schedule_id, capabilities)
  -> merge_approved_capabilities()
  -> ScheduleStore 标记 active
```

执行流程：

```text
scheduler_worker.run_schedule()
  -> ScheduledAgentRunner.run()
  -> 创建 task session
  -> session.metadata["kind"] = "scheduled_agent"
  -> session.metadata["approved_capabilities"] = [...]
  -> Pipeline.run(AUTOMATION_PROFILE)
  -> ToolApprovalPolicyHook 检查每个工具调用
  -> 写 report / trace / task artifacts
  -> Telegram outbox 推送
```

讲解重点：

> 自动任务不是假装用户又发了一条消息，而是创建内部 scheduled_agent task session。它有独立 metadata、预算限制和工具批准策略。

## 19. Telegram 流程

重点文件：

- `telegram_worker.py`
- `gateway/telegram/adapter.py`
- `gateway/telegram/client.py`
- `gateway/telegram/store.py`
- `gateway/telegram/storage.py`
- `scheduler_worker.py`

### 19.1 普通聊天

```text
Telegram getUpdates
  -> handle_update()
  -> 校验 private chat
  -> TelegramIdentityResolver.resolve()
  -> runtime_chat_id()
  -> runtime.submit_user_message(channel="telegram")
  -> runtime.run_once()
  -> outbound channel="telegram"
  -> TelegramGateway.send()
  -> Bot API sendMessage
```

### 19.2 Telegram 文件命令

命令：

```text
/files
/cat <path>
/download <path>
```

流程：

```text
handle_update()
  -> command == "/files"
  -> list_storage_text(identity.user_id, path)
```

这些命令不进入 LLM，直接由 gateway 处理。好处：

- 快。
- 可控。
- 文件下载不需要模型参与。

### 19.3 定时任务推送

定时任务完成后：

```text
TelegramScheduleNotifier.notify()
  -> TelegramGatewayStore.enqueue_message()
  -> TelegramGatewayStore.enqueue_document()
```

Telegram worker：

```text
flush_outbox()
  -> list_pending_messages()
  -> send_message 或 send_document
  -> mark_sent()
```

为什么要 outbox：

- scheduler worker 和 telegram worker 是两个进程。
- outbox 用 SQLite 做进程间缓冲。
- Telegram 临时失败时可以重试。

## 20. 前端功能流程

重点文件：

- `web/static/index.html`
- `web/static/app.js`
- `web/static/styles.css`
- `web/static/login.html`
- `web/static/login.js`
- `web/server.py`

### 20.1 聊天流式输出

前端：

```text
app.js
  -> fetch("/api/chat/stream")
  -> 读取 NDJSON event
  -> delta event 追加到当前 assistant bubble
  -> complete event 保存最终消息
```

后端：

```text
_handle_chat_stream()
  -> _send_stream_headers()
  -> AgentService.ask_stream(on_text=send_delta)
  -> send complete
```

Pipeline：

```text
provider.stream_chat(on_text=...)
```

### 20.2 工具调用折叠

前端会把 assistant/tool 消息中的工具请求和工具结果默认折叠。这样普通用户先看到答案，想看细节再展开。

讲解点：

> 工具 trace 对调试重要，但直接展示会干扰阅读。所以 UI 上默认折叠，保留可观察性。

### 20.3 会话删除

```text
DELETE /api/session
  -> _web_storage_id() 校验当前用户拥有该 web session
  -> AgentService.delete_session()
  -> SessionManager.delete()
  -> SessionStore.delete_session()
```

### 20.4 文件预览弹窗

```text
GET /api/files/preview?path=...
  -> _safe_storage_path()
  -> 读取前 MAX_PREVIEW_BYTES
  -> 返回 text / metadata
```

前端用弹窗展示，不固定在页面底部。

## 21. 安全设计

### 21.1 权限边界

```text
admin
  -> Coding
  -> bash / write_file / edit_file
  -> 高风险定时任务批准

user
  -> Bot / Hybrid 受限能力
  -> 自己的 storage / memory
```

### 21.2 文件边界

```text
workspace 写入:
  -> FileWriteScopeHook 限制不得逃出 WORKDIR

storage 写入:
  -> storage_root_for_user()
  -> _safe_storage_path()

task sandbox:
  -> session scoped 临时目录
```

### 21.3 工具边界

```text
ToolRegistry.enabled_for(mode, session)
ToolRegistry.visible_names_for_turn(session, mode)
ToolExecutor hooks
```

### 21.4 自动任务边界

```text
planner 提议工具
  -> auditor 审计
  -> 用户批准 scope
  -> ToolApprovalPolicyHook 执行时检查
```

## 22. 测试怎么讲

测试目录：`tests/`

重点测试：

- `test_hybrid_mode_routing.py`：Hybrid 路由和 LLM 判别。
- `test_pipeline_tool_loop_guard.py`：工具循环保护。
- `test_model_pool_routing.py`：多模型路由和 fallback。
- `test_scheduler_plugin.py`：定时任务和 Telegram 推送。
- `test_multi_user_isolation.py`：多用户隔离。
- `test_web_streaming.py`：Web 流式输出。
- `test_markdown_pdf_plugin.py`：Markdown 转 PDF。
- `test_bot_storage_tools.py`：Bot storage 工具。

可以展示运行：

```bash
python -m unittest discover -s tests -v
```

## 23. 课堂演示脚本

### 23.1 先启动

```bash
python web/server.py --host 127.0.0.1 --port 8000
```

打开：

```text
http://127.0.0.1:8000
```

### 23.2 演示登录和会话

讲：

> 登录后每个用户有自己的 session、memory、storage。这里是为了公网部署时多用户隔离。

可以展示：

- 左侧会话列表。
- 新建会话。
- 删除会话。

### 23.3 演示 Hybrid

输入：

```text
/hybrid
```

再输入：

```text
帮我总结一下这个项目的功能
```

讲：

> 这是普通聊天，不需要代码工具，所以留在 Bot。

再输入：

```text
请帮我检查 core/pipeline.py 里工具循环保护的逻辑
```

讲：

> 这个请求涉及项目文件和代码分析，Hybrid 会把它识别成 coding candidate，再交给 LLM 判断，最后可能进入 TaskSession。

### 23.4 演示记忆

输入：

```text
请记住，我喜欢回答直接一点，少写客套话。
```

再输入：

```text
你记得我的回答偏好吗？
```

讲：

> 第一条会被 MemoryLifecycle 识别为显式记忆，写入当前用户 memory。第二条会让模型使用 recall_memory。

### 23.5 演示文件生成

输入：

```text
帮我生成一份 taleclaw 项目介绍，保存成 markdown 文件。
```

讲：

> Bot 模式不会写项目源码，它只能写当前用户 storage 或 sandbox。生成后可以在前端文件区预览。

### 23.6 演示搜索

输入：

```text
帮我搜索最新 AI Agent 新闻，整理成三条要点，并附上来源链接。
```

讲：

> 搜索能力来自 WebSearchPlugin。它注册了 web_search 工具，Pipeline 只是统一调度工具，不关心插件内部怎么实现。

### 23.7 演示定时任务

输入：

```text
每天早上 8 点搜索最新 AI 资讯，生成一份带分析的日报。
```

讲：

> 这里模型会使用 scheduler 插件工具创建 workflow，配置写入 SQLite。真正执行由 scheduler_worker 进程负责。

### 23.8 演示代码导航

按顺序打开：

1. `core/bootstrap.py`
2. `core/agent_loop.py`
3. `modes/router.py`
4. `core/pipeline.py`
5. `tools/tool_registry.py`
6. `tools/executor.py`
7. `memory/lifecycle.py`
8. `plugins/scheduler/plugin.py`
9. `core/model_pool.py`

每个文件一句话：

```text
bootstrap.py: 系统装配入口。
agent_loop.py: 一轮消息主流程。
router.py: 决定走 Bot、Code 还是 Hybrid。
pipeline.py: LLM 推理和工具调用循环。
tool_registry.py: 工具注册、权限、可见性。
executor.py: 执行工具和安全 hooks。
lifecycle.py: 每轮结束后更新记忆。
scheduler/plugin.py: 插件如何扩展 agent 能力。
model_pool.py: 多模型 provider 池和用途路由。
```

## 24. 老师可能会问的问题

### Q1：你这个项目用了什么框架？

答：

> 后端主体没有使用 FastAPI/Flask，而是 Python 标准库 HTTP server + asyncio runtime。Agent 部分是自己实现的轻量框架，包括 MessageBus、AgentLoop、Pipeline、ToolRegistry、PluginManager 和 MemoryLifecycle。模型调用使用 OpenAI-compatible API。定时任务使用 APScheduler。前端是原生 HTML/CSS/JS。

### Q2：为什么不用 LangChain？

答：

> 这个项目主要是为了理解和控制 Agent 内部机制，所以自己实现了 tool loop、权限、记忆、插件、定时任务。LangChain 更适合快速拼链路，但这里我想对每一步可见性、工具权限、记忆写入和多用户隔离有更细粒度控制。

### Q3：怎么避免模型乱调用工具？

答：

> 第一层是 ToolRegistry 区分 mode 和 visible tools；第二层是 `tool_search` 延迟解锁高风险工具；第三层是 ToolExecutor hooks，比如文件写入范围限制和重复工具调用保护；第四层是 Pipeline 的 reasoning step 上限和不可见工具循环保护。

### Q4：记忆会不会越来越大？

答：

> 当前设计是用户原文完整保存，助手回复先总结。RECENT_CONTEXT 只保留最近窗口，旧内容进入 archive。后续可以接向量数据库做检索式记忆。

### Q5：定时任务为什么不直接调用搜索函数？

答：

> 简单 workflow 可以直接搜索和分析；更复杂的 autonomous agent 会先由 planner 规划工具，再由 auditor 审计权限，执行时还会用 ToolApprovalPolicyHook 检查，避免无人值守任务越权。

### Q6：多用户隔离怎么保证？

答：

> 登录态会映射成 `user_id/user_role`，进入 AgentLoop 后写到 session metadata。memory 和 storage 都通过 `user_scope.py` 解析到 `.users/<user_id>/...`。普通用户不能进入 Coding，也不能拿到 admin-only 工具。

### Q7：如果模型不可用怎么办？

答：

> `core/model_pool.py` 支持按用途配置 provider 和 fallback，比如总结优先 MiMo，失败后回 DeepSeek。流式输出时，如果已经开始输出文字，就不 fallback，避免用户看到重复内容。

## 25. 1 分钟压缩版总结

> taleclaw 的核心是一个自己实现的 AI Agent Runtime。Web、CLI、Telegram 都把消息交给 MessageBus，AgentLoop 负责一轮消息调度，ModeRouter 判断 Bot/Code/Hybrid，Pipeline 负责模型推理和工具循环。工具由 ToolRegistry 管理可见性和权限，由 ToolExecutor 运行安全 hooks。每轮结束后 MemoryLifecycle 更新用户隔离的长期记忆。插件系统扩展了 web search、markdown pdf、scheduler 等能力。定时任务由 scheduler_worker 独立执行，并且支持权限审计和 Telegram 推送。最近还加入了 ModelPool，可以按聊天、代码、总结、定时分析等用途选择不同模型并 fallback。
