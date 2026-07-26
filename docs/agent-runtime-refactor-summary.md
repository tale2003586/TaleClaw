# TaleClaw Agent Runtime 瘦身与分层重构摘要

> 分析基线：本地仓库提交 `bb4e845`。本结论仅依据本地代码，不依据远程 TaleClaw 仓库。

## 1. 核心结论

TaleClaw 应取消 Chat Mode 和 Coding Mode 作为 Core Runtime 的全局运行模式，但不应把两者机械地压缩成完全相同的配置。

- Chat 应成为一个 `AgentSpec`，由轻量 Chat Application 调用。
- Coding 的 Prompt、模型、工具、上下文和限制应成为 `coding AgentSpec`。
- 当前 Coding 还拥有 workspace 解析、隔离 task session、上下文 handoff、Working Memory 同步、artifact、workspace diff、结论提取和 Memory promotion 等独立生命周期，因此应保留为 `CodingApplication`。
- Chat、Coding 和 Subagent 最终都应调用同一个 `Runtime.run()` 和同一个 `Runner`。

推荐定位：

> TaleClaw 是一个可嵌入的本地 Agent Runtime，并附带 Chat、Coding 和 Gateway 应用适配器；当前不应定位为通用 Workflow Engine 或分布式 Agent Platform。

## 2. 当前真实执行链

### Chat

```text
Gateway / AppRuntime
→ AgentLoop
→ ModeRouter
→ Pipeline
→ AgentRunner
→ ReasoningLoop
→ ContextBuilder
→ ModelProvider
→ ToolRegistry / ToolExecutor
```

### Coding

```text
Gateway / AppRuntime
→ AgentLoop
→ ModeRouter
→ TaskSessionRunner
→ WorkspaceResolver
→ 创建隔离 Task Session
→ Handoff / Working Memory 继承
→ Forked Pipeline
→ AgentRunner
→ ReasoningLoop
→ Artifact / Workspace Diff / Memory Promotion
```

两条路径共享 Model、ToolExecutor、Pipeline、AgentRunner 和 ReasoningLoop。Coding 的额外部分主要属于应用生命周期，而不是另一种底层 Agent Runtime。

## 3. 当前复杂度的主要来源

当前核心相关代码约 2.1 万行，其中：

- `runtime/execution/reasoning_loop.py`：约 1983 行；
- `runtime/context/builder.py`：约 1776 行；
- `runtime/working_memory.py`：约 1177 行；
- `tools/schema.py` 与 `tools/handlers.py`：合计约 3380 行。

关键问题不是单纯“类太多”，而是应用策略进入了公共执行机制：

1. `AgentLoop` 同时负责 Gateway 消息、身份、Session、路由、取消、插件、Trace、执行和投递。
2. `Pipeline → AgentRunner → ReasoningLoop` 存在偏薄的转发层。
3. `ReasoningLoop` 混入 Working Memory、Web Search 预算、并行任务、Coding 收尾提示和完整 Trace。
4. `ContextBuilder` 同时处理 Prompt、Skills、指令文件、Memory、历史检索、Security RAG、Coding Context State 和 Token Budget。
5. `Session.metadata` 被用作路由状态、Run 状态、Workspace 状态、Memory 状态和 Tool 状态的共享隐式总线。
6. Tool 权限分散在 `enabled_modes`、`admin_only`、`ToolPolicy`、Tool Hooks 和具体 Handler 中。
7. `runtime/bootstrap.py` 一次构造模型、Memory、RAG、插件、工具、Hooks、Runner 和应用层对象，装配职责过重。
8. 用户 MessageBus 和多 Agent Team Bus 使用相似名称，但承担不同职责。

## 4. 对主流框架的结论

主流 Agent 框架并不普遍使用 Kernel、System Call、Process 或 Driver 等操作系统术语，但普遍采用以下边界：

```text
Agent Definition
Runner / Runtime
Run Context
Model
Tools
Session / State
Optional Workflow / Persistence
```

主要参考价值：

