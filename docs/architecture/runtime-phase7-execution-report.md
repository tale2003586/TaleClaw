# TaleClaw Runtime Phase 7 执行回复

Phase 7 已完成。

- Runtime 与 turn execution 已合并到 `runtime/runtime.py`；
- 删除 `runtime/pipeline.py` 和临时 `execution_engine.py`；
- 删除 ModeProfile、ModeRouter 和整个 `modes/` 目录；
- Bot/Coding AgentSpec 集中在 `agents/definitions.py`；
- AgentRouter、ExecutionPlan 以 AgentSpec 为唯一 Agent 定义；
- Session/Store 统一使用 `active_agent`；
- Tool Registry/Plugins 统一使用 `allowed_agents`；
- Coding Application 替换旧 task-session identity 和 lifecycle event；
- 完整回归：416 passed，39 skipped；
- 安全定向回归：47 passed，1 skipped；
- Runtime facade median：0.710 ms；Subagent median：2.630 ms；
- 代码净减少约 872 行。

快照变化仅有两组，均属于 Phase 7 明确目标：

1. Tool catalog 字段 `enabled_modes` → `allowed_agents`；
2. Coding lifecycle event `task_session_*` → `coding_application_*`。

Prompt、Tool Schema 内容、权限集合和 Streaming payload 均未改变。
