# TaleClaw Runtime Phase 7 阻塞报告

> 日期：2026-07-23  
> 状态：阻塞已解除并完成（2026-07-23）

## 已安全删除

- 删除 `TaskSessionRunner` 兼容别名；
- 所有生产、Evaluation、脚本和测试导入改用 `CodingApplication`；
- 删除 AgentLoop 的 `task_session_runner` 参数和属性；
- 删除 AgentSpec 中的 `legacy_mode` metadata；
- 完整回归保持通过：414 passed，39 skipped；
- Runtime facade median 0.725 ms，无性能退化。

## 无法安全删除的路径

Phase 7 计划要求删除 ModeProfile、Pipeline、旧 AgentRunner、Core ModeRouter、
`current_mode`、`enabled_modes` 和旧 execution 字符串。但当前代码事实是：

1. `Runtime.run()` 仍以 Pipeline 为唯一实际执行实现；
2. Pipeline 仍负责 Context、Tool catalog、Memory lifecycle 和 turn adapter；
3. AgentRunner 仍由 Pipeline 创建并持有 ReasoningLoop；
4. ContextBuilder、Routing、Coding/Subagent profile 仍读取 ModeProfile；
5. ToolRegistry 的权限判定仍使用 `enabled_modes`；
6. Session routing 和 Coding lifecycle 仍读取/写入 `current_mode`；
7. Trace 和路由测试仍以 execution path 字段作为稳定契约。

因此直接删除会造成 Runtime 无执行路径、Tool 权限语义丢失、Session 路由回归以及
Trace 契约变化。Phase 0～6 没有建立足以替换这些路径的完整实现。按照“不绕过
Gate、不因报告与代码冲突而破坏生产行为”的规则，本阶段必须停止。

## 解除阻塞所需的补充工作

需要批准修订阶段计划，先增加一个兼容迁移阶段：

- 让 Runtime 直接拥有 AgentRunner、Context composer 和 lifecycle hooks；
- 将 RouteResult 改为 AgentSpec 主导，不再要求 ModeProfile；
- 将 Tool 权限从 mode 字符串迁到 AgentSpec ToolSet/authorization policy；
- 将 Session `current_mode` 迁为 application/agent identity，并提供存量数据迁移；
- 将 Trace execution 字符串升级为结构化 run/application identity；
- 全量双读/双写验证后，才能在独立删除阶段移除旧字段和类型。

用户已批准上述前置兼容迁移。Phase 7 恢复实施，但在全部替代路径和 Gate
完成前仍不得标记 completed。

## 解除结果

批准后的迁移已全部完成：

- Runtime 与执行引擎合并为单一 `runtime/runtime.py`；
- Pipeline、ExecutionEngine、ModeProfile、ModeRouter 已删除；
- `modes/` 目录已删除，AgentSpec 定义集中到 `agents/definitions.py`；
- Session 使用 `active_agent`，数据库启动时执行旧列一次性迁移；
- Tool authorization 使用 `allowed_agents`；
- 路由使用 AgentSpec/AgentRouter；
- Coding 生命周期事件和 metadata 使用 `coding_application`；
- 旧标识全仓生产搜索结果为空。

本报告保留为阻塞识别和解除过程的审计记录。
