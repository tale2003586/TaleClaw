# 自研 LLM Agent Runtime 逐题答辩稿

本文针对“自研 LLM Agent Runtime 细节追问”逐题作答，答案基于当前仓库实现，而不是抽象架构话术。重点代码入口：

- `runtime/agent_loop.py`：一条 inbound 消息如何变成一次 run。
- `runtime/pipeline.py`：turn 级上下文、工具目录、记忆生命周期编排。
- `runtime/reasoning_loop.py`：模型调用、工具调用、停止条件和 trace。
- `tools/tool_registry.py`、`tools/policy.py`、`tools/executor.py`、`tools/hooks.py`：工具注册、可见性、执行和护栏。
- `runtime/workspace.py`：workspace 解析与路径安全。
- `models/model_pool.py`、`models/provider.py`：模型路由、fallback、streaming provider。
- `runtime/trace/trace_store.py`：trace、metrics、report 工件。

## 0. 总括回答

这个 Runtime 的定位不是“封装一次 LLM API 调用”，而是一套围绕多入口消息、会话身份、模型路由、工具治理、记忆生命周期、trace 证据和 coding task session 组织起来的 Agent Runtime。它和 LangChain/LangGraph/Dify/Coze 的差异在于，我把工程里最容易出事故的部分做成了自己的控制面：入口身份、workspace 边界、工具可见性、hook 拒绝、上下文压缩、run 工件和 benchmark 复盘。

如果面试时需要一句话开场，可以说：

> 我自研 Runtime 不是为了重复造框架，而是因为我的核心目标是做一个多入口、可追踪、可评测、带工具安全边界的 coding/chat hybrid agent。现成框架能帮我调模型和串工具，但很难把 session 身份、workspace 写权限、工具 hook、上下文压缩证据和 benchmark trace 统一到同一条执行链里，所以我把这些做成了 Runtime 的一等对象。

## 1. 这个 Runtime 和直接用 LangChain / LangGraph / Dify / Coze 的区别是什么？为什么要自研？

区别主要在控制边界。

LangChain/LangGraph 更偏模型与工具编排，Dify/Coze 更偏产品化 Bot 平台。我的 Runtime 更关注“运行时治理”：一条消息从 CLI/Web/Telegram/飞书进来后，如何映射到 session、如何路由到 bot/coding、模型能看到哪些上下文、能调用哪些工具、工具是否能执行、是否越权写文件、执行过程怎么落 trace、失败后怎么复盘。

自研的原因有三点：

1. Coding agent 对 workspace 安全要求很高。当前代码里 `runtime/workspace.py` 和 `tools/hooks.py` 都在执行前做路径 resolve、workspace 范围校验和 shell 安全拦截，这些不能只靠 prompt。
2. 我需要 run 级证据链。`TraceStore` 会写 `trace.jsonl`、`metrics.json`、`context_metrics.json`、`report.json` 和 `trace_summary.*`，用于 benchmark 和线上复盘。
3. 我需要多入口共享同一套 Runtime。`InboundMessage` / `OutboundMessage` 把 Web、Telegram、CLI 的差异收敛到统一消息协议，而不是每个入口各写一套 agent loop。

代码依据：

- `bus/events.py:16-28` 定义统一 inbound message 和 `session_key`。
- `runtime/agent_loop.py:50-70` 是一轮消息的主编排。
- `runtime/trace/trace_store.py:52-84` 写结构化 trace，并旁路派发实时事件。

## 2. 支持 CLI / Web / Telegram 多入口，这几个入口进入 Runtime 后，内部是怎么统一抽象的？

所有入口最终都会转成 `InboundMessage`：

```python
InboundMessage(
    channel="web" | "telegram" | "cli",
    chat_id="入口侧会话 id",
    sender="入口侧发送者",
    content="用户文本",
    media=[...],
    metadata={...},
)
```

Runtime 不直接关心 Telegram update、HTTP request 或 CLI stdin。入口层负责适配，Runtime 只消费统一的 inbound message，并通过 `OutboundMessage(channel, chat_id, content, ...)` 发回入口。

