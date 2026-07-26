# TaleClaw Phase 12：显式可选执行策略

Phase 12 将默认 Agent Kernel 与具体产品策略分开。

`runtime/execution/policy_set.py` 只包含零副作用策略：

- 不启用 Web Search Budget；
- 不注入 Finishing Reminder；
- 不读写 Working Memory；
- 不自动合并 Task/Read Tool calls。

`runtime/execution/loop_policies.py` 保留完整产品策略，并通过
`standard_execution_policies()` 显式组合。生产 Bootstrap、CodingApplication
和 Teammate 明确选择标准策略；最小 Runtime/AgentRunner 默认使用最小策略。

直接导入 AgentRunner 已验证不会加载 Working Memory、具体策略、Application、
Plugin、Retrieval 或 Knowledge 模块。ReasoningLoop 和 AgentRunner 也不再直接
导入 `loop_policies.py`。

行为测试没有降低断言。原本测试可选能力的 fixture 现在显式装配标准策略，
继续验证 Search 限额、Finishing、Working Memory 和 Tool Batch 的原行为。

Gate：完整回归 425 passed/39 skipped，安全 64 passed/1 skipped，Context token
730/2347；Runtime Facade 0.713 ms，Subagent 2.435 ms，无性能退化，快照无变化。
