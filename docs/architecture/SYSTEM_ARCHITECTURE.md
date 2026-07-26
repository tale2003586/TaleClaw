# TaleClaw 系统架构

> 基于本地仓库当前实现生成  
> 代码基线：`a6636ca`（Phase 9 完成后）  
> 更新日期：2026-07-23

## 1. 系统定位

TaleClaw 是一个 Python Agent Runtime 平台，而不是单一聊天机器人。系统通过
CLI、Web、Telegram 和飞书接收消息，完成身份解析、会话加载、Agent 路由、
上下文构建、模型调用、工具执行、状态持久化和回复投递。

平台在通用 Runtime 之上提供 Coding Application，管理独立任务会话、真实
Workspace、Coding Context、工作记忆、Subagent/Teammate 编排、产物、结论
提取和记忆提升。

当前架构的核心原则：

- `AgentSpec` 描述不可变 Agent 能力；
- `RunContext` 保存单次运行状态；
- `Session` 保存跨轮次会话状态；
- `Runtime.run()` 是统一执行 Facade；
- Application 生命周期不进入通用 Reasoning Loop；
- Tool Registry 决定可见性，Tool Executor 和 Hook 强制安全策略；
- Memory、RAG、Trace、Plugin 均为可组合能力。

## 2. 总体分层

```text
┌──────────────────────────────────────────────────────────────┐
│ Entrypoints: CLI · Web · Telegram · Feishu                  │
└──────────────────────────────┬───────────────────────────────┘
                               │ InboundMessage
┌──────────────────────────────▼───────────────────────────────┐
│ Application Runtime: AppRuntime · MessageBus · AgentLoop     │
└──────────────────────────────┬───────────────────────────────┘
                               │ route
┌──────────────────────────────▼───────────────────────────────┐
│ Routing/Application: AgentRouter · Plan · CodingApplication  │
└──────────────────────────────┬───────────────────────────────┘
                               │ AgentSpec + input + RunContext
┌──────────────────────────────▼───────────────────────────────┐
│ Core Runtime: Runtime → AgentRunner → ReasoningLoop          │
│ Context · Policies · Model Routing · ToolExecutor            │
└───────────────┬──────────────────┬───────────────────────────┘
                │                  │
┌───────────────▼──────────┐ ┌────▼───────────────────────────┐
│ State/Observability     │ │ Extensions                    │
│ Session · Memory · Trace│ │ Plugins · RAG · Subagents    │
└──────────────────────────┘ └────────────────────────────────┘
```

## 3. 代码目录与所有权

```text
applications/coding/      Coding 产品应用及任务生命周期
agents/definitions.py     Bot/Coding AgentSpec
agents/subagent/          Child Run、并行任务、重试和结果协议

runtime/
  runtime.py              Runtime.run() Facade
  app_runtime.py          多入口应用级异步 Facade
  agent_loop.py           单条用户消息的入口生命周期
  agent_spec.py           AgentSpec 与声明式策略
  bootstrap.py            生产 Composition Root
  context/                Context 构建子系统
  execution/              Runner、ReasoningLoop 与执行策略
  messaging/              用户及 Agent 团队消息
  routing/                Intent、ExecutionPlan、AgentRouter
  sessions/               Session 与 SessionStore
  tooling/                Tool result 压缩、存储、签名
  trace/                  RunState、事件、索引、报告、Workspace Diff

models/                   Provider、ModelPool、用途路由
tools/                    Schema、Registry、Policy、Executor、Hook、Handler
memory/                   长期记忆、归档、候选、向量索引和生命周期
plugins/                  Turn/Tool 扩展
retrieval/ + knowledge/   Security RAG
gateway/ + web/           外部入口
evaluation/               评测与 SWE-bench adapter
```

`runtime/` 顶层只保留稳定入口、契约、Composition Root 和尚不适合形成独立
子包的共享能力。

## 4. 启动与依赖组装

生产组装入口是 `runtime/bootstrap.py::build_runtime()`：