代码依据：

- `bus/events.py:16-39` 定义 `InboundMessage` 和 `OutboundMessage`。
- `runtime/app_runtime.py` 中 `submit_user_message()` / `run_message()` 把外部消息包装成 `InboundMessage`。
- `gateway/telegram/adapter.py` 会把 Telegram 私聊文本映射成 `runtime.submit_user_message(...)`。
- `web/server.py` 的 `AgentService._ask_async()` 会把 Web 请求映射成 channel=`web` 的 runtime message。

## 3. 多入口场景下，会话状态怎么管理？不同入口的 session id、user id、conversation id 怎么映射？

内部 session key 是：

```text
<channel>:<chat_id>
```

`InboundMessage.session_key` 直接返回 `f"{channel}:{chat_id}"`，所以 Telegram 和 Web 即使外部 chat id 一样，也不会撞到同一个 session。

Web 入口进一步把用户 id 编进 chat id：`web:<user_id>:<chat_id>`。Telegram 入口会用 `TelegramIdentityResolver` 把外部 Telegram user id 映射为内部 `user_id` 和 `role`，再把 conversation id 编进 runtime chat id。这样有三层：

- `channel`：入口来源，例如 `web`、`telegram`。
- `chat_id` / conversation id：入口侧会话或当前对话。
- `metadata.user_id` / `metadata.user_role`：用户身份和权限。

`AgentLoop._apply_inbound_identity()` 会把 inbound metadata 中的身份写入 session。如果一个已存在 session 绑定了 `user_id=A`，后续 inbound 带 `user_id=B`，会抛 `ValueError`，避免会话串用户。

代码依据：

- `bus/events.py:26-28`：`session_key = channel:chat_id`。
- `runtime/agent_loop.py:55`：`sessions.get_or_create(inbound.session_key)`。
- `runtime/agent_loop.py:91-93`、`runtime/agent_loop.py:260+`：接收阶段应用身份并创建 run。
- `user_scope.py`：规范化 `user_id`、`chat_id`，并生成 Web session id。

## 4. “统一管理会话、模型路由、工具执行、记忆生命周期”，这几个模块之间的调用顺序是什么？

主顺序在 `AgentLoop.run_inbound()`：

1. `SessionManager.get_or_create(inbound.session_key)` 获取 session。
2. `_receive()` 应用用户身份并创建 `RunState`。
3. `plugin_manager.before_turn()` 做 turn 前拦截。
4. `ModeRouter.route()` 选择 bot/coding/hybrid profile。
5. `_record()` 把用户消息写入 session。
6. `_execute()` 根据 profile 进入普通 `Pipeline.run()` 或 coding `TaskSessionRunner.run_coding_task()`。
7. `Pipeline` 触发 `AgentRunner` / `ReasoningLoop`，内部做模型路由、上下文构建、模型调用、工具执行。
8. `Pipeline._after_turn()` 触发 memory lifecycle。
9. `AgentLoop._deliver()` 完成 run、保存 session、发布 outbound。

可以概括为：

```text
session -> identity/run -> plugin -> route -> record -> context/model/tool loop -> memory -> trace/report -> outbound
```

代码依据：

- `runtime/agent_loop.py:50-70`：主调用顺序。
- `runtime/pipeline.py:110-149`：turn 级编排。
- `runtime/reasoning_loop.py:106-247`：reasoning step 内部上下文和模型调用。

## 5. “单轮执行拆成五个阶段”，五个阶段分别是什么？每个阶段解决什么问题？

我会把它表述成五阶段：

1. 接入与身份阶段：把不同入口统一成 `InboundMessage`，拿到 session，校验 `user_id/user_role`。解决多入口和多用户隔离问题。
2. 路由阶段：`ModeRouter` 决定本轮是普通聊天、coding 任务还是模式切换。解决“同一句话走哪套 profile 和工具模式”的问题。
3. 上下文准备阶段：`Pipeline` reset turn 状态，`ContextBuilder` 构建 system/history/memory/RAG/working memory，随后注入 `<tool_catalog>`。解决模型该看什么的问题。
4. 推理执行阶段：`ReasoningLoop` 做模型调用、工具调用、工具结果写回、loop guard、token gate 和 trace。解决模型如何安全推进任务的问题。
5. 收尾沉淀阶段：memory lifecycle、session 保存、run state/report/metrics/context metrics/trace summary 写入，outbound 回复入口。解决可复盘和长期记忆的问题。

