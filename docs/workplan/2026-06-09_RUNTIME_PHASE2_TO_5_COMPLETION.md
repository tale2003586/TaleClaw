# Runtime 第二至第五阶段完成记录

日期：2026-06-09

关联计划：[Runtime 平台化收束改进计划](2026-06-09_RUNTIME_PLATFORM_REFOCUS_PLAN.md)

## 任务背景

本次任务继续推进 Runtime 平台化收束，覆盖原计划中的第二、三、四、五阶段：

- 第二阶段：RuntimeKernel。
- 第三阶段：ToolPolicy。
- 第四阶段：拆分 ModeRouter。
- 第五阶段：Coding Agent 产品化文档。

## 改动范围

新增：

- `core/kernel.py`
- `tools/policy.py`
- `modes/intent.py`
- `modes/execution_plan.py`
- `docs/overview/PLATFORM_AND_CODING_AGENT.md`
- `docs/overview/CODING_AGENT.md`

改动：

- `core/runtime.py`
- `core/bootstrap.py`
- `tools/tool_registry.py`
- `modes/router.py`
- `docs/README.md`
- `docs/workplan/2026-06-09_RUNTIME_PLATFORM_REFOCUS_PLAN.md`

## 核心实现

### RuntimeKernel

新增 `RuntimeKernel`，集中持有平台核心依赖：

- bus
- sessions
- model_pool
- tools
- tool_executor
- memory_store
- plugin_manager
- router
- pipeline
- task_session_runner
- trace_store
- loop

`AppRuntime` 现在可以持有 `kernel`，同时保留原有 `bus` 和 `loop` 字段，避免破坏 Web、gateway 和测试中的旧访问方式。

### ToolPolicy

新增 `tools/policy.py`，把工具可见性和执行策略从 `ToolRegistry` 中抽离：

- `visible_tools(session, mode, run_context)`
- `can_execute(tool_name, args, session, mode, run_context)`
- `requires_approval(tool_name, args, session, mode, run_context)`

`ToolRegistry.visible_names_for_turn()` 和 `ToolRegistry.execution_error_for_turn()` 继续保留，但内部委托 `ToolPolicy`，保证兼容现有调用方。

### ModeRouter 拆分

新增：

- `modes/intent.py`：`IntentClassifier` 和 `IntentCandidate`
- `modes/execution_plan.py`：`ExecutionPlanner` 和 `ExecutionPlan`

`ModeRouter` 现在主要负责协调：

```text
IntentClassifier.classify()
  -> ExecutionPlanner.plan()
  -> ModeRouter 兼容 RouteResult
```

原有 route 行为保持兼容，hybrid classifier 仍用于 coding candidate 的二次判断。

### Coding Agent 文档化

新增平台和 Coding Agent 两份文档：

- `PLATFORM_AND_CODING_AGENT.md`
- `CODING_AGENT.md`

文档明确了 taleclaw 是 Agent Runtime Platform，Coding Agent 是平台上的仓库级垂直应用。

## 验证方式

已先执行关键回归测试：

```bash
python -m unittest discover -s tests -p 'test_hybrid_mode_routing.py' -v
python -m unittest discover -s tests -p 'test_pipeline_tool_loop_guard.py' -v
python -m unittest discover -s tests -p 'test_scheduler_planning.py' -v
python -m py_compile core/kernel.py core/runtime.py core/bootstrap.py tools/policy.py tools/tool_registry.py modes/intent.py modes/execution_plan.py modes/router.py
```

已执行全量测试：

```bash
python -m unittest discover -s tests -v
```

结果：

```text
Ran 169 tests
OK
```

## 后续建议

- 可以继续把 scheduler 的审批逻辑逐步迁移到 `ToolPolicy.requires_approval()`。
- 可以给 `RuntimeKernel` 增加更明确的 factory 或 builder，进一步瘦身 `bootstrap.py`。
- 可以给 `ExecutionPlan` 增加 `required_capabilities` 字段，为后续多垂直 Agent 做准备。