- OpenAI Agents SDK：`Agent + Runner + RunContext` 的轻量边界；
- PydanticAI：typed dependencies、RunContext 和 UsageLimits；
- smolagents：直白、轻量的 model-tool 循环；
- Claude Code：子 Agent 的独立上下文、工具和权限配置；
- Codex CLI：Core、UI、App Server、Sandbox 和 Approval 的边界分离。

当前不适合 TaleClaw 默认采用：

- LangGraph 的强状态图与 durable checkpoint；
- AutoGen Core 的消息 Runtime 和分布式 Agent 生命周期；
- Microsoft Agent Framework 的 Workflow/Superstep 模型；
- CrewAI 的 Crew/Task/Process 默认建模；
- OpenHands 的容器 Client-Server Runtime 和完整控制面。

结论是组合借鉴，而不是直接模仿某一个框架。

## 5. “机制与策略分离”的适用范围

值得保留：

- 统一执行入口；
- 统一 Agent 生命周期；
- Runtime 不感知 Chat、Coding 等业务类型；
- 模型和工具统一受控；
- Limits、Cancellation 和错误边界由 Runtime 保证；
- Subagent 复用普通 Agent Run；
- 可选扩展不进入默认路径。

只保留思想或延后：

- 父子 Run 的资源预算；
- 简单 Tool Capability/Allow-list；
- Checkpoint、Task Graph、分布式 Worker；
- 跨进程 MessageBus。

不应实现：

- 完整 System Call 表；
- POSIX Signal；
- VFS 和 Driver 抽象；
- 微内核式所有调用消息化；
- 抢占式调度和 cgroups；
- 一切操作 Task 化；
- 通用复杂 Capability Token。

## 6. 目标架构

```text
CLI / Web / Gateway
        │
        ▼
ChatApplication / CodingApplication
        │
        ▼
Runtime.run(agent, input, context)
        │
        ▼
Runner
 ├─ ContextPolicy
 ├─ ModelProvider
 ├─ ToolRegistry / ToolExecutor
 ├─ RunLimits
 ├─ CancellationToken
 └─ RuntimeHooks

Optional Extensions
 ├─ Session Persistence
 ├─ Long-term Memory / RAG
 ├─ Working Memory / Checkpoint
 ├─ AgentSpawner / Multi-Agent
 ├─ Artifact
 ├─ Sandbox
 └─ Full Trace Export
```

最简单请求的目标调用链：

```text
Gateway
→ Application
→ Runtime.run
→ Runner
→ ContextPolicy
→ ModelProvider
→ RunResult
```

只有模型返回 Tool Call 时才进入 `ToolExecutor`。

## 7. 最小核心抽象

### AgentSpec

应包含：

- name；
- instructions；
- model policy；
- tool set；
- context policy；
- termination policy；
- run limits；
- output schema；
- skills；
- agent-scoped hooks；
-可选 spawn policy。

不应包含：

- 当前 Session 消息；
- Workspace 实例；
- Gateway/Channel；
- DB connection；
- mutable run counters；
- Memory 内容；
- Cancellation Event；
- current mode。

### Runner

负责：

- 一次 Run 的生命周期；
- Context 构建；
- Model 调用；
- Tool 调用循环；
- Limits 和 Cancellation；
- 最终输出和错误归一化；
- 最小 Run Events。

不负责：

- Chat/Coding 判断；
- Workspace 解析；
- 特定 Prompt；
- 长期 Memory 策略；
- Artifact；
- Gateway 投递；
-业务路由。

### RunContext

保存：

- run ID 和 parent run ID；
- 当前输入和 Run 内消息；
-显式依赖；
-可变 Run State；
- Usage；
- CancellationToken。

当前不需要 Context Snapshot、Delta、Fork/Merge 或 Version Tree。

### ToolRegistry 与 ToolExecutor

- Registry 只负责定义、查询和生成受限 Tool View；
- Executor 负责校验、授权、超时、执行、错误归一化和结果处理；
- Registry 不再直接执行 Handler。

Runtime 必须强制保证：

- Tool schema validation；
- Tool allow-list；
-最终授权；
- Workspace boundary；
- Cancellation；
- Step/Tool/Token limits；
-错误归一化。

这些能力不能仅依靠 Hooks。

