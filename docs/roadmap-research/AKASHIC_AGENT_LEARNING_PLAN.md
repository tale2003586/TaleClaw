# Akashic Agent 学习与后续改进路线

本文基于对 `/home/tale/kaggle/akashic-agent` 的代码通读，目标不是把 `mytry` 复制成另一个 akashic-agent，而是从它的成熟结构里提取下一轮最值得学习和迁移的工程能力。

当前 `mytry` 已经完成了从单文件 harness 到 runtime 骨架的第一轮演进：

- SessionManager
- PassiveTurnPipeline
- Runtime MessageBus + AgentLoop
- bootstrap/runtime 装配
- ToolRegistry
- ModeProfile + ModeRouter
- Markdown Memory

下一轮学习重点应从“拆模块”转向“建立稳定运行时协议、生命周期钩子、可观测性和长期演进能力”。

---

## 一、Akashic Agent 架构地图

### 1. 启动与装配

核心文件：

```text
main.py
bootstrap/app.py
bootstrap/tools.py
bootstrap/wiring.py
bootstrap/providers.py
bootstrap/channels.py
```

关键设计：

- `main.py` 只是命令入口：setup、init、dashboard、cli、serve。
- `bootstrap/app.py` 负责应用级 runtime 生命周期：启动 core、channels、scheduler、dashboard、proactive，再统一 shutdown。
- `bootstrap/tools.py` 负责核心 runtime 装配：bus、provider、tool registry、session manager、memory runtime、scheduler、AgentLoop。
- `bootstrap/wiring.py` 是插件式装配表：context、memory engine、toolset provider 都通过名字解析。

值得学习：

> bootstrap 不只是 new 对象，而是把“可替换部件”集中到 wiring 层。这样以后换 memory engine、toolset、context builder，不用改主循环。

---

### 2. Runtime MessageBus

核心文件：

```text
bus/events.py
bus/queue.py
bus/event_bus.py
bus/events_lifecycle.py
```

关键设计：

- `bus/events.py` 定义 typed inbound/outbound item。
- `InboundMessage.session_key` 统一为 `channel:chat_id`。
- `MessageBus` 使用 `asyncio.Queue`，支持后台持续消费和 dispatch。
- outbound subscriber 按 channel 分发。
- 失败发送有一次 retry 和 fallback message。
- 另有 `EventBus` 负责 lifecycle 事件，不和用户消息总线混用。

值得学习：

> akashic-agent 把“用户消息队列”和“内部生命周期事件”分成两个 bus。前者负责 channel I/O，后者负责观测、插件、trace。

---

### 3. AgentLoop 与 PassiveTurnPipeline

核心文件：

```text
agent/looping/core.py
agent/core/passive_turn.py
agent/core/runner.py
agent/turns/orchestrator.py
agent/turns/outbound.py
```

关键设计：

`AgentLoop` 不直接把所有逻辑写在一个 while 里，而是装配成：

```text
MessageBus
  -> AgentLoop
  -> AgentCore
  -> PassiveTurnPipeline
  -> Reasoner
  -> ToolExecutor / ToolRegistry
  -> OutboundPort
```

`PassiveTurnPipeline` 又拆成明确阶段：

```text
BeforeTurn
BeforeReasoning
Reasoner.run_turn
AfterReasoning
AfterTurn
```

每个阶段都有输入、输出和插件模块链。

值得学习：

> 你的 `pipeline.py` 现在已经能跑，但还是“一段函数”。下一步可以学习 akashic 的 phase 思路：先不做完整插件系统，先把关键阶段命名出来。

---

### 4. Context Builder

核心文件：

```text
agent/context.py
agent/core/prompt_block.py
agent/prompting/assembler.py
agent/prompting/budget.py
```

关键设计：

上下文不是随手 `messages.append()`，而是由 ContextBuilder 统一渲染：

```text
system prompt
history
context frame
current user message
media envelope
```

