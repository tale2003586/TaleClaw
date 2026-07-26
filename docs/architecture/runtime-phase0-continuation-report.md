# TaleClaw Runtime Phase 0 补充执行报告

> 执行日期：2026-07-23
> 基线：`bb4e845571428069a78a05526e71da8e41ddbcb7`
> 范围：继续完成首轮 Phase 0 报告列出的未完成事项

## 1. 本轮完成内容

本轮继续增加了 5 个离线基线测试，并扩展性能基准。生产代码仍然零修改。

新增覆盖：

1. Model → Tool → Model 的完整 Trace 事件顺序快照；
2. Tool 执行期间请求取消的当前语义；
3. Memory 关闭/开启及其 Context 注入位置；
4. Coding Task Session Factory 的隔离状态和 Memory Root；
5. Subagent 独立 Session、过滤工具、父历史隔离和结构化结果；
6. Memory、Null Trace、Streaming、Cancellation、Task Session Factory 和
   Subagent 性能。

## 2. 新增行为结论

### 2.1 Trace 事件

稳定事件快照位于：

```text
tests/snapshots/runtime_phase0_trace_events.json
```

该快照覆盖：

```text
reasoning step
→ context build
→ model call
→ tool call
→ second reasoning step
→ second model call
→ loop completed
```

当前普通 `on_text` chunk 不会作为 `ReasoningLoop` TraceEvent 写入；文本 delta 属于
Provider/Web Streaming 层。

### 2.2 Tool 执行中 Cancellation

当前 ToolExecutor 和 Handler 是同步调用。若 cancellation 在 Handler 执行期间被设置：

1. Handler 会先完成；
2. Tool Result 会写入 Session；
3. 下一轮 `ReasoningLoop` 入口观察 cancellation；
4. 不再进行下一次 Model Call；
5. Run 以 `USER_CANCELLED` 停止。

这不是抢占式 Tool Cancellation。

### 2.3 Memory 注入

Memory 关闭时，Context report 的 `memory.rendered_chars == 0`。

Memory 开启时，长期 Memory 当前以独立 `user` message 注入：

```text
system
→ user(memory block)
→ user(current request)
```

### 2.4 Task Session

`TaskSessionFactory` 当前保证：

- 新建独立 Session；
- Session ID 使用 `task:coding-*`；
- 写入 parent session ID、task ID/type、status、user request 和 Memory Root；
- 创建独立 Memory Root；
- 不修改父 Session；
- 不自动复制父 workspace metadata。

Workspace metadata 由 `TaskSessionRunner` 在 WorkspaceResolver 完成后显式绑定。

### 2.5 Subagent

补充测试确认：

- 使用独立 Session；
- 不复制父消息历史原文；
- Tool View 来自 Agent Type 白名单；
- 结果解析为结构化 `SubagentResult`；
- Parent Session 内容保持不变。

## 3. 测试结果

### 新增与累计 Phase 0 测试

| 测试范围 | 通过 | 失败 | 跳过 | 结果 |
|---|---:|---:|---:|---|
| 首轮 Phase 0 测试 | 12 | 0 | 0 | 通过 |
| 本轮新增扩展测试 | 5 | 0 | 0 | 通过 |
| Phase 0 累计 | 17 | 0 | 0 | 通过 |

### 相关既有测试

| 命令范围 | 通过 | 失败 | 跳过 | 说明 |
|---|---:|---:|---:|---|
| Context/Memory/Working Memory/Subagent/Web/Bus/Workspace 等 | 90 | 2 | 2 | 2 个 Coding Benchmark 因 PostgreSQL Session Store 失败 |
| Trace/Subagent/Run Trace，`TRACE_INDEX_ENABLED=0` | 39 | 2 | 2 | 同样由 Session Store 强制 PostgreSQL 导致 |

失败不是新增测试或生产行为变化造成。当前 `TraceStore` 默认 Trace Index 和
`SessionManager` 都要求 PostgreSQL；只关闭 Trace Index 仍不足以运行 Coding
Benchmark。

