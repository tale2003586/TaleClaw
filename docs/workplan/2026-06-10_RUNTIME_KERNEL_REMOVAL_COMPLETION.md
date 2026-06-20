# RuntimeKernel 移除完成记录

日期：2026-06-10

## 任务背景

Scheduler Agent 移除后，`RuntimeKernel` 只剩依赖容器作用。`AppRuntime` 已经持有实际运行所需的 `bus` 和 `loop`，而 `AgentLoop` 又持有 `pipeline`、`router`、`plugin_manager`、`task_session_runner` 和 `trace_store`。继续在 `RuntimeKernel` 中重复保存这些引用会让 `bootstrap.py` 看起来比实际复杂。

## 改动范围

删除：

- `runtime/kernel.py`
- `legacy/compat/core/kernel.py`

更新：

- `runtime/app_runtime.py`
- `runtime/bootstrap.py`
- `docs/overview/PROJECT_STRUCTURE.md`
- `docs/overview/PLATFORM_AND_CODING_AGENT.md`

## 核心实现

- `AppRuntime` 只保留 `bus` 和 `loop`。
- `bootstrap.py` 不再构造 `RuntimeKernel`。
- 当前运行链路仍然是 `AppRuntime -> AgentLoop -> Pipeline / TaskSessionRunner`。

## 验证方式

执行全量测试：

```bash
python -m unittest discover -s tests -v
```

结果：

```text
Ran 135 tests
OK
```