它还把系统提示拆成 prompt block：

- identity
- behavior rules
- long-term memory
- self model
- recent context
- session context
- active skills
- skills catalog

值得学习：

> “装填上下文”应该成为一个独立对象，而不是散落在 pipeline 里。ContextBuilder 是下一阶段最值得迁移的模块形态。

---

### 5. ToolRegistry 与 ToolExecutor

核心文件：

```text
agent/tools/base.py
agent/tools/registry.py
agent/tool_hooks/executor.py
bootstrap/toolsets/*.py
```

关键设计：

- 每个工具是 `Tool` class，而不是 schema dict + handler lambda。
- `ToolRegistry` 保存工具、metadata、搜索文档和上下文。
- `ToolExecutor` 包一层 pre/post hook。
- 工具执行流程固定：

```text
pre_tool_use hooks
真实工具执行
post_tool_use / post_tool_error hooks
trace result
```

值得学习：

> 你已经做了 ToolRegistry。下一步不是立刻 class 化所有工具，而是先加 `ToolExecutor`，让安全检查、参数修正、审计日志有统一入口。

---

### 6. Memory 系统

核心文件：

```text
core/memory/runtime.py
core/memory/markdown.py
core/memory/engine.py
memory2/store.py
memory2/retriever.py
memory2/memorizer.py
memory2/query_rewriter.py
memory2/dedup_decider.py
memory2/post_response_worker.py
```

关键设计：

akashic-agent 有两层 memory：

```text
Markdown memory
  MEMORY.md / SELF.md / NOW.md / HISTORY.md / RECENT_CONTEXT.md / PENDING.md

Memory2
  SQLite + embedding + keyword search + RRF merge + dedup/supersede
```

被动 turn 前会通过 retrieval pipeline 做预检索：

```text
message + history + session metadata
  -> MemoryEngine.retrieve()
  -> text block + trace
  -> prompt context frame
```

对话结束后还有 consolidation/optimization，把 session 历史归档成长期记忆。

值得学习：

> 你的 Phase 8 Markdown memory 是正确第一步。下一步不要直接上 embedding，而是先做“记忆生命周期”：何时写、何时召回、何时压缩、何时去重。

---

### 7. Proactive 独立循环

核心文件：

```text
proactive_v2/loop.py
proactive_v2/agent_tick.py
proactive_v2/gateway.py
proactive_v2/context.py
proactive_v2/contracts.py
proactive_v2/energy.py
proactive_v2/presence.py
```

关键设计：

ProactiveLoop 不复用被动用户消息 loop，而是独立 tick：

```text
DataGateway 拉取 alerts/content/context
Presence / busy gate / quota / energy 计算
AgentTick 判断是否主动发
TurnOrchestrator 统一发送和记录
```

它有几个很重要的约束：

- 主动触达和被动回复分开。
- 先做 gate，再调用模型。
- 发送路径也走统一 OutboundPort。
- 主动消息会写回 session，后续用户可指代。

值得学习：

> 主动性不是“定时调用 LLM”。主动性是一套 gate、quota、presence、evidence、dedupe 和 outbound 编排。

---

### 8. Plugin 与 Lifecycle

核心文件：

```text
agent/lifecycle/phase.py
agent/lifecycle/phases/*.py
agent/plugins/manager.py
agent/plugins/base.py
plugins/*/plugin.py
```

关键设计：

插件不是随便 monkey patch，而是挂到明确阶段：

```text
before_turn
before_reasoning
prompt_render
before_step
after_step
after_reasoning
after_turn
before_tool_call
after_tool_result
```

`Phase` 会检查模块声明的 `requires/produces` slot，虽然只是 warning，但已经形成阶段契约。

值得学习：

> 插件系统可以很晚再做，但 lifecycle phase 应该尽早有。因为 phase 是插件、观测、测试、trace 的共同挂点。

---

## 二、mytry 下一轮学习目标

