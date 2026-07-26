# TaleClaw Phase 10：最小 Agent Kernel 边界

日期：2026-07-23

## 目标

Phase 10 不直接拆分 ReasoningLoop，而是先建立可执行的 Kernel 边界，防止后续
提取协作者时继续依赖 Coding、RAG、Plugin 或其他产品实现。

最小 Kernel 输入输出：

```text
AgentSpec + input + RunContext
  → ContextPort
  → ModelPort
  ↔ ToolPort / ToolExecutorPort
  → LifecyclePort
  → RunResult

ObservabilityPort 只旁路接收事件。
```

## 端口

`runtime/ports.py` 定义结构化 Protocol：

| 端口 | 最小职责 |
|---|---|
| `ContextPort` | 构建一次模型上下文 |
| `ModelPort` | 发起模型 Chat 调用 |
| `ToolPort` | 提供本轮 schema 并调用已授权 Tool |
| `ToolExecutorPort` | 执行 Hook 包围的 Tool request |
| `LifecyclePort` | 在 Turn 边界执行可选后处理 |
| `ObservabilityPort` | 追加事件并保存 RunState |

协议采用 structural typing，不要求具体实现继承 Runtime 基类，也不引入 Adapter
层或运行时包装开销。

## 当前依赖矩阵

| Core 模块 | 直接依赖 | Phase 10 判断 |
|---|---|---|
| `agent_runner.py` | AgentSpec、Ports、Loop/Policies | 合法 |
| `reasoning_loop.py` | Ports、Token、Trace event helpers、Tool request types | Trace 仍待后续端口化 |
| `loop_policies.py` | Working Memory、Trace preview、config | 可选策略债务，Phase 12 外移 |
| `child_run.py` | Runtime RunExecutionState | 存在反向依赖，后续状态契约处理 |
| `reflection.py` | JSON repair | 合法纯单元 |

依赖 Gate 立即禁止 `runtime/execution/` 导入：

- `applications.*`
- `agents.subagent.*`
- `gateway.*`
- `knowledge.*`
- `plugins.*`
- `retrieval.*`
- `web.*`

## 本阶段解耦

ReasoningLoop 原本直接导入 `runtime.working_memory.partial_summary`。现在它只调用
注入的 WorkingMemoryPolicy，具体摘要实现留在策略模块。由此主循环不再直接
认识 Working Memory 模块。

AgentRunner、Runtime 和 ReasoningLoop 的构造边界已经使用 Ports 进行类型约束，
但保持原有 duck-typed 实现和调用语义。

## 未提前处理

- WorkingMemoryPolicy 本身仍依赖 Working Memory；
- Trace helper 仍被 ReasoningLoop 直接使用；
- WebSearchBudgetPolicy 仍是默认策略；
- ReasoningLoop 仍包含 model/tool/trace 多类逻辑；
- ContextBuilder 尚未进一步缩小。

这些是 Phase 11～13 的范围。本阶段若同时移动，会失去可验证的依赖基线。

## Gate

- Kernel/Runtime 定向测试：38 passed；
- 完整回归：419 passed，39 skipped；
- 安全、Workspace、Tool：64 passed，1 skipped；
- `pip check`、`compileall`、`git diff --check`：通过；
- Context token：Chat 730、Coding 2347；
- Runtime Facade：0.734 ms；
- Subagent：2.494 ms；
- 快照无变化。
