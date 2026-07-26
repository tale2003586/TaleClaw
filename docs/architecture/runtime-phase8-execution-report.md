# TaleClaw Phase 8 执行回复

目录结构收敛已完成：

- Coding Application 及编排集中到 `applications/coding/`；
- Messaging 与 Sessions 收入 `runtime/`；
- Coding 专属 Context/Handoff 移出 Core；
- Background Memory 回归 `memory/`；
- 顶层生产包净减少三个；
- 打包配置、egg-info、README 和结构文档已同步。

完整回归为 416 passed、39 skipped；安全回归为 47 passed、1 skipped；性能无
退化。Prompt、Tool Schema、权限集合、Streaming 和行为快照均未变化。

详细证据见 `docs/architecture/runtime-phase8-directory-convergence.md`。

## 最终确认

用户已于 2026-07-23 确认目录结构与 Gate 结果。Phase 8 已正式结束，所有
改动仅保存在本地分支，未执行 push 或 merge。
