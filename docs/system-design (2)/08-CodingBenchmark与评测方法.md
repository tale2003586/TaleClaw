# Coding Benchmark 与评测方法

这篇文档讲当前 coding benchmark 怎么设计、怎么接入真实 runtime、怎么判断通过失败。

## 这层解决什么问题

agent 能跑一次不代表稳定。

benchmark 要解决的是：

- 任务是不是固定。
- 环境是不是隔离。
- 工具权限是不是明确。
- 结果是不是由 verifier 判断。
- workspace diff 和 trace 有没有被验证。
- 失败原因能不能归类。
- scripted baseline 和真实模型能不能分开。

当前实现位于：

- `benchmarks/coding_tasks.json`
- `benchmarks/fixtures/`
- `evaluation/task_schema.py`
- `evaluation/harness.py`
- `evaluation/verifiers.py`
- `evaluation/metrics.py`
- `plugins/eval_report`
- `scripts/run_evals.py`

## 任务合同

benchmark 文件是 JSON，schema version 当前为 1。

每条任务必须包含：

- `id`
- `category`
- `fixture_repo`
- `prompt`
- `step_budget`
- `allowed_tools`
- `expected`
- `script`
- `verifier`

`evaluation/task_schema.py` 的 `load_benchmark()` 会校验这些字段。

这让任务不只是自然语言 prompt，而是一份执行合同。

## fixture workspace

每条任务都有一个 `fixture_repo`。

`CodingBenchmarkHarness.run_task()` 会把 fixture 复制到新的 workspace：

```python
workspace = self._fresh_workspace(task, workspaces_root)
_init_git_repo(workspace)
```

这样每条任务都在干净副本里运行，前一条任务的修改不会污染后一条。

## scripted runner

默认 runner 是 `scripted`。

它使用 `ScriptedProvider`，按任务里的 `script` 返回固定 tool call 或 final answer。

这层非常重要，因为它可以先验证：

- benchmark harness 是否正确。
- 工具是否真的可见。
- workspace path guard 是否正常。
- verifier 是否可靠。
- trace 和 diff 是否能记录。

如果 scripted 都过不了，问题通常在平台或测试设计，而不是模型能力。

## real runner

`--runner real` 会使用：

```python
model_pool = get_model_pool()
model_pool.routed_provider("coding")
model_pool.model_for("coding")
```

这会把 benchmark 接到真实模型路由。

real runner 更适合评估模型和 prompt/profile 的能力，但它有波动，因此应该和 scripted baseline 分开看。

## benchmark 如何接入 runtime

每条任务都会创建一套隔离 runtime 组件：

- `TraceStore(eval_dir / "runs")`
- task scoped `SessionManager`
- `Pipeline(...)`
- `ToolExecutor`，带 `ShellSafetyHook`、`ShellWorkspaceScopeHook(workspace)`、`FileWriteScopeHook(workspace)`、`ToolLoopGuardHook`、`ToolTraceHook`
- `TaskSessionRunner(...)`
- `WorkspaceResolver(allowed_roots=[workspaces_root], default_workspace=workspace)`
- parent `Session(id=f"web:benchmark:{task.id}")`
- `RunState(channel="benchmark", chat_id=task.id, execution_path="task_session")`

这不是绕过 runtime 直接调工具。benchmark 走的是同一个 coding task runner 和 pipeline。

## allowed_tools 如何生效

benchmark 会根据任务的 `allowed_tools` 创建工具 registry：

```python
registry = build_lead_tool_registry()
for name in list(registry._tools):
    if name not in allowed:
        registry.unregister(name)
for name in allowed_tools:
    tool.always_on = True
```

额外允许：

- `tool_search`
- `recall_memory`
- `memorize`
- `load_skill`

任务允许的工具会被设为 always-on，避免 benchmark 本身被 deferred unlock 干扰。

所以 benchmark 测的是 coding 能否完成任务，而不是能不能先找到工具。

## 一条任务怎么判定通过

