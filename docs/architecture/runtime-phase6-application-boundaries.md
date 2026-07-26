# TaleClaw Runtime Phase 6：Application 与 Optional Extension 边界

> 实施日期：2026-07-23

## CodingApplication

原 `TaskSessionRunner` 的真实职责包括 workspace 解析、隔离 Session、handoff、
Working Memory、artifact、diff、结论提取和 Memory promotion。这是应用生命周期，
不是通用 Runtime 机制，因此主类现命名为 `CodingApplication`。

`TaskSessionRunner` 暂时是同一类的兼容别名；Bootstrap 与 AgentLoop 的内部主路径
已经使用 `coding_application`。旧构造参数和属性留到 Phase 7 删除。

## Child Run

Subagent 继续通过统一 `Runtime.run()` 执行，但现在创建显式 `ChildRun`：

- 独立 `child_*` run identity；
- 显式 `parent_run_id`；
- identity 写入 Child Run 的 `RunExecutionState`；
- 现有 Trace span 和落盘布局保持兼容。

## Optional Extensions

新增 `RuntimeExtensions`，将 Memory、Retrieval、Artifact、Trace 和 MessageBus
表达为应用显式组合的可选服务。核心 `RunContext` 默认值不启用任何扩展，因此
`Runtime.run()` 默认路径不会强制构造、序列化或持久化这些能力。App Bootstrap
仍按当前产品配置组装既有服务，避免生产行为变化。

## Gate

- Phase 6 定向及相邻兼容测试：39 passed；
- 完整回归：415 passed，39 skipped；
- Prompt、Tool Schema、权限、Streaming 快照：无变化；
- `pip check`、`compileall`、`git diff --check`：通过；
- Runtime facade median：0.732 ms（Phase 5：0.751 ms）；
- Subagent median：2.580 ms（Phase 5：2.560 ms）。

没有明显性能退化，也没有新增强制扩展。