1. 加载 `.env` 并配置代理；
2. 创建 `ModelPool` 与用途路由 Provider；
3. 创建 `MessageBus`、`SessionManager` 和 `TraceStore`；
4. 构建 Lead Tool Registry；
5. 创建 Agent Router 和 Hybrid Classifier；
6. 创建 Scoped Memory、Archive、History Vector Index；
7. 按配置创建 Security RAG；
8. 创建 `ContextBuilder` 和 Memory Lifecycle；
9. 注册 Plugin；
10. 组装 Tool Executor 与 Hook；
11. 创建通用 `Runtime`；
12. 创建 Coding Application 和 Subagent Runner；
13. 创建 `AgentLoop`；
14. 返回 `AppRuntime`。

`bootstrap.py` 是允许依赖具体 Application、Plugin 和 Infrastructure 的
Composition Root。Core Context 通过注入的 Coding Context builder 使用 Coding
能力，不直接导入 Coding Application。

## 5. 用户消息主链路

```text
Gateway/Web/CLI
→ AppRuntime.run_message() / MessageBus
→ AgentLoop.run_inbound()
→ SessionManager.get_or_create()
→ PluginManager.before_turn()
→ AgentRouter.route()
→ ExecutionPlan
    ├─ direct_reply
    ├─ Runtime.run()
    └─ CodingApplication.run_coding_task()
→ Session save / Trace finalize
→ OutboundMessage
```

`AgentLoop` 依次应用身份、创建 RunState、执行前置插件、路由、记录用户消息、
执行 Chat 或 Coding、更新状态并投递回复。取消使用每个 Session 独立的
`threading.Event`。

## 6. Agent 与路由

`runtime/agent_spec.py` 定义不可变 `AgentSpec`：

- `ModelPolicy`：模型用途和可选固定模型；
- `ToolSet`：Tool mode、allow、deny；
- `ContextPolicy`：History、Memory、Skill 策略；
- `TerminationPolicy`：结束行为；
- `RunLimits`：Token、Step、Tool call 限额；
- `SpawnPolicy`：Child Agent 权限；
- instructions、skills、hooks、output schema。

生产 Bot/Coding 定义位于 `agents/definitions.py`。AgentSpec 不保存 Session 或
单次运行状态。

`runtime/routing/` 将输入与 Session 状态转换为 `ExecutionPlan`：

1. `IntentClassifier` 识别指令、聊天或 Coding 候选；
2. `ExecutionPlanner` 结合所选 Agent 与用户角色生成计划；
3. `HybridModeClassifier` 判定模糊 Coding 候选；
4. `AgentRouter` 记录最后一次路由。

`bot`、`coding`、`hybrid` 仍是用户选择和 Tool View 名称，但旧 Mode 类与旧
Pipeline 已删除。Coding 权限默认要求管理员角色。

## 7. Core Runtime

统一调用形式：

```python
result = runtime.run(
    agent_spec,
    input_text,
    RunContext(session=session, ...),
)
```

`Runtime.run()` 校验 AgentSpec/RunContext、设置 RunExecutionState、构建稳定
Context Prefix、委派 AgentRunner/ReasoningLoop，并返回 RunResult。
`Runtime.fork()` 让 Coding Task 复用模型与工具配置，同时替换 Context、
Memory Lifecycle 或执行限额。

`runtime/execution/` 包含：

- `agent_runner.py`：将 AgentSpec 策略适配给 ReasoningLoop；
- `reasoning_loop.py`：模型—工具循环；
- `loop_policies.py`：搜索预算、收尾、Working Memory、Tool batch；
- `failure_reasons.py`：稳定停止原因；
- `reflection.py`：可选反思；
- `child_run.py`：子运行身份。

Reasoning Loop：

```text
build context
→ sanitize / token safety
→ select provider and model
→ model call
→ final text? ─ yes → finish
→ execute tool calls
→ append results / checkpoint / trace
→ next step
```

它支持流式文本、取消、Tool 限额、不可用 Tool 防循环、并行 Tool batch、
Working Memory checkpoint 和显式 Stop Reason。

## 8. Context

```text
runtime/context/
  builder.py       主编排与 Prefix cache
  providers.py     Prompt、History、Memory、Retrieval、Coding Provider
  budget.py        Section Budget
  history.py       历史与活动轮次压缩
  sections.py      ContextSection、ContextBuildReport
  build_state.py   单次构建状态
```

Context 按顺序组装 System/Agent instructions、指令文件、Skills、History、
Active Turn、长期/近期 Memory、Working Memory、History Retrieval、Security
RAG、Inbox/Background events 和可选 Coding Context State。