### 目标 A：从“能跑”走向“可观测”

当前问题：

- LLM 调用了什么、工具用了什么、上下文装了什么，不容易回看。
- 出错时只能看 traceback。
- session 里缺少 turn-level metadata。

学习方向：

- 引入 lifecycle event。
- 每轮记录 turn trace。
- 工具执行记录 final args、status、result preview。
- 保存 prompt context frame。

### 目标 B：从“函数式 pipeline”走向“阶段式 pipeline”

当前问题：

- `pipeline.py` 里 compact、inbox、background、LLM、tool loop 混在一起。
- 想插入 memory retrieval / prompt render / tool hook 会越来越难。

学习方向：

- 先抽 5 个阶段函数。
- 再引入轻量 Phase class。
- 最后为 plugin 预留 hooks。

### 目标 C：从“工具注册”走向“工具执行治理”

当前问题：

- ToolRegistry 解决了 schema/handler 对齐。
- 但安全、参数校验、审计、deny、重试仍无统一入口。

学习方向：

- 新增 ToolExecutor。
- 引入 pre_tool_use / post_tool_use hook。
- 先做 shell/edit 安全 hook。

### 目标 D：从“Markdown memory”走向“记忆生命周期”

当前问题：

- 有 `memorize` / `recall_memory`，但缺少策略。
- 什么该写、什么时候写、怎么去重，还没有系统化。

学习方向：

- 每轮 after_turn 做候选记忆提取。
- 写入 `PENDING.md`，再人工/模型确认入 `MEMORY.md`。
- 加 source_ref，避免重复写。

### 目标 E：从“单 CLI”走向“多 channel runtime”

当前问题：

- CLI 可用，但 channel 抽象还很薄。
- 后续 Telegram/HTTP/IPC 容易重新长出入口逻辑。

学习方向：

- Channel adapter 只负责收发。
- Runtime 只认 InboundMessage/OutboundMessage。
- Async MessageBus 替换同步 deque。

---

## 三、建议新增路线图

下面阶段从当前 `REFACTOR_PLAN.md` 之后继续编号。

---

## Phase 9：修正与稳定当前骨架

目标：先把 Phase 1-8 的接口边界稳定下来，避免继续在半重构状态上叠楼。

任务：

- 统一文件命名：
  - `AgentLoop.py` 建议改回 `agent_loop.py`。
  - `models/` 如果实际表示 mode，建议改为 `modes/`。
  - `bus.message_bus` 明确改名为 `team_bus.py`，避免和 `bus.user_bus.MessageBus` 混淆。
- 修正 import 风格：
  - 包内统一绝对 import 或统一相对 import。
  - 给 `tools/`、`memory/`、`bus/`、`models/` 补齐 `__init__.py`。
- 清理 pipeline 残留：
  - `pipeline.py` 不应再 import `TEAM` 或 `LEAD_TOOLS`。
  - `schemas_for_mode(profile.tool_mode)`，不要传整个 profile。
- 给 `Session` 增加 metadata：
  - `current_mode`
  - `created_at`
  - `updated_at`
  - `last_compacted`

完成标准：

- `python cli.py` 可以启动。
- `/coding`、`/chat`、`/hybrid` 可切换。
- chat mode 看不到 `bash/write_file/edit_file`。
- coding mode 能用代码工具。
- `memorize` / `recall_memory` 能读写 Markdown。

---

## Phase 10：Async Runtime MessageBus

学习来源：

```text
akashic-agent/bus/queue.py
akashic-agent/bus/events.py
```

目标：把当前同步 deque bus 升级成 async bus，为后台任务、channel、proactive 做准备。

新增/改造：

```text
bus/events.py
bus/user_bus.py
```

设计：

```python
async def publish_inbound(msg)
async def consume_inbound()
async def publish_outbound(msg)
async def dispatch_outbound()
```

`InboundMessage` 增加：