## 8. Run、Task 与 Operation

不应强制所有行为使用 `Run → Task → Operation` 三层模型。

- Run：一次 Agent 从输入到终止的完整执行。
- Task：需要被独立跟踪、恢复、调度或汇总的业务工作单元，可选。
- Operation/Span：Model Call、Tool Call、Context Build 等技术操作。

建议关系：

```text
Task? ──关联──> Run
Run ──产生──> Operations / Events
Run ──创建──> Child Run
```

具体判断：

- Model Call：Operation，不是 Task；
- Tool Call：Operation，不是 Task；
- Context Compact：普通函数，可选 Span；
- Coding Task Session：Application Task + Run；
- Subagent：独立 Child Run；
- 只有需要持久化管理时，Subagent 才关联 Task。

## 9. Hooks 的边界

适合 Hooks：

-日志和 Trace 导出；
- Usage/预算统计；
- Context 注入；
- Tool 结果压缩；
-非权威安全分析；
-持久化通知；
-可选 Model 选择提示。

不适合只用 Hooks：

-最终权限判断；
- Tool Schema 校验；
- Cancellation；
-硬性 Limits；
- Workspace containment；
-核心状态一致性。

Hook 应只有少数固定阶段。安全 Hook 失败应 fail-closed，观测 Hook 失败可 fail-open，避免 Hook 链再次形成复杂 Runtime。

## 10. 重点模块调整

| 当前模块 | 建议 |
|---|---|
| `runtime/app_runtime.py` | 迁移为应用服务 facade |
| `runtime/agent_loop.py` | 拆为 Application orchestration 与 Core Runner |
| `runtime/routing/*` | 合并为应用层 RequestRouter |
| `modes/*` | 用 AgentSpec/Profile 替代 |
| `runtime/pipeline.py` | 兼容后删除 |
| `runtime/execution/agent_runner.py` | 保持轻量 AgentSpec 到 ReasoningLoop 适配 |
| `runtime/execution/reasoning_loop.py` | 继续提取纯 model-tool loop，移除应用策略 |
| `runtime/context/` | 已按 builder/providers/budget/history/report 收敛 |
| `runtime/coding_context_state.py` | 移入 Coding Application |
| `runtime/coding_handoff.py` | 移入 Coding Application |
| `runtime/working_memory.py` | 移入可选 Working Memory 扩展 |
| `tools/tool_registry.py` | 移除执行逻辑，增加正式 Tool View |
| `tools/executor.py` | 保留并承担强制安全边界 |
| `tools/schema.py`、`tools/handlers.py` | 按 fs/shell/memory/agents 等能力拆分 |
| `applications/coding/runner.py` | CodingApplication 生命周期 |
| `agents/subagent/runner.py` | 使用统一 `Runtime.run()` |
| `runtime/messaging/user_bus.py` | Gateway dispatcher |
| `runtime/messaging/team_bus.py` | 可选 Multi-Agent mailbox |
| `runtime/trace/*` | 作为可选 Trace 扩展 |
| `runtime/bootstrap.py` | 拆分 Core、Extensions、Applications、Gateways 装配 |

## 11. Bootstrap 重组

```python
runtime = build_core_runtime(config)
extensions = build_optional_extensions(config)
applications = build_applications(runtime, extensions)
service = build_gateway_service(applications)
```

`build_core_runtime()` 不应 import：

- Security RAG；
- Gateway；
- Coding Artifact；
- Session DB；
- Team Bus；
-分布式或后台 Worker。

## 12. 分阶段实施

### Phase 0：行为基线

- 固化 Chat、Coding、Tool Calling、Streaming、Cancellation、Session、Memory 和 Subagent 行为；
- 保存 Model 输入消息快照和 Token 数；
- 建立性能基线。

### Phase 1：统一 Agent 定义

- 扩展现有 `AgentSpec`；
- 将 Bot/Coding Profile 转换为 AgentSpec；
- 保留 ModeRouter 兼容层。

### Phase 2：建立统一 Runner

- 新增唯一 `Runtime.run()`；
- 先代理现有 Pipeline/ReasoningLoop；
- 让 Chat、Coding、Subagent 都经由该入口。