代码依据：

- `runtime/agent_loop.py:50-70` 对应接入、路由、执行和收尾。
- `runtime/pipeline.py:151-158` reset turn 状态。
- `runtime/reasoning_loop.py:220-247` context build 后进入模型调用。
- `runtime/pipeline.py:258+` 的 `_after_turn()` 触发记忆生命周期。

## 6. 为什么要把生命周期拆阶段？如果不拆，会出现什么工程问题？

拆阶段是为了让每个风险点都有独立边界和证据。

如果不拆，常见问题包括：

- 插件命令、普通聊天、coding 任务混在一起，`/status` 这种命令也会污染对话历史。
- 工具可见性和工具执行混在 prompt 里，模型可能看见不该看的写文件或 shell 工具。
- 上下文压缩和模型调用耦合，provider 报错时很难知道模型实际看到了哪些消息。
- 记忆写回阻塞主回复，用户会感到慢，而且后台失败会影响主回答。
- trace 只能事后猜，无法定位失败发生在路由、上下文、模型、工具还是记忆阶段。

当前代码中，`plugin_manager.before_turn()` 在用户消息写入 session 之前执行；如果插件 abort，这轮不会进入普通对话历史。这就是阶段化带来的直接收益。

代码依据：

- `runtime/agent_loop.py:95-122`：插件提前拦截，并以 `plugin_abort` 完成 run。
- `runtime/pipeline.py:121-149`：turn 前状态和 reasoning loop 分离。
- `runtime/trace/trace_store.py:94-123`：run 结束后统一写报告和指标。

## 7. “上下文构建、工具目录注入、RAG 自动注入、工具 hook、记忆写回”，这些节点发生在模型调用前还是模型调用后？

发生位置如下：

| 节点 | 发生时间 | 说明 |
| --- | --- | --- |
| 上下文构建 | 模型调用前 | `ReasoningLoop` 每个 step 调 `build_context`。 |
| RAG 自动注入 | 模型调用前 | `Pipeline._before_reasoning()` 传入 `include_security_knowledge`，同一用户 turn 只注入一次。 |
| 工具目录注入 | 模型调用前 | `Pipeline._with_tool_catalog()` 把 `<tool_catalog>` 插到 active turn 前。 |
| provider sanitize / token gate | 模型调用前 | `ReasoningLoop._reasoning_step()` 里 sanitize、估算 token、必要时 emergency trim。 |
| 工具 hook | 模型返回 tool calls 后、真实 handler 前后 | `ToolExecutor.execute()` 先跑 pre hooks，再执行 handler，再跑 after hooks。 |
| 记忆写回 | 本轮结束后 | `Pipeline._after_turn()` 调 memory lifecycle，默认可后台执行。 |

代码依据：

- `runtime/pipeline.py:178-223`：构建上下文并控制安全 RAG 自动注入。
- `runtime/pipeline.py:225-247`：插入工具目录。
- `runtime/reasoning_loop.py:485-549`：sanitize、token gate、emergency trim。
- `tools/executor.py:73-152`：工具 hook 执行边界。
- `runtime/pipeline.py:258+`：turn 后记忆生命周期。

## 8. 工具目录如何注入 prompt？全量注入所有工具，还是动态选择部分工具？

不是全量注入所有工具。

当前分两层：

1. 真正传给模型的 function schema 来自 `ToolRegistry.schemas_for_turn(session, mode)`，只包含本 turn 可见工具。
2. prompt 中插入的 `<tool_catalog>` 是轻量目录，列出 `Visible now` 和 `Available after unlock`。它告诉模型有哪些工具可直接用，哪些需要 `tool_search select:<tool_name>` 解锁。

这样能降低 prompt 噪声，也能防止模型一开始就看到所有高风险工具的完整 schema。

