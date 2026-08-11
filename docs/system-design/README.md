# System Design Notes (Historical Snapshot)

这组文档记录 2026 年 7 月重构前后的阶段性实现，包含已经删除的 Pipeline、
ModeProfile、WorkingMemory、CodingContextState 和 Memory lifecycle。当前架构以
[`../architecture/SYSTEM_ARCHITECTURE.md`](../architecture/SYSTEM_ARCHITECTURE.md)
为准；本目录仅用于历史追溯。

## 文档列表

- [00-全局总览](00-全局总览.md)
- [01-消息入口、会话与身份边界](01-消息入口、会话与身份边界.md)
- [02-运行时主循环与执行编排](02-运行时主循环与执行编排.md)
- [03-工具接入、可见性与执行护栏](03-工具接入、可见性与执行护栏.md)
- [04-模型路由与 Provider 池](04-模型路由与Provider池.md)
- [05-上下文构建、预算与记忆生命周期](05-上下文构建、压缩与记忆生命周期.md)
- [06-Coding Task Session 与真实 Workspace](06-CodingTaskSession与真实Workspace.md)
- [07-Trace、Run 工件与报告插件](07-Trace、Run工件与报告插件.md)
- [08-Coding Benchmark 与评测方法](08-CodingBenchmark与评测方法.md)
- [09-代码安全 RAG 知识库与检索路由](09-代码安全RAG知识库与检索路由.md)
- [10-数据持久化、索引与本地工件边界](10-数据持久化、索引与本地工件边界.md)
- [11-会话类型、Profile 与工作记忆边界](11-会话类型、Profile与工作记忆边界.md)
- [Memory Hierarchy 与演进边界](memory-hierarchy.md)
- [Relation / Evolution Proposal 运行时接入设计](relation-evolution-runtime-integration.md)