### Phase 3：收敛执行循环

- 合并 Pipeline、AgentRunner 和 ReasoningLoop 的生命周期逻辑；
- 将 Working Memory、Web Search、并行任务和 Coding 提示移出核心循环。

### Phase 4：统一 RunContext

- 将 Run 临时状态移出 `Session.metadata`；
-明确 Session、Run State 和 Application State 的边界；
-过渡期双写，保证兼容。

### Phase 5：Context Policy 化

-按 Prompt、History、Memory、Retrieval、Coding Context 拆成 Context Providers；
-保持 Prompt 顺序和 Token 行为不变。

### Phase 6：应用与扩展隔离

- `TaskSessionRunner` 收敛为 CodingApplication；
- Subagent 改为 Child Run；
- Memory、RAG、Artifact、Trace、MessageBus 移出默认路径。

### Phase 7：删除兼容层

-删除 ModeProfile、Pipeline、旧 AgentRunner 和 Core ModeRouter；
-清理 `current_mode`、`enabled_modes` 和旧 execution 字符串；
-拆分 Bootstrap 与大文件。

## 13. 第一阶段最值得完成的三件事

1. 建立 Prompt、Tool、Session、Streaming 和性能行为基线。
2. 用完整 `AgentSpec` 表达 Prompt、Tools、Model、Context Policy 和 Limits。
3. 建立唯一 `Runtime.run()`，先作为兼容 facade 统一 Chat、Coding 和 Subagent 入口。

不要在第一阶段直接重写 ContextBuilder、Memory 或 Tool Handler。

## 14. 性能原则

默认单 Agent 路径必须满足：

-不经过 MessageBus；
-不序列化；
-不强制持久化；
-不创建 Task Graph；
-不开启完整 Trace；
-不加载全部 Memory/RAG；
-不启动 Worker；
-不复制完整父 Agent Context；
-未启用扩展时几乎没有额外成本。

应重点测量：

-启动时间和 Runtime 构建时间；
-首 Token 时间；
-模型外 Runtime 开销；
- Context 构建时间和 Token 数；
- Tool wrapper 开销；
-内存占用；
-取消响应时间；
- Child Run 创建开销；
- Trace、MessageBus、Memory 开关前后差异。

## 15. 主要风险

- Context 重构改变 Prompt 顺序或 Token 数；
- Tool 权限在 Mode 迁移时意外放宽；
- Tool Schema 或错误格式变化；
- Streaming 事件顺序改变；
- Session/Memory 数据无法恢复；
- Coding Workspace containment 失效；
- Subagent 输出和取消语义变化；
- Plugin Hook 顺序或失败语义变化；
- MessageBus 移出默认路径后 Gateway 行为变化；
-可选扩展重新污染 Core；
-全面重写导致范围失控。

规避原则：

-先适配、再迁移、最后删除；
-一个阶段只移动一个边界；
-保留一个版本的兼容 facade；
- Prompt、Tool Schema 和权限使用快照/矩阵测试；
-关键迁移支持新旧路径对照；
-每个阶段可以通过回退单独提交恢复。

## 16. 最终决策清单

1. 取消 Core Runtime 的 Chat/Coding Mode。
2. Chat 使用 AgentSpec；Coding 使用 AgentSpec + CodingApplication。
3. MessageBus 不进入默认单 Agent 路径。
4. Pipeline 和当前 AgentRunner 最终删除。
5. ReasoningLoop 收敛为统一 Runner 的核心循环。
6. Context 模块按 Policy/Provider/Budget/Report 重组。
7. Subagent 是独立 Child Run，不是普通 Task。
8. Task 只用于需要独立跟踪和持久化的工作。
9. RuntimeHooks 承载可选扩展，但不替代强制安全边界。
10. Memory、RAG、Artifact、Sandbox 实现、Multi-Agent 和完整 Trace 均为可选扩展。
11. Bootstrap 拆成 Core、Extensions、Applications 和 Gateways 四层。
12. TaleClaw 保持轻量自定义 Runtime，不复制大型框架的 Workflow 或分布式架构。