`run_task()` 会构造 row，并计算：

- `run_status == "completed"`
- `within_budget`
- `verifier_passed`
- `workspace_diff_passed`
- `trace_passed`

全部满足才算 pass：

```python
row["passed"] = (
    row["run_status"] == "completed"
    and within_budget
    and verifier_passed
    and workspace_diff_passed
    and trace_passed
)
```

这比只看最终回答可靠得多。

## verifier 检查什么

verifier 支持按任务定义检查：

- 文件是否被修改。
- 文件是否被创建。
- 文件是否不该被修改。
- 文件内容是否包含指定文本。
- 命令是否通过。
- trace 中是否出现某事件。
- 某工具是否被调用。
- 某工具是否被拒绝。

这让任务可以同时验证结果、行为和证据。

## 失败分类

`evaluation/metrics.py` 提供粗分类：

- `run_failed`
- `budget_exceeded`
- `verifier_failed`
- `workspace_diff_failed`
- `trace_missing`
- `unknown`

`evaluation/harness.py` 还提供更细的 `diagnose_failure()`，例如：

- `budget_stop`
- `write_tool_not_visible`
- `run_error`
- `test_command_failed`
- `expected_file_not_modified`
- `unexpected_file_modified`
- `expected_file_not_created`
- `expected_content_missing`
- `expected_tool_not_called`
- `expected_tool_denial_missing`
- `trace_event_missing`
- `missing_discovery_tool`
- `workspace_diff_mismatch`
- `trace_incomplete`

这些字段会写入 `rows.json`，便于定位失败是模型问题、平台问题、工具可见性问题还是 verifier 问题。

## 产物

一次 benchmark 会写：

```text
.evals/runs/<eval_id>/
```

主要文件：

- `summary.json`：完整 payload。
- `rows.json`：每条任务的结果行。
- `summary.md`：由 `plugins/eval_report` 生成的人类可读报告。
- `runs/<run_id>/`：每条任务的 run trace/report/metrics。
- `sessions/`：隔离 session db。
- `memory/`：隔离 memory。

如果 `--keep-workspace`，workspace 副本会保留，方便人工检查。

## 进度反馈

`scripts/run_evals.py` 支持 progress callback。

运行时会输出：

- eval started
- task started
- task finished
- eval finished

`--quiet` 可以关闭这类反馈。

## 常用命令

跑全部 scripted benchmark：

```bash
python scripts/run_evals.py --suite coding
```

跑单条任务并保留 workspace：

```bash
python scripts/run_evals.py --suite coding --task-id coding-git-diff-008 --keep-workspace
```

跑真实模型：

```bash
python scripts/run_evals.py --suite coding --runner real
```

临时关闭 benchmark 步数预算，观察真实模型不被 task `step_budget` 截停时能否完成：

```bash
python scripts/run_evals.py --suite coding --runner real --task-id coding-invalid-edit-recovery-009 --no-step-budget --keep-workspace
```

临时指定更大的步数上限：

```bash
python scripts/run_evals.py --suite coding --runner real --task-id coding-invalid-edit-recovery-009 --max-reasoning-steps 30 --keep-workspace
```

指定 eval root：

```bash
python scripts/run_evals.py --suite coding --eval-root /tmp/my-evals
```

## 当前边界

当前 benchmark 已经接入真实 runtime 和 workspace diff，但还没有：

- 多模型批量矩阵实验。
- 多次重复运行统计方差。
- 自动生成对比报告。
- sandbox 容器隔离。
- 真实项目大规模任务集。
- 按能力维度输出雷达图或可视化。

现在第一版更像是 coding runtime 的稳定性和行为证据测试。

## 总结

当前 coding benchmark 的重点不是给模型打一个模糊分数，而是把任务变成合同，把执行放进隔离 workspace，把结果交给 verifier，把行为交给 trace，把失败写成可诊断 row。

scripted runner 保证平台链路可靠，real runner 再评估模型能力。这个分层可以避免把平台 bug 和模型波动混在一起。
