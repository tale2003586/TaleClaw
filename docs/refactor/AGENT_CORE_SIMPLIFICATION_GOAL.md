# TaleClaw Agent Core 精简目标

> 状态：后续重构总目标
> 制定日期：2026-07-23
> 当前基线：Phase 9 / `a6636ca`

## 目标

将 TaleClaw 收敛为一个小而稳定的 Agent 执行内核：

```text
AgentSpec
  + input
  + RunContext
        ↓
Context Port
        ↓
Model Call ↔ Tool Execution
        ↓
RunResult
```

Core Runtime 只负责：

1. 解释 AgentSpec；
2. 管理单次运行生命周期；
3. 构建或请求 Context；
4. 调用模型；
5. 执行 Tool call；
6. 判断停止条件；
7. 返回结构化结果。

Coding、Working Memory、Web Search、Security RAG、Subagent、Teammate、
Background Task、Trace 报告和 Memory Lifecycle 都通过显式策略或端口组合，
不再成为默认内核必须理解的业务分支。

## 为什么这是下一目标

Phase 0～9 已经统一 Runtime API、状态边界和物理目录，但复杂度仍集中在：

- `runtime/execution/reasoning_loop.py` 接近 2000 行；
- `runtime/context/builder.py` 接近 1800 行；
- `runtime/working_memory.py` 超过 1100 行；
- `tools/handlers.py` 超过 2000 行；
- Reasoning Loop 仍了解 Working Memory、Web Search、Subagent 和 Trace 细节；
- Runtime/AgentLoop 仍直接引用部分 Coding Application 状态；
- Session.metadata 仍承担多个子系统之间的隐式通信。

下一轮不应继续以“减少顶层文件”为目标，而应减少 Core 的职责、依赖和分支。

## 可验收的架构结果

### Core 依赖

`runtime/execution/` 不直接导入：

- `applications.*`
- `plugins.*`
- `retrieval.*`
- `knowledge.*`
- 具体 Coding/Subagent/Teammate 实现

Working Memory、Trace 和 Search 通过窄接口或策略使用。

### 默认执行路径

Chat no-tool 默认路径只需要：

- AgentSpec；
- RunContext/Session；
- Context builder 接口；
- Model provider；
- Tool registry/executor；
- termination policy。

它不强制创建或执行：

- Coding Application；
- Working Memory checkpoint；
- RAG；
- 完整 Trace；
- MessageBus；
- Task Graph；
- 远程 Worker；
- 持久化 Tool Result。

### 状态

新增跨层状态不得直接写入任意 `Session.metadata` key。需要跨模块共享的状态必须：

- 属于 RunContext 的显式字段；
- 属于命名清晰的 typed state；
- 或由扩展自己持有。

Session.metadata 仅保留真正需要跨轮次持久化的兼容状态。

### 规模

文件行数不是唯一指标，但应达到：

- Reasoning Loop 主编排控制在约 800 行以内；
- Context Builder 主编排控制在约 800 行以内；
- 单个核心函数只表达一个生命周期阶段；
- Tool handler 按能力形成独立模块，不再由一个文件承载全部实现。

这些指标不能通过把代码机械平移到大量无意义文件达成。

### 行为和性能

- Phase 0 行为、Prompt、Tool、Streaming 和 Trace 契约不变；
- 完整回归不得减少测试或放宽断言；
- Tool authorization 与 Workspace containment 保持 fail-closed；
- Chat/Coding Context token 不增加；
- Runtime facade 与 Chat no-tool 中位开销不得出现明显退化；
- 可选能力关闭时不产生额外持久化、序列化或后台线程。

## 建议任务序列

### Phase 10：定义最小 Agent Kernel 边界

目标：

- 生成 Core 依赖矩阵；
- 定义 Context、Model、Tool、Lifecycle、Observability 五个窄端口；
- 给 `runtime/execution/` 建立禁止依赖清单；
- 先用 characterization tests 固定行为；
- 不在本阶段大规模拆文件。

这是下一项应直接执行的任务。

### Phase 11：精简 Reasoning Loop

将以下能力从主循环提取为无状态协作者：

- Model invocation；
- message sanitation；
- Tool batch execution；
- termination evaluation；
- usage/trace event projection。

ReasoningLoop 最终只保留步骤推进和状态机。

### Phase 12：策略外移

将以下逻辑从 Core Loop 移到显式可选策略：

- Working Memory checkpoint；
- Web Search budget；
- finishing reminder；
- Subagent incomplete handling；
- Coding 特定提示与结束语义。

### Phase 13：Context Builder 核心化

- 将 Context assembly 与数据获取彻底分离；
- Memory、Retrieval、Security RAG、Coding 使用独立 Provider；
- Builder 只排序、预算、组装和报告；
- 可选 Provider 关闭时零额外开销。

### Phase 14：应用编排外移

- 将 AgentLoop 中的 Coding Handoff/Application 分支移入应用 dispatcher；
- Runtime 不再引用 Coding background manager；
- Chat 和 Coding 通过同一执行端口进入，但生命周期由 Application 持有。

### Phase 15：显式状态与 Tool 能力拆分

- 清理 Session.metadata 隐式总线；
- 按 filesystem、git、storage、sandbox、agents、memory 拆分 Tool handlers；
- 保持 Registry/Executor 作为统一权限边界。

## 每阶段 Gate

每个阶段必须独立验证：

- Behavior Gate；
- Tests Gate；
- Compatibility Gate；
- Performance Gate；
- Security Gate；
- Dependency Gate；
- Documentation Gate。

每阶段单独提交、可单独回滚，不跨阶段提前实现，不使用兼容 shim 掩盖错误边界。

## 完成定义

当以下条件同时成立时，Agent Core 精简目标完成：

1. Core Runtime 可在不加载 Coding、Memory、RAG、Plugin、Trace 报告模块的情况下
   完成最小 Chat no-tool 运行；
2. Core Execution 不依赖具体 Application；
3. Reasoning Loop 只负责模型—工具状态机；
4. Context Builder 只负责 Context assembly；
5. 所有可选能力通过显式端口、Provider 或 Policy 注入；
6. Session、RunContext 和扩展状态所有权清晰；
7. 完整回归、安全快照和性能基线全部通过；
8. 没有因精简而新增重复 Facade、旧路径 shim 或强制基础设施。

## 下一步

下一任务应为：

> Phase 10：先审计并固定最小 Agent Kernel 的依赖边界和端口契约，再实施最小范围
> 的解耦；不得直接开始拆 Reasoning Loop。