```text
timestamp
media
metadata
session_key property
```

`OutboundMessage` 增加：

```text
thinking
reply_to
media
metadata
```

完成标准：

- CLI adapter 用 `asyncio.run()` 启动。
- AgentLoop 可以阻塞等待 inbound。
- outbound dispatch 是后台 task。
- 发送失败不会直接崩 runtime。

---

## Phase 11：Provider 抽象

学习来源：

```text
akashic-agent/agent/provider.py
akashic-agent/infra/providers/llm_provider.py
bootstrap/providers.py
```

目标：pipeline 不直接依赖 `config.client`，而是依赖 provider port。

新增：

```text
provider.py
```

接口：

```python
class LLMProvider:
    async def chat(self, messages, tools, model, max_tokens, tool_choice="auto"):
        ...
```

第一版可以包装当前 OpenAI-compatible client。

完成标准：

- `pipeline.py` 不 import `client/MODEL`。
- model/base_url/api_key 从 bootstrap 装配进入 provider。
- 测试时可以用 FakeProvider 模拟 tool call。

---

## Phase 12：ContextBuilder

学习来源：

```text
akashic-agent/agent/context.py
akashic-agent/agent/core/prompt_block.py
```

目标：把“上下文装填”从 pipeline 移到专门的 ContextBuilder。

新增：

```text
context.py
prompt_blocks.py
```

第一版 prompt sections：

```text
identity
mode_rules
memory
recent_context
skills_catalog
runtime_inbox
background_results
current_message
```

`Pipeline` 只做：

```text
context = context_builder.build(session, inbound, profile)
reasoner.run(context.messages)
```

完成标准：

- system prompt、memory block、inbox、background results 不再散落 append。
- 可以打印或保存本轮 context frame。
- 切换 mode 只影响 profile/prompt block，不改 pipeline 主流程。

---

## Phase 13：Lifecycle Phase

学习来源：

```text
akashic-agent/agent/lifecycle/phase.py
akashic-agent/agent/core/passive_turn.py
```

目标：把被动 turn 拆成明确阶段，为插件和观测铺路。

新增：

```text
lifecycle/phase.py
lifecycle/types.py
lifecycle/phases/
```

第一版阶段：

```text
BeforeTurn
BeforeReasoning
BeforeStep
AfterStep
AfterReasoning
AfterTurn
```

先不要做完整插件系统。可以每个阶段只是函数列表。

完成标准：

- 每个阶段都有输入/输出 dataclass。
- 每轮 turn trace 能记录阶段耗时。
- memory retrieval 挂在 BeforeReasoning。
- session save/outbound publish 挂在 AfterTurn。

---

## Phase 14：ToolExecutor + Tool Hooks

学习来源：

```text
akashic-agent/agent/tool_hooks/executor.py
akashic-agent/plugins/shell_safety
akashic-agent/plugins/tool_loop_guard
```

目标：把工具执行从 `registry.execute()` 升级为可治理执行链。

新增：

```text
tools/executor.py
tools/hooks.py
```

执行流程：

```text
pre_tool_use hooks
registry.execute
post_tool_use hooks
post_tool_error hooks
```

第一批 hook：

- ShellSafetyHook：阻止危险命令。
- FileWriteScopeHook：限制写入 workspace。
- ToolLoopGuardHook：检测重复工具循环。
- ToolTraceHook：记录工具调用状态。

完成标准：

- 工具调用结果包含：
  - status
  - final_arguments
  - pre_hook_trace
  - post_hook_trace
  - result_preview
- shell/edit 安全逻辑不再散落在具体工具函数里。

---

## Phase 15：Turn Trace 与可观测性

学习来源：

```text
akashic-agent/bus/events_lifecycle.py
akashic-agent/static/dashboard/
akashic-agent/plugins/00_observe
```

目标：每轮 turn 可回放、可排障。

新增：

```text
observe/store.py
observe/events.py
```

