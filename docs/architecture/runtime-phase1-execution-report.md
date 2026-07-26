# TaleClaw Runtime Phase 1 执行回复

本次已完成 Phase 1“统一 Agent 定义”。

实施内容：

- 扩展 AgentSpec 及 Model、Tool、Context、Termination、Limits、Spawn Policy；
- 新增 `BOT_AGENT_SPEC` 与 `CODING_AGENT_SPEC`；
- ModeRouter 返回 AgentSpec，并保留 ModeProfile；
- AgentLoop 将 AgentSpec 透传给 Chat/Coding Pipeline；
- Pipeline 优先使用显式 AgentSpec，同时兼容旧 Profile 调用；
- 增加 Phase 1 专项测试和性能对比。

验证：

```text
60 passed, 1 skipped
6 Coding Benchmark passed
75 passed, 2 skipped（Trace/Context/Tool 扩展回归）
```

Phase 0 快照、Prompt、Tool Mode、Coding 生命周期和性能均未发生不兼容变化。
`Runtime.run()` 未在本阶段实现，它属于 Phase 2。

详细设计与证据见 `docs/architecture/runtime-phase1-agent-definition.md`。
