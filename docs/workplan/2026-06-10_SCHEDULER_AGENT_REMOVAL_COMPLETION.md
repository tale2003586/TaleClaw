# Scheduler Agent 移除完成记录

日期：2026-06-10

## 任务背景

项目当前主线收束为 Chat Runtime + Coding Agent。Scheduler / Scheduled Agent 是独立复杂子系统，已经不再作为核心能力保留，因此本次直接移除定时任务 agent、scheduler 插件、worker、专属测试和专属文档。

## 改动范围

删除：

- `plugins/scheduler/`
- `scheduler_worker.py`
- `modes/automation.py`
- `docs/scheduler/`
- `tests/test_scheduler_plugin.py`
- `tests/test_scheduler_planning.py`
- `.scheduler/`

更新：

- `runtime/bootstrap.py`
- `runtime/kernel.py`
- `runtime/pipeline.py`
- `runtime/reasoning_loop.py`
- `runtime/routing/intent.py`
- `models/model_pool.py`
- `tools/policy.py`
- `tools/handlers.py`
- `docker-compose.yml`
- `requirements.txt`
- `.gitignore`
- `docs/README.md`
- `docs/overview/PROJECT_STRUCTURE.md`
- `docs/overview/PLATFORM_AND_CODING_AGENT.md`

## 核心实现

- Runtime 不再注册 `SchedulerPlugin`。
- `RuntimeKernel` 不再持有 `scheduler_plugin`。
- `ModelPool` 移除 `scheduled_agent`、`scheduler_plan`、`scheduler_analyze` route。
- `Pipeline` 不再根据 `session.metadata.kind == "scheduled_agent"` 切换模型用途。
- `ReasoningLoop` 移除 unattended automation budget 和 runtime approval pause 分支。
- `ToolPolicy` 移除 scheduled agent capability approval 特判。
- Docker Compose 移除 `scheduler-worker` 服务和 `.scheduler` 挂载。
- Python 依赖移除 `APScheduler`。

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