记录：

- inbound message
- selected mode/profile
- rendered context frame
- visible tools
- LLM call count
- tool chain
- final assistant reply
- errors
- token estimate

第一版可以写 JSONL：

```text
.observe/turns.jsonl
```

完成标准：

- 每次 CLI 问答后生成一条 turn trace。
- 出错时 trace 中能看到失败阶段。
- 可用 `python scripts/show_last_turn.py` 查看上一轮。

---

## Phase 16：Session SQLite 持久化

学习来源：

```text
akashic-agent/session/store.py
akashic-agent/session/manager.py
```

目标：替换内存 session，使 CLI 重启后能延续对话。

新增：

```text
session_store.py
```

表：

```text
sessions(key, created_at, updated_at, metadata, current_mode)
messages(id, session_key, seq, role, content, extra, ts)
```

注意：

- assistant 的 tool_chain 应作为 extra 保存。
- tool result 要截断，避免 SQLite 膨胀。
- session history 要按 user boundary 截取，避免从 tool message 开头恢复。

完成标准：

- 退出 CLI 再进入，保留历史。
- `/new` 可以开启新 session。
- `/sessions` 可以列出历史 session。

---

## Phase 17：Memory Lifecycle

学习来源：

```text
akashic-agent/core/memory/markdown.py
memory2/post_response_worker.py
memory2/dedup_decider.py
memory2/memorizer.py
```

目标：让 memory 从“手动工具”进化为“有生命周期的长期上下文”。

新增文件：

```text
memory/PENDING.md
memory/HISTORY.md
memory/RECENT_CONTEXT.md
memory/consolidator.py
```

流程：

```text
AfterTurn
  -> 提取候选事实/偏好/项目约定
  -> 写 PENDING.md
  -> 定期 consolidate 到 MEMORY.md / NOW.md / HISTORY.md
```

第一版不要 embedding。

完成标准：

- 用户明确说“记住...”时直接写 MEMORY.md。
- 普通对话中疑似长期事实先进 PENDING.md。
- `recall_memory` 同时读取 MEMORY/SELF/NOW/RECENT_CONTEXT。
- 每条记忆带 source_ref，避免重复写入。

---

## Phase 18：Tool Search / Deferred Tools

学习来源：

```text
akashic-agent/agent/tools/tool_search.py
akashic-agent/agent/tools/registry.py
agent/core/passive_turn.py 中 visible_names 逻辑
```

目标：当工具数量变多后，不再每轮暴露全部 schema。

设计：

```text
always_on tools:
  recall_memory
  memorize
  tool_search
  task_list

deferred tools:
  bash
  write_file
  edit_file
  web_fetch
  spawn_teammate
```

本轮工具可见性：

```text
always_on + router/profile preloaded + tool_search 解锁
```

完成标准：

- chat/coding mode 仍然保留硬边界。
- coding mode 下也不是所有工具默认可见。
- 模型可以通过 `tool_search("select:bash")` 解锁工具。

---

## Phase 19：Plugin MVP

学习来源：

```text
akashic-agent/agent/plugins/manager.py
akashic-agent/plugins/*
```

目标：给 runtime 加可插拔扩展点，但保持极简。

新增：

```text
plugins/
plugin_manager.py
plugins/shell_safety/plugin.py
plugins/status_commands/plugin.py
```

第一版插件能力：

- 注册工具。
- 注册 tool hook。
- 注册 before_turn / after_turn 回调。

不要一开始支持动态 import manifest 全套机制，可以先手动注册。

完成标准：

- shell safety 可以作为插件启用/禁用。
- `/status` 命令可以作为插件工具或 before_turn handler。
- 插件不需要改 pipeline 主代码。

---

## Phase 20：Proactive MVP

学习来源：

```text
akashic-agent/proactive_v2/loop.py
akashic-agent/proactive_v2/gateway.py
akashic-agent/proactive_v2/presence.py
akashic-agent/agent/turns/orchestrator.py
```