代码依据：

- `tools/tool_registry.py:125-134`：只返回 visible tool schema。
- `tools/tool_registry.py:136-162`：生成 `<tool_catalog>`。
- `runtime/pipeline.py:225-247`：把 tool catalog 插进 context messages。

## 9. 工具可见性控制具体怎么做？普通聊天为什么不应该暴露写文件工具？

可见性由 `ToolPolicy.visible_tools()` 决定。它把几类工具合并：

- always-on 工具，例如 `recall_memory`、`memorize`、`tool_search`。
- registry 中标记 `always_on=True` 的工具。
- 当前 mode 的预加载工具，例如 bot/coding/teammate。
- 当前 turn 通过 `tool_search select:<tool>` 解锁的工具。

最后再和 `enabled_for(mode, session)` 求交集。

普通聊天不应该暴露 `write_file`、`edit_file`、通用 `bash`，因为普通聊天的用户意图通常是问答、检索、生成文本。如果把 workspace 写权限暴露给普通聊天，模型可能因为误判或 prompt 注入修改代码或执行 shell。当前 bot 模式只预加载 storage/sandbox 这类受限工具，coding 模式才预加载 workspace 读写和 bash。

代码依据：

- `tools/policy.py:7-11`：always-on 工具。
- `tools/policy.py:13-78`：不同 mode 的预加载工具。
- `tools/policy.py:112-132`：本 turn 可见工具计算。
- `tools/tool_registry.py:57-63`：`enabled_for()` 支持 mode 和 admin-only。

## 10. ToolRegistry、ToolPolicy、ToolExecutor 分别负责什么？边界怎么划分？

三者边界如下：

- `ToolRegistry`：注册工具。保存工具名、schema、handler、risk、enabled_modes、source、always_on、session_scoped、admin_only。也负责按 turn 输出 schema 和轻量 catalog。
- `ToolPolicy`：做决策。决定某个 session/mode 下哪些工具可见，某个 tool 是否能执行。
- `ToolExecutor`：做执行边界。它不关心工具业务逻辑，只负责执行前后 hook、拒绝、缓存回放、异常包装、耗时和 hook trace。

这三个分开后，模型只能“请求工具”，不能直接绕过 policy 和 hook 执行 handler。

代码依据：

- `tools/tool_registry.py:45-94`：`ToolSpec` 和注册。
- `tools/policy.py:106-176`：可见性和可执行性判断。
- `tools/executor.py:73-152`：执行 pre hooks、handler、after hooks 和错误包装。

## 11. 工具调用前的权限裁决有哪些规则？路径、写文件、shell 命令、网络访问分别怎么限制？

当前主要规则：

1. mode 可见性：工具必须在当前 profile 的 `tool_mode` 下 enabled，并且本 turn visible。
2. admin-only：某些工具只有 `metadata.user_role == "admin"` 的 session 可用。
3. workspace 路径：workspace 工具使用 session 中的 `workspace_root`，路径必须是相对路径，不能 `..` 逃逸。
4. 写文件：`FileWriteScopeHook` 对 `write_file`、`edit_file` 做 workspace resolve 校验。
5. shell：`ShellSafetyHook` 拦截 sudo、shutdown、mkfs、危险 rm 等模式；`ShellWorkspaceScopeHook` 拦截 `cd` 到 workspace 外。
6. 网络：当前主要通过工具可见性控制，例如 `web_search` 是插件工具，并有每 turn 预算；还没有完整的 egress policy 或域名级网络审批。

代码依据：

- `tools/policy.py:134-176`：工具执行前 policy 判断。
- `tools/hooks.py:13-35`：危险 shell 命令拦截。
- `tools/hooks.py:38-65`：shell `cd` workspace 边界。
- `tools/hooks.py:68-94`：写文件 workspace 边界。
- `runtime/reasoning_loop.py` 中 `web_search` 每 turn budget 逻辑。

## 12. 如何防止 Agent 写出工作区之外的文件？有没有做 path canonicalize / resolve？

有。主要有两层。

