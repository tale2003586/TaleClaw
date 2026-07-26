# TaleClaw Runtime Phase 5 执行回复

Phase 5 已将 Prompt、History、Memory、Retrieval 和 Coding Context 建立为显式
Context Providers，并让 AgentSpec Context Policy 参与真实构建。

默认 Chat/Coding 行为、Prompt 顺序、Context 快照和 Token 估算保持不变。完整回归
为 411 passed、39 skipped，所有阶段 Gate 通过。

详细说明见 `docs/architecture/runtime-phase5-context-policies.md`。

