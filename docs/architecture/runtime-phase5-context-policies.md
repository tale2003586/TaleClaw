# TaleClaw Runtime Phase 5：Context Policy 与 Providers

> 实施日期：2026-07-23

## 实现

`ContextBuilder` 的数据选择职责已拆分为顺序明确的 Providers：

1. `PromptContextProvider`
2. `HistoryContextProvider`
3. `MemoryContextProvider`
4. `RetrievalContextProvider`
5. `CodingContextProvider`

Provider 负责选择和预算各自的数据；`ContextBuilder` 在兼容期继续按原顺序组合
消息并生成 `ContextBuildReport`。这保留了 Prefix cache、Budget、Coding Context
压缩、Security RAG trace 和消息顺序。

`AgentSpec.context_policy` 现在沿 Pipeline 传给 ContextBuilder。`include_history`
和 `include_memory` 已成为真实执行策略，而不再只是声明字段。Bot/Coding 默认策略
均保持开启，因此生产默认路径没有 Prompt 或 Token 行为变化。

## Gate

- 定向 Context/快照/AgentSpec/RunContext：43 passed；
- 完整回归：411 passed，39 skipped；
- Phase 0 Context 快照：无变化；
- `pip check`、`compileall`、`git diff --check`：通过；
- 独立 lint、format、type-check：仓库未配置。

性能见 `benchmarks/results/runtime_phase5.json`：

| 场景 | Phase 4 | Phase 5 |
|---|---:|---:|
| Runtime facade | 0.718 ms | 0.751 ms |
| Chat no-tool | 0.700 ms | 0.763 ms |
| Chat context build | 0.182 ms | 0.185 ms |
| Coding context build | 0.224 ms | 0.223 ms |

直接 Context 构建基本持平；端到端亚毫秒测量有约 0.03～0.06 ms 波动，没有新增
序列化、持久化、MessageBus、Memory/RAG 或远程调用。

