# Runtime 第一阶段完成记录：RunState + TraceStore

日期：2026-06-09

关联计划：[Runtime 平台化收束改进计划](2026-06-09_RUNTIME_PLATFORM_REFOCUS_PLAN.md)

## 任务背景

本阶段目标是给 taleclaw Runtime 增加统一的 run/turn 证据链，让普通对话、Coding TaskSession 和 Scheduled Agent 都能生成可审计的运行状态、事件 trace 和报告。

## 改动范围

新增：

- `core/run_state.py`
- `core/trace_store.py`
- `tests/test_run_trace.py`

改动：

- `core/agent_loop.py`
- `core/pipeline.py`
- `core/agent_runner.py`
- `core/reasoning_loop.py`
- `tasksessions/runner.py`
- `plugins/scheduler/agent_runner.py`
- `core/bootstrap.py`
- `.gitignore`

## 核心实现

- 新增 `RunState`，记录 `run_id`、session、channel、用户身份、mode、execution path、状态、推理步数、工具调用数、最后工具、停止原因、最终回复和错误信息。
- 新增 `TraceStore`，统一写入 `.runs/<run_id>/run_state.json`、`trace.jsonl`、`report.json`。
- `AgentLoop` 在收到 inbound message 时创建 run，并覆盖插件短路、模式切换、普通 pipeline、Coding TaskSession 四条路径。
- `ReasoningLoop` 记录 `model_requested`、`model_returned`、`tool_executed`、`reasoning_loop_completed`、`run_stopped` 等事件。
- `TaskSessionRunner` 将 coding 子会话绑定到 parent run，并把 task id、task log、conclusions 和 promotion 统计写回 run metadata。
- `ScheduledAgentRunner` 接入统一 `TraceStore`，同时保留原有 scheduler run id、`TOOL_TRACE.json` 和 markdown report 链路。
- `.runs/` 已加入 `.gitignore`，避免运行产物进入仓库。

## 验证方式

已执行全量测试：

```bash
python -m unittest discover -s tests -v
```

结果：

```text
Ran 169 tests
OK
```

## 当前效果

每次 runtime 执行后会生成：

```text
.runs/<run_id>/
  run_state.json
  trace.jsonl
  report.json
```

面试展示时可以用 `trace.jsonl` 说明模型调用、工具调用、loop guard、task session 绑定和 scheduler agent 执行过程。

## 后续建议

下一阶段可以继续推进 `RuntimeKernel`，把 `bootstrap.py` 中的依赖装配收束成平台核心对象。