第一层在 workspace 工具本身：`safe_workspace_path()` 要求 path 不能是绝对路径，不能包含 `..`，并把 `(root / relative).resolve()` 后检查是否仍然在 root 下。

第二层在 hook：`FileWriteScopeHook` 对 `write_file` 和 `edit_file` 再做一次 `(workspace / raw_path).resolve()`，并检查 `target.is_relative_to(workspace)`。即使 handler 层漏了，执行前 hook 仍会拒绝。

此外 `WorkspaceResolver` 解析 workspace 本身时，也会检查 requested workspace 必须在 configured `allowed_roots` 下面。

代码依据：

- `runtime/workspace.py:31-93`：workspace resolve 和 allowed roots。
- `runtime/workspace.py:104-117`：`safe_workspace_path()`。
- `tools/hooks.py:68-94`：`FileWriteScopeHook`。

## 13. 如果模型连续重复调用同一个工具，怎么处理？有限流、去重或失败熔断吗？

有。`ToolLoopGuardHook` 做多层保护：

- 完全相同的 `tool + arguments` 指纹重复达到阈值会拒绝。
- 同一个工具在窗口内使用过频会拒绝。
- 对 read/search/list 类工具，相同结果 hash 多次出现会判定为 no-information-gain 并拒绝。
- 对可缓存的读类工具，第一次重复会直接回放缓存并附加警告，再重复就拒绝。
- 每个 turn 开始会 reset guard 状态，避免跨 turn 污染。

`ReasoningLoop` 发现 loop guard denial 后，会触发 reflection 或直接保护性停止。模型连续请求不可见工具也有限制，`MAX_UNAVAILABLE_TOOL_ATTEMPTS = 2`。

代码依据：

- `tools/hooks.py:97-247`：`ToolLoopGuardHook`。
- `runtime/reasoning_loop.py:387-421`：loop guard 触发后的停止或 reflection。
- `runtime/reasoning_loop.py:423-442`：不可用工具重复请求保护。

## 14. 工具调用失败后，是直接把错误返回给模型，还是结构化包装？错误信息会不会泄露系统路径或密钥？

不是裸抛异常。工具执行统一返回 `ToolExecutionResult`，包含：

- `status`：`success` / `denied` / `error`
- `output`
- `final_arguments`
- `duration_ms`
- `error_type`
- `error_message`
- `metadata`
- `pre_hook_trace`
- `post_hook_trace`

这个结果会写入 session 的 tool message，也会进入 trace 的工具事件。hook 拒绝会标记为 `denied`，handler 异常会标记为 `error`。

密钥方面，trace 写入前有 `_json_safe()`，会按 key 过滤常见 `token/password/secret/api_key/authorization/access_token/refresh_token`。但要诚实地说：这不是完整 DLP；如果某个工具把绝对路径或敏感字符串拼在普通错误文本里，仍可能进入错误预览。当前已做 preview 截断和 secret key 过滤，后续可以在 `ToolExecutor` 或 trace 层增加统一 error sanitizer。

代码依据：

- `tools/executor.py:36-47`：结构化 `ToolExecutionResult`。
- `tools/executor.py:91-108`：hook deny 包装。
- `tools/executor.py:132-152`：handler 异常包装。
- `runtime/trace/trace_store.py:64-84`：事件写入和 `_json_safe()` 清洗。

## 15. “插件和工具按运行阶段插入，而不是堆叠在 prompt 中”，这句话怎么理解？插件到底是代码层扩展，还是 prompt 片段？

插件是代码层扩展，不只是 prompt 片段。

插件可以在不同阶段接入：

- 注册工具：例如 web search、security RAG、markdown PDF。
- 注册 tool hooks：例如 shell safety。
- `before_turn` 拦截：例如 `/status`、`/plugins`。
- `after_turn` 后处理。
- `after_run` 生成 run report。
- `after_eval` 生成 eval report。
- 向 context builder 注册 runtime guidance。

所以“按运行阶段插入”的意思是：插件不只是把一大段提示词塞进 system prompt，而是在 Runtime 生命周期的明确节点执行代码逻辑。提示词只是插件能力的一种表现形式。