## 4. 扩展性能结果

环境：

- Python 3.14.6；
- macOS arm64；
- 50 次迭代；
- Fake Model/Fake Tool；
- 无网络。

| 场景 | 迭代 | Median | P95 | 说明 |
|---|---:|---:|---:|---|
| Pipeline 测试装配 | 50 | 0.041 ms | 0.044 ms | 不含外部 Store |
| 无工具 Chat | 50 | 0.712 ms | 0.753 ms | Fake Model |
| 单 Tool Call | 50 | 1.294 ms | 1.447 ms | Fake Tool |
| 三次 Tool Call | 50 | 2.663 ms | 2.747 ms | 顺序执行 |
| ToolExecutor wrapper | 50 | 0.0015 ms | 0.0016 ms | 无 Hook/I/O |
| Chat Context | 50 | 0.177 ms | 0.186 ms | Memory 关闭 |
| Coding Context | 50 | 0.218 ms | 0.229 ms | Coding instructions |
| Chat Context + Memory | 50 | 0.176 ms | 0.188 ms | 本地文本 Memory |
| Chat + Null Trace | 50 | 0.767 ms | 0.785 ms | 不含磁盘 I/O |
| Chat Streaming | 50 | 0.671 ms | 0.680 ms | 两个文本 chunk |
| Model 前 Cancellation | 50 | 0.181 ms | 0.185 ms | 无 Model Call |
| Task Session Factory | 50 | 0.490 ms | 0.524 ms | 含临时目录创建/清理 |
| Subagent 创建并返回 | 50 | 2.477 ms | 2.703 ms | 单次 Fake Final |

机器可读结果：

```text
benchmarks/results/runtime_phase0.json
```

## 5. 本轮修改文件

| 文件 | 操作 | 内容 | 影响生产逻辑 |
|---|---|---|---:|
| `tests/test_runtime_phase0_extended_baseline.py` | 新增 | 扩展行为基线 | 否 |
| `tests/snapshots/runtime_phase0_trace_events.json` | 新增 | Trace 事件快照 | 否 |
| `benchmarks/runtime_phase0.py` | 修改 | 扩展离线性能场景 | 否 |
| `benchmarks/results/runtime_phase0.json` | 更新 | 新基准结果 | 否 |
| `docs/architecture/runtime-phase0-baseline.md` | 更新 | 补充行为结论 | 否 |
| `docs/architecture/runtime-phase0-benchmark.md` | 更新 | 新性能结果 | 否 |
| `docs/architecture/runtime-phase0-continuation-report.md` | 新增 | 本次执行报告 | 否 |

## 6. 仍未完成事项

仍受环境或现有依赖边界影响：

1. 完整 `TaskSessionRunner.run_coding_task` 离线测试仍依赖 PostgreSQL Session Store；
2. Artifact/Workspace Diff/Conclusion/Promotion 已有测试，但完整 Runner 集成需要
   PostgreSQL；
3. Trace 磁盘写入开关性能未测，当前只测 Null Trace；
4. Vector Memory/RAG 性能未测，当前只测本地文本 Memory；
5. Tool Handler 无抢占式 timeout/cancellation 接口，Phase 0 只能固定现状；
6. Gateway delivery failure 尚无统一错误事件快照；
7. 全量测试环境仍缺少完整声明依赖。

## 7. Phase 1 判断

相较首轮报告，以下区域的基线已经明显增强：

- Trace 事件顺序；
- Tool 执行中取消；
- Memory 注入；
- Task Session 创建；
- Subagent 隔离；
- 可选路径性能。

但涉及完整 Coding 生命周期和持久化的重构仍不应开始，除非先提供 PostgreSQL
测试 fixture，或增加不改变生产行为的 Fake Session Store 测试装配。

本轮没有修改任何生产行为，也没有发现无法解释的性能变化。
