# TaleClaw Phase 12 执行回复

默认 Agent Kernel 已不再自动构造或加载 Working Memory、Web Search Budget、
Finishing Reminder 和 Tool Batch 的具体策略。完整能力由生产 Composition Root
和 Coding Application 显式选择。

这使“最小 Runtime”与“完整产品 Runtime”成为真实、可测试的两种组合，而不是
依靠配置分支隐藏在同一个默认构造路径中。完整回归、安全、Token、性能与快照
Gate 全部通过。