目标：给 Nanobot/Hybrid 增加主动能力，但不干扰 Coding。

新增：

```text
proactive/loop.py
proactive/policy.py
proactive/state.py
PROACTIVE_CONTEXT.md
```

第一版只做三种主动事件：

- background task 完成提醒。
- 用户设置的定时提醒。
- 每日总结。

必须有 gate：

- 用户显式开启。
- quiet hours。
- cooldown。
- session busy。
- coding mode 禁止闲聊式主动触达。

完成标准：

- proactive 默认关闭。
- `/proactive on` 后启用。
- coding mode 只允许任务完成通知。
- 所有 proactive outbound 也走 MessageBus。

---

## Phase 21：Dashboard / Inspector

学习来源：

```text
akashic-agent/static/dashboard/
bootstrap/dashboard_api.py
```

目标：先做一个本地 inspector，不急着做完整 dashboard。

第一版页面或 CLI 命令展示：

- sessions
- last turn trace
- memory files
- tool registry
- current mode
- background tasks

完成标准：

- 不打开日志也能知道 agent 当前状态。
- 可以查看最近 10 轮工具调用。
- 可以查看 memory 当前内容。

---

## 四、推荐学习顺序

### 第一组：稳定当前项目

1. Phase 9：修正与稳定当前骨架
2. Phase 10：Async Runtime MessageBus
3. Phase 11：Provider 抽象

这三步解决“能长期跑”的问题。

### 第二组：提升 agent 质量

4. Phase 12：ContextBuilder
5. Phase 13：Lifecycle Phase
6. Phase 14：ToolExecutor + Tool Hooks
7. Phase 15：Turn Trace

这四步解决“能看懂、能调试、能安全扩展”的问题。

### 第三组：长期能力

8. Phase 16：Session SQLite
9. Phase 17：Memory Lifecycle
10. Phase 18：Tool Search

这三步解决“越用越稳定”的问题。

### 第四组：产品化能力

11. Phase 19：Plugin MVP
12. Phase 20：Proactive MVP
13. Phase 21：Dashboard / Inspector

这三步解决“可扩展、可运营、可观察”的问题。

---

## 五、最应该先仿的三个 akashic 模块

### 1. `bus/events.py` + `bus/queue.py`

原因：

- 改动范围小。
- 能立刻统一 CLI、background、future channel。
- 是 proactive 的前置条件。

### 2. `agent/core/passive_turn.py` 的阶段划分

原因：

- 不需要复制完整代码。
- 只要学习它的阶段命名和输入输出。
- 能让你当前 pipeline 立刻清爽很多。

### 3. `agent/tool_hooks/executor.py`

原因：

- 你的工具已经开始增多。
- shell/write/edit 的风险会越来越高。
- hook 比把安全逻辑塞进每个工具更稳。

---

## 六、不要急着复制的部分

这些模块很强，但现在不建议直接搬：

- `memory2` 的 embedding/SQLite-vec/RRF 全套。
- `proactive_v2` 的完整 energy/anyaction/drift。
- 完整 PluginManager 动态加载系统。
- Dashboard 前端。
- Telegram/QQ 全 channel。
- MCP server registry。

原因：

> 这些系统都依赖前面的 runtime、event、trace、tool hook 和 persistence。现在直接搬会变成“功能很多但边界不稳”。

---

## 七、下一刀建议

最建议马上做：

```text
Phase 9：修正与稳定当前骨架
```

因为你当前项目已经进入第二轮重构，容易出现这些问题：

- `MessageBus` 名字冲突。
- `models` / `modes` 命名偏移。
- `AgentLoop.py` 和 `agent_loop.py` 混用。
- pipeline 仍有旧 import。
- profile/tool_mode 传参容易错位。

先把这些钉牢，再进入 async bus 和 lifecycle。这样后面的学习会顺很多。

