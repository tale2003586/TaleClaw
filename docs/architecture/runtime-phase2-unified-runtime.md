# TaleClaw Runtime Phase 2：统一 Runtime.run

> 实施日期：2026-07-23
> 基线提交：`bb4e845571428069a78a05526e71da8e41ddbcb7`

## 目标

建立唯一 Agent 执行入口：

```text
Runtime.run(agent, input, context) -> RunResult
```

本阶段是兼容 facade。它代理现有 Pipeline、AgentRunner 和 ReasoningLoop，不重写
执行循环，也不改变 Session、Prompt、Tool、Memory、Trace、Streaming 或
Cancellation 语义。

## 核心类型

### Runtime

持有一个现有 Pipeline，并负责：

- 校验 AgentSpec；
- 校验 RunContext；
- 解析兼容 ModeProfile；
- 将瞬时 Run 参数传给 Pipeline；
- 统一返回 RunResult。

Runtime 不创建数据库、MessageBus、Session、Task Graph、Worker 或外部连接。

### RunContext

显式承载一次 Run 的瞬时依赖：

- Session；
- 兼容 Profile；
- Streaming callback；
- Cancellation callback；
- Checkpoint callback；
- RunState；
- TraceStore；
- parent span。

Phase 2 仍以 Session 作为兼容状态容器。将临时 Run 状态迁出
`Session.metadata` 属于 Phase 4。

### RunResult

返回：

- output；
- session；
- agent；
- run_state。

## 三条统一路径

### Chat

```text
AgentLoop
→ Runtime.run(BOT_AGENT_SPEC, input, RunContext)
→ Pipeline
→ AgentRunner
→ ReasoningLoop
```

### Coding

```text
TaskSessionRunner
→ forked Pipeline
→ Runtime.run(CODING_AGENT_SPEC, input, RunContext)
→ AgentRunner
→ ReasoningLoop
→ 原 Coding lifecycle
```

### Subagent

```text
TaskSubagentRunner
→ subagent AgentSpec
→ filtered Pipeline
→ Runtime.run(agent, input, RunContext)
→ AgentRunner
→ ReasoningLoop
```

源码静态检查后，`runtime/` 和 `agents/` 中唯一的直接 `pipeline.run()` 位于
`runtime/runtime.py`。

## 兼容性

- Fake/旧 Pipeline 不接受新参数时，Runtime 根据函数签名只传入支持的参数；
- AgentLoop 测试 Router 不提供 AgentSpec 时，从 Profile 创建兼容 AgentSpec；
- TaskSessionRunner 原 Profile 参数保留；
- Pipeline 原公开 `run(session, profile, ...)` 接口保留；
- ContextBuilder、ReasoningLoop 和 Tool 权限路径不变。

## 验证

```text
Phase 0～2 专项：28 passed
Chat/Coding/Subagent 集成选择：39 passed, 1 skipped
Scripted Coding Benchmark：6 passed
```

性能结果：`benchmarks/results/runtime_phase2.json`

| 场景 | Median | P95 |
|---|---:|---:|
| 原 Pipeline no-tool | 0.681 ms | 0.694 ms |
| Runtime facade no-tool | 0.772 ms | 0.803 ms |

该对比每次都重新构造 Pipeline、Runtime、AgentSpec 和 Session，因此是 facade
装配开销的保守上界。生产 Chat 路径复用 Runtime 实例。没有增加 I/O、序列化或
持久化。

## Phase 2 完成判断

- 唯一 Runtime.run 已建立；
- Chat、Coding、Subagent 已全部迁入；
- 旧 Pipeline/ReasoningLoop 仍作为兼容执行实现；
- Phase 0 行为门禁保持通过。

Phase 3 才允许收敛 Pipeline、AgentRunner 和 ReasoningLoop 的生命周期职责。