每个 Section 记录原始大小、预算、截断、压缩原因和传输方式。模型调用前还会
再次进行 Token 安全窗口检查。

稳定 API：

```python
from runtime.context import ContextBuilder, ContextBundle, ContextPrefix
```

## 9. Coding Application

`applications/coding/runner.py::CodingApplication` 包装通用 Runtime：

- 解析并限制 Workspace；
- 从父 Session 构造 Handoff；
- 创建隔离 Task Session；
- 继承 Working Memory；
- 创建 Task-local Memory；
- Fork Runtime 并注入 Coding Context；
- 捕获 Workspace before/after 与 Diff；
- 提取结论并按规则提升长期 Memory；
- 写入任务日志、Artifact 和 Trace；
- 将结果返回父 Session。

状态边界：

- 父 Session：用户对话与 Coding Task 摘要；
- Task Session：一次 Coding 任务完整消息；
- RunContext：单次执行状态；
- CodingContextState：压缩后的工具历史；
- WorkingMemory：行动队列、checkpoint、证据；
- Workspace：真实且受限的代码目录；
- Task-local Memory：完成后按置信度提升。

`applications/coding/orchestration/` 提供 Task、Teammate、Background 和 Agent
Message Protocol。`agents/subagent/` 提供独立 Child Run、并行派发、失败分类、
重试/降级和结果协议。Subagent 使用独立 Session 与过滤后的 Tool View。

## 10. Tool 与安全边界

```text
Tool Schema
→ ToolRegistry visibility
→ ToolPolicy authorization
→ ToolExecutionRequest
→ before hooks
→ handler
→ after hooks
→ ToolExecutionResult
```

`ToolRegistry` 保存 schema、handler、允许 Agent、管理员限制、风险等级和默认
可见性。`ToolExecutor` 是统一强制执行边界。主要 Hook：

- `ShellSafetyHook`；
- `ShellWorkspaceScopeHook`；
- `FileWriteScopeHook`；
- `ToolLoopGuardHook`；
- `ToolResultStoreHook`；
- `ToolTraceHook`。

权限不能仅依赖 Prompt 或 schema。Registry、Policy、Executor 和 Hook 共同构成
安全边界。安全 Hook 应 fail-closed，纯观测 Hook 可以 fail-open。

`runtime/tooling/` 负责 Tool call/result 签名、大结果存储、结果压缩和引用取回。

## 11. Session、身份与持久化

Session 保存 id、messages、metadata、所选 Agent 和时间信息。SessionManager
提供 get/create/save，SessionStore 支持数据库持久化。

入口 Adapter 将平台身份写入 Inbound metadata，再由 AgentLoop 绑定 Session。
Memory、Storage、Workspace 与 Session 必须使用相同用户作用域。

| 数据 | 默认/可选后端 | 所有者 |
|---|---|---|
| Session | PostgreSQL + 进程内 LRU cache | Runtime Sessions |
| 长期 Memory | 文件/scoped store | Memory |
| Archive | PostgreSQL | Memory |
| Memory Candidates | 本地 JSON | Memory |
| History Vector | 本地协议或 Qdrant | Memory |
| Tool Result | 文件或 PostgreSQL | Runtime Tooling |
| Trace | 本地目录 + 可选数据库索引 | Runtime Trace |
| Gateway/Web identity | PostgreSQL | Gateway/Web |
| Coding artifacts | Task 目录 | Coding Application |

`runtime/db.py` 被多个子系统共享，是基础设施所有权尚未独立的一处架构债务。

## 12. Memory

`memory/` 包含 ScopedMemoryStore、MemoryStore、MemoryLifecycle、
BackgroundMemoryLifecycle、Archive、Candidates、Processing Device 和 Vector
Index/Qdrant。

Memory Lifecycle 在 Turn 边界之外或之后执行，不强制阻塞默认模型调用路径。
Coding 使用 Task-local Memory，仅提升符合质量规则的结论。

`runtime/working_memory.py` 是可恢复执行状态，不等同于长期 Memory。

## 13. Model 与 Provider

`models/provider.py` 提供 OpenAI 兼容 Provider。
`models/model_pool.py` 从环境构建多个 Model Profile，按 `chat`、`coding`、
`summary`、`hybrid` 等用途路由，支持候选链、健康检查和失败切换。
`ModelTaskRunner` 服务摘要、结论提取和 Memory candidate 等小型模型任务。

