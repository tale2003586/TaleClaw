# Runtime 目录结构重组完成记录

日期：2026-06-10

## 任务背景

本次任务将项目目录从早期的 `core/`、`tasksessions/` 混合结构，调整为更符合当前定位的结构：

```text
Agent Runtime Platform
  + Models
  + Vertical Agents
```

目标是让平台主链路、模型层、Coding Agent 应用边界更清晰。初始迁移保留旧 import 路径兼容层，随后已将这些兼容层归档到 `legacy/compat/`。

## 改动范围

新增主要包：

- `runtime/`
- `runtime/trace/`
- `runtime/routing/`
- `models/`
- `agents/`
- `agents/coding/`

迁移主要模块：

- `core/agent_loop.py` -> `runtime/agent_loop.py`
- `core/runtime.py` -> `runtime/app_runtime.py`
- `core/bootstrap.py` -> `runtime/bootstrap.py`
- `core/kernel.py` -> `runtime/kernel.py`
- `core/pipeline.py` -> `runtime/pipeline.py`
- `core/agent_runner.py` -> `runtime/agent_runner.py`
- `core/reasoning_loop.py` -> `runtime/reasoning_loop.py`
- `core/run_state.py` -> `runtime/trace/run_state.py`
- `core/trace_store.py` -> `runtime/trace/trace_store.py`
- `modes/router.py` -> `runtime/routing/router.py`
- `modes/intent.py` -> `runtime/routing/intent.py`
- `modes/execution_plan.py` -> `runtime/routing/execution_plan.py`
- `core/provider.py` -> `models/provider.py`
- `core/model_pool.py` -> `models/model_pool.py`
- `core/model_task_runner.py` -> `models/model_task_runner.py`
- `tasksessions/*` -> `agents/coding/*`

## 核心实现

- 新代码路径统一切到 `runtime.*`、`models.*`、`agents.coding.*`。
- `core/*` 兼容 shim 已归档到 `legacy/compat/core/`。
- `modes/router.py`、`modes/intent.py`、`modes/execution_plan.py` 兼容 shim 已归档到 `legacy/compat/modes/`。
- `tasksessions/*` 兼容 shim 已归档到 `legacy/compat/tasksessions/`。
- 仓库内旧路径 import 已切换到新路径。
- `runtime/routing/` 继续复用 `modes.base`、`modes.bot`、`modes.coding` 中的 profile 定义。
- 更新了 `docs/overview/PROJECT_STRUCTURE.md`，说明新目录边界和兼容策略。

## 验证方式

执行全量测试：

```bash
python -m unittest discover -s tests -v
```

结果：

```text
Ran 169 tests
OK
```

## 后续建议

- 稳定一段时间后，可以删除 `legacy/compat/` 中的旧兼容 shim。
- Scheduler 目前仍主要位于 `plugins/scheduler/`，后续可以把执行型逻辑抽到 `agents/scheduler/`，插件层只保留命令和注册胶水。
- 网关入口仍分布在根目录 worker 与 `gateway/` 下，后续可统一收束到 `entrypoints/` 或 `gateways/`。