代码依据：

- `plugins/plugin_manager.py`：注册插件、工具、hook、before/after turn。
- `plugins/status_commands/plugin.py`：`before_turn` 拦截命令。
- `plugins/security_rag/plugin.py`：注册 `security_rag_search` 和 RAG trace。
- `plugins/run_report/plugin.py`：`after_run` 写人类报告。

## 16. 模型路由怎么设计？什么情况下走不同模型？按任务类型、成本、上下文长度还是用户配置？

当前路由主要按 purpose，而不是动态成本优化。

已有 purpose 包括：

- `chat`
- `coding`
- `summary`
- `hybrid`
- `compact`
- `teammate`
- `reflection`
- `task_conclusion`

主 `Pipeline` 根据 profile 的 `tool_mode` 决定 purpose：coding profile 走 `coding`，否则走 `chat`。summary、history summarizer、task conclusion、security router classifier 这类一次性任务用 `ModelTaskRunner` 指定 purpose。

模型池按环境变量配置 route chain 和 fallback。失败时 `RoutedModelProvider` 会按 chain 尝试 fallback，并维护 profile 级失败计数和 cooldown。现在还没有按成本、延迟、上下文长度自动动态选择模型；这些是后续可以加在 `ModelPool` 上的策略层。

代码依据：

- `runtime/pipeline.py:249-260`：构造 `AgentSpec` 并决定 `model_purpose`。
- `models/model_pool.py`：purpose route、fallback chain、health cooldown。
- `models/model_task_runner.py`：一次性模型任务。

## 17. Runtime 如何支持流式输出？SSE / WebSocket / 轮询分别有没有考虑？

模型层有 `OpenAICompatibleProvider.stream_chat()`，它用 OpenAI-compatible streaming API，收到文本 delta 后调用 `on_text(text)`，同时能拼回完整 content 和流式 tool_call arguments。

Web 层现在使用 `POST /api/chat/stream` 返回 NDJSON 流，事件类型包括：

- `delta`：文本增量。
- `event`：trace activity 投影，例如 tool/model/subagent/workspace diff。
- `complete`：最终回复和 session。
- `error`：异常。

实现上更像 SSE/NDJSON，而不是 WebSocket。选择 NDJSON 的原因是实现简单、和现有 HTTP server 兼容、Nginx 只要关闭 buffering 即可。WebSocket 后续可以复用同一套 `TraceStore.subscribe()` 和 `_stream_event_projection()`，不需要改 Runtime 主链路。轮询适合 run 详情页读取已有 trace，但不适合 token 级实时输出。

代码依据：

- `models/provider.py:105-188`：`stream_chat()`。
- `runtime/reasoning_loop.py:479-483`：有 `on_text` 时优先使用 `stream_chat`。
- `web/server.py` 的 `/api/chat/stream`：HTTP NDJSON stream。
- `runtime/trace/trace_store.py:45-50`：按 session 订阅 trace 事件。

## 18. `trace.jsonl` 每条事件结构是什么？有哪些字段？

每行是一个 JSON 事件，核心字段：

```json
{
  "timestamp": "...",
  "run_id": "...",
  "session_id": "...",
  "request_id": "...",
  "event": "tool.call.completed",
  "span_id": "...",
  "parent_span_id": "...",
  "step": 3,
  "payload": {}
}
```

其中 `span_id` / `parent_span_id` 用于把 model/tool/context/subagent 事件挂到 reasoning step 下；`step` 表示第几轮 reasoning step；`payload` 放具体事件数据，例如模型、token、工具名、参数预览、输出预览、hook trace、错误类型等。

代码依据：

- `runtime/trace/trace_store.py:52-84`：构造并写入 trace event。
- `runtime/reasoning_loop.py`：写 `reasoning.step.*`、`context.build.*`、`model.call.*`、`tool.call.*`。

## 19. metrics 具体统计哪些指标？token、耗时、工具次数、失败率、压缩率有没有？

有。`metrics.json` 从 `trace.jsonl` 聚合，主要包括：

