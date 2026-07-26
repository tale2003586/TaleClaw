# Phase 16：提取 Prompt Assets 与 Memory Context 服务

`ContextBuilder` 不再实现以下基础设施逻辑：

- Instruction 文件发现、读取、缓存、截断、报告与 fingerprint；
- Skill catalog 加载、渲染与 fingerprint；
- Durable Memory store 选择、recall/read 与 Context 标签渲染；
- Working Memory renderer 调用。

这些职责分别由以下显式服务承担：

```text
PromptAssetsService
├─ instruction files
├─ instruction cache and budgets
├─ skill catalog
└─ prefix fingerprint

ContextMemoryService
├─ durable memory rendering
└─ working memory rendering
```

`ContextBuilder` 只负责按既有顺序编排服务结果。为保持本阶段兼容，
`instruction_root`、`instruction_limit`、`skill_loader`、`memory_store` 和
`working_memory_renderer` 构造参数仍会适配为服务；下一阶段再迁移调用方并删除这些
兼容参数。

验证结果：

- Phase 16 定向与相邻测试：48 passed；
- 完整回归：437 passed，39 skipped；
- 安全/Workspace/Tool 组合测试：61 passed，1 skipped；
- Token：Chat 730、Coding 2347；
- Runtime facade：0.713ms；
- Chat/Coding Context：0.181/0.222ms；
- `ContextBuilder` 从 1478 行降至 1272 行；
- 快照无变化，未发现性能退化。
