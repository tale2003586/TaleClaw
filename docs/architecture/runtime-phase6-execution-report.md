# TaleClaw Runtime Phase 6 执行回复

Phase 6 已完成应用和可选扩展隔离：

- Coding 生命周期主边界改为 `CodingApplication`；
- Subagent 成为带独立 identity 和 parent linkage 的 Child Run；
- Runtime 默认上下文不启用 Memory、Retrieval、Artifact、Trace 或 MessageBus；
- 兼容别名和 Trace 布局暂时保留，供 Phase 7 安全删除。

完整回归为 415 passed、39 skipped，全部 Gate 通过。详细说明见
`docs/architecture/runtime-phase6-application-boundaries.md`。