- run id、session id、status、run duration。
- reasoning steps。
- model calls、model failures、model route attempts、model retry count。
- input/output/total tokens。
- total model duration、total tool duration。
- tool calls、tool failures、tool denials。
- duplicate tool call count/ratio。
- truncated tool output count。
- sanitized messages。
- subagent fanout/incomplete count。
- context builds、compressed context build count、context build duration。
- models、tools 列表。

上下文压缩更细的指标在 `context_metrics.json`：每次 context build 的 section reduction、token emergency trim、coding context state generation、压缩前后 token 等。

代码依据：

- `runtime/trace/trace_store.py:171-300`：metrics 聚合字段。
- `runtime/trace/context_metrics.py`：context metrics 聚合。

## 20. report 和 trace 的区别是什么？report 是给用户看，还是给开发者复盘看的？

`trace.jsonl` 是原始证据，适合机器分析和深度排障；`report.json` 是机器可读 run 汇总；`report.md` 是插件生成的人类可读报告，主要给开发者或评测复盘看，不是普通终端用户的最终回答。

可以这样理解：

- trace：逐事件事实日志，最完整。
- metrics：从 trace 聚合出的指标。
- report.json：run 状态和报告 payload。
- trace_summary.json/md：把 trace 压成执行路径、失败线索、工具链、workspace 影响。
- report.md：面向人读的复盘报告，由插件生成。

代码依据：

- `runtime/trace/trace_store.py:94-123`：写 report、metrics、context metrics、trace summary。
- `plugins/run_report/plugin.py`：生成人类可读 `report.md`。

## 21. “用于分析上下文压缩影响”，怎么判断一次任务失败是因为上下文压缩导致的？

我不会只凭最终回答判断。判断链路是：

1. 看 `context.build.completed` 事件里的 `context_report.reductions`，确认哪一段被裁剪。
2. 看 `context_metrics.json`，确认是否触发 section budget、active turn compression、coding context state 或 emergency trim。
3. 看失败前后的行为：是否开始重复读取、找不到刚读过的文件、误用旧结论、调用 `retrieve_tool_result` 失败。
4. 看 long tool result 是否已进入 tool result store，压缩文本里是否保留 `tool_result_ref`。
5. 做 ablation：同任务同模型关闭或放宽压缩，或提高 active turn budget，看失败是否消失。

只有当“压缩发生在关键证据所在 section”且“失败行为与丢证据一致”，并且 ablation 改善，才会归因到上下文压缩。

代码依据：

- `runtime/reasoning_loop.py:220-231`：`context.build.completed` payload 带 report 和 metrics。
- `runtime/reasoning_loop.py:501-529`：超 token 后写 `context_emergency_trim`。
- `tools/hooks.py:300-346`：长工具输出写入 tool result store，并保留引用。

## 22. 316 个公开测试主要覆盖哪些模块？单元测试、集成测试、端到端测试各有多少？

简历里可以说“当时公开测试 316 个，后来继续扩展”。当前我在仓库中用：

```bash
PYTHONPATH=. pytest --collect-only -q
```

收集到的是 `411 tests collected`。

覆盖模块大致包括：

- Runtime 主循环：`test_agent_loop_phases.py`、`test_run_trace.py`、`test_pipeline_tool_loop_guard.py`。
- 工具安全：`test_tool_safety.py`、`test_git_tools.py`、`test_workspace_resolver.py`、`test_bot_storage_tools.py`、`test_bot_sandbox_tools.py`。
- 上下文和压缩：`test_context_budget.py`、`test_context_instructions.py`、`test_token_estimator.py`、`test_coding_context_state.py`。
- 模型路由：`test_model_pool_routing.py`、`test_model_task_runner.py`。
- 记忆：`test_memory_lifecycle_archive.py`、`test_memory_recall.py`、`test_memory_scope.py`。
- RAG：`test_security_router.py`、`test_security_rag_hybrid.py`、`test_security_rag_observability.py`。
- 多入口：`test_web_streaming.py`、`test_web_auth.py`、`test_telegram_gateway.py`、`test_feishu_gateway.py`。
- 多用户隔离：`test_multi_user_isolation.py`。
- coding benchmark / SWE-bench：`test_coding_benchmark.py`、`test_swebench_adapter.py`、`test_swebench_verified_matrix.py`。
- subagent / orchestration：`test_subagent_runner.py`、`test_subagent_tools.py`、`test_parallel_tasks.py`。