## 14. Plugin 与 Security RAG

PluginManager 组合 Turn、Tool、Run/Eval 扩展。生产可包含 Shell Safety、Status
Commands、Web Search、Markdown PDF、Run Report 和可选 Security RAG。Plugin
不能绕过 Tool Executor。

Security RAG 分为：

- `knowledge/`：摄取、切块、Embedding、缓存、重排；
- `retrieval/`：查询分类与混合检索；
- Security RAG Plugin：显式 Tool；
- Context Provider：可选自动注入；
- Trace events：决策、查询、命中和耗时。

Qdrant、Embedding 和 RAG 均可关闭，不影响主 Runtime。

## 15. Trace 与可观测性

每个入口 Turn 创建 RunState。TraceStore 写入：

- run state；
- JSONL events；
- Context metrics；
- Tool/model timeline；
- failure classification；
- Workspace snapshot/diff；
- JSON/Markdown summary；
- 可选数据库索引。

Trace 覆盖 Run、Context、Model、Tool、Subagent、Memory、RAG 和 Coding
Application。敏感字段在序列化前过滤。Trace 是观测层，不参与授权。

## 16. 外部入口与部署

- `cli.py`：本地交互；
- `web/server.py`：认证、Session、Streaming、文件预览和静态前端；
- Telegram/飞书 Gateway：平台事件、身份、消息转换与投递。

最小部署：

```text
Agent Console/Worker
├─ LLM Provider
└─ PostgreSQL
```

可选 RAG：

```text
Agent Console/Worker
├─ LLM Provider
├─ PostgreSQL
├─ Qdrant
└─ Embedding model
```

Telegram、飞书 Worker 可独立启动。Runtime 不要求远程 Worker、分布式 Task
Graph 或强制 MessageBus 序列化。

## 17. 依赖规则

```text
gateway/web/cli → AppRuntime
AppRuntime/AgentLoop → routing + applications + Runtime Facade
applications → runtime contracts
Runtime Facade → context + execution + tools/models
execution → context/tooling/trace contracts
plugins/memory/rag → runtime extension points
```

应避免：

- Context Core 直接导入具体 Application；
- AgentSpec 保存可变 Session/Run 状态；
- Application 生命周期进入 ReasoningLoop；
- Tool handler 绕过 Executor/Hook；
- 用 Session.metadata 新增无契约的跨层通信；
- 默认路径强制启用 RAG、Trace index、远程 Worker或持久化。

## 18. 当前架构债务

1. `runtime/execution/reasoning_loop.py` 接近2000行，需要继续提取 model call、
   tool batch、message sanitation 和 trace emission。
2. `runtime/context/builder.py` 接近1800行，目录已收敛但编排仍重。
3. `runtime/working_memory.py` 超过1100行，应按 checkpoint、render、
   normalization 拆分。
4. `tools/handlers.py` 很大，尚未按文件、Git、Storage、Sandbox、Agent、
   Memory 能力拆包。
5. AgentLoop 仍了解 Coding Handoff，Runtime 仍引用 Coding background manager，
   Application 边界可进一步纯化。
6. `runtime/db.py` 被多个领域共享。
7. Session.metadata 仍承载较多兼容状态。
8. 仓库没有统一类型检查、Formatter Gate 和 Linter。

## 19. 当前验证基线

- Context/Execution/Tooling：74 passed；
- 完整回归：416 passed，39 skipped；
- 安全、Workspace、Tool：64 passed，1 skipped；
- package build、import smoke、`pip check`、`compileall`：通过；
- Chat/Coding Context token：730 / 2347；
- Runtime Facade 中位开销：约0.715 ms；
- Prompt、Tool Schema、Streaming 和行为快照无变化。

以上数据证明 Phase 9 目录调整没有改变可观察行为，不代表真实模型或网络吞吐。

## 20. 文档维护

本文件是当前系统级架构入口。专题细节位于 `docs/system-design/`、
`docs/runtime/`、`docs/sessions-memory/`、`docs/tools-plugins/`、`docs/rag/`、
`docs/gateways/` 和 `docs/web/`。

`docs/architecture/runtime-phase*.md` 与 `docs/refactor/` 保存阶段设计和历史决策，
不应替代本文件描述当前实现。