单元/集成/端到端没有用 pytest marker 严格标注。如果按文件语义粗分，可以说：大多数是模块单测；几十个是 Runtime/网关/trace 集成测试；benchmark、script、SWE-bench adapter 相关是脚本级或端到端测试。

## 23. 怎么 mock LLM 调用？测试时会真的调 API 吗？

默认不真调 API。

测试里大量使用 `FakeProvider`、`ScriptedProvider` 或 `FakeModelPool`，直接返回内部统一的 `LLMResponse(content, tool_calls, raw_message, usage)`。coding benchmark 的 scripted runner 会按任务脚本返回固定 tool call 或 final answer，用来验证 Runtime、工具、trace、verifier，而不是测试模型能力。

真实模型只在显式选择 real runner 时调用，例如 benchmark 使用 `--runner real`。这样单测和 CI 不依赖外部 API key，也不受模型波动影响。

代码依据：

- `models/provider.py:20-26`：统一 `LLMResponse`。
- `evaluation/harness.py`：`ScriptedProvider` 模拟 benchmark 模型行为。
- 多个测试文件中自定义 `FakeProvider` / `ScriptedProvider`。

## 24. 如果线上模型返回格式不稳定，比如 tool_call JSON 解析失败，Runtime 怎么兜底？

provider 层会先兜底。

OpenAI-compatible provider 解析 tool call arguments 时，如果 `json.loads()` 失败，会把 arguments 降级为空 dict `{}`，不会让整个 Runtime 崩掉。后续工具执行会进入正常 policy/schema/handler 流程：缺少参数会返回结构化错误，写入 tool message 和 trace。

流式 tool call 也是先拼接 chunks，再 `json.loads()`；失败同样降级 `{}`。

此外，针对 reflection/subagent/task conclusion 这类“模型返回 JSON 文本”的场景，仓库里有 `runtime/units/json_repair.py`，支持从 fenced code block、文本中的 balanced object、常见 trailing comma/引号问题中提取 JSON 对象，但不凭空发明字段。

空 assistant message 也有兜底：`ReasoningLoop` 如果连续收到空回复且没有 tool call，会先插入 runtime retry 提示，再空一次就保护性停止。

代码依据：

- `models/provider.py:86-96`：tool call arguments 解析失败降级 `{}`。
- `models/provider.py:163-173`：streaming tool call arguments 解析失败降级 `{}`。
- `runtime/units/json_repair.py`：结构化 JSON repair。
- `runtime/reasoning_loop.py:249-304`：空模型回复 retry/stop。

## 25. Runtime 目前最大的工程短板是什么？如果给你一个月，最想重构哪一块？

我会诚实地说，最大短板是还没有 step 级 checkpoint/replay 和持久任务队列。

当前系统已经有：

- session 持久化。
- run artifacts。
- trace.jsonl。
- working memory。
- coding context state。
- tool result store。
- workspace diff。

这些能支持语义级续做和复盘，但还不能从某个精确 reasoning step 或 tool call 之后恢复执行。后台记忆也是本地线程池 best-effort，不是持久任务队列。

如果给一个月，我最想重构三件事：

1. 把 `ReasoningLoop` 的 step 状态做成可序列化 checkpoint，包括 context hash、model request summary、tool call plan、tool result refs。
2. 在 `ToolExecutor` pre-hook 层加 approval hook，对 `git_commit`、大范围写文件、危险 shell、外部网络等高风险操作支持“挂起 -> 人类审批 -> 继续”。
3. 把 trace span 状态补齐成更接近 OpenTelemetry 的结构，方便跨进程 worker 和 Web/VS Code UI 共用。

这能把当前 Runtime 从“可追踪单进程 agent runtime”推进到“可暂停、可审批、可恢复”的执行内核。

