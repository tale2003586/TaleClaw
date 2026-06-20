# Coding Task Session 与真实 Workspace

这篇文档讲 coding 请求如何接入真实项目目录，以及 workspace 改动如何被记录。

## 这层解决什么问题

普通聊天可以只在 session 里对话，但 coding 任务必须面对真实文件系统。

这里有几个关键问题：

- 用户给一个项目地址，agent 能不能在那个目录里工作。
- 工具路径能不能逃出 workspace。
- 一次 coding task 的中间过程是否污染主聊天 session。
- 用户停止后，任务进度能不能带回父 session 等下一轮续做。
- 任务完成后怎么知道 workspace 改了哪些文件。
- 任务结论怎么进入长期记忆候选，而不是直接乱写全局 memory。

当前这些问题由 `agents/coding/runner.py` 和 `runtime/workspace.py` 解决。

## 什么时候进入 coding task

`AgentLoop.run_once()` 里判断：

```python
if self.task_session_runner is not None and route.profile.tool_mode == "coding":
    reply = self.task_session_runner.run_coding_task(...)
```

也就是说，是否进入 coding task 不是用户随便调用工具决定的，而是 route/profile 决定的。

如果 inbound metadata 里带了 `workspace_root`，会传给 task runner：

```python
workspace_root = (inbound.metadata or {}).get("workspace_root")
```

## WorkspaceResolver

`WorkspaceResolver` 的职责是把用户请求的 workspace 转成安全的 `WorkspaceRef`。

它会检查：

- requested path 是否存在。
- 是否是目录。
- 是否在 allowed roots 内。

如果没有 requested workspace，会 fallback 到 `DEFAULT_CODING_WORKSPACE`。

返回的 `WorkspaceRef` 包含：

- `root`
- `display_name`
- `allowed_root`
- `source`
- `requested`

这些会通过 `to_metadata()` 写入 session metadata。

## allowed roots

`WorkspaceResolver` 默认读取配置：

- `WORKSPACE_ROOTS`
- `DEFAULT_CODING_WORKSPACE`
- `WORKDIR`

如果请求路径不在 allowed roots 下，会抛错：

```python
Workspace is outside allowed roots
```

这是真实 workspace 接入最重要的安全边界。

## path guard

文件工具最终会通过：

```python
safe_workspace_path(path, session=session)
```

它会拒绝：

- 绝对路径。
- 包含 `..` 的路径。
- resolve 后不在 workspace root 下的路径。

所以即使模型给了 `../../secret`，handler 也不会执行到 workspace 外。

## task session 为什么要隔离

`TaskSessionRunner.run_coding_task()` 会创建一个新的 task session：

```python
record = self.factory.create(
    parent_session_id=parent_session.id,
    task_type="coding",
    user_request=user_text,
    user_id=...,
    user_role=...,
)
```

父 session 和 task session 的区别：

- 父 session 保存用户请求和最终摘要。
- task session 保存 coding 中间消息、工具结果和 task-local memory。
- 父 run 的 `run_state.metadata["task_session"]` 会记录 task id、task session id 和任务状态。
- 父 session 的 working memory 会继承到 task session，task 完成后再同步回父 session。

这样 coding 的探索过程不会把主聊天上下文弄得很长，也不会直接污染主 session。

## task request 怎么构造

task session 的第一条 user message 会包一层：

```text
<task-session parent_session="...">
You are running in an isolated coding task session...
</task-session>

<global-memory-snapshot>
...
</global-memory-snapshot>

User coding task:
...
```

这让 task agent 知道：

- 自己是隔离 task。
- 中间发现写 task-local memory。
- 可复用结论应该进入 pending，再由后处理晋升。
- 可以参考 global memory snapshot。

## task pipeline

task runner 不直接复用主 pipeline 的 memory lifecycle。

它会 fork 一个 task pipeline：

```python
task_pipeline = self.base_pipeline.fork(
    context_builder=ContextBuilder(memory_store=task_memory),
    memory_lifecycle=TaskMemoryLifecycle(task_memory),
)
```

这表示：

- 模型、工具、executor、reflection 等能力沿用主 pipeline。
- context builder 换成 task-local memory。
- memory lifecycle 换成 task-specific lifecycle。

## working memory 继承和同步

如果 `WORKING_MEMORY_CHECKPOINT_ENABLED` 开启，coding route 在进入 task session 前会先在父 session 准备 working memory。

task session 创建后会调用：

```python
inherit_working_memory(
    source_session=parent_session,
    target_session=record.session,
    objective=user_text,
    task_id=record.task_id,
)
```

这样 task session 能在上下文中看到上一轮留下的 completed/pending units。

task pipeline 结束后会调用：

```python
sync_working_memory(
    source_session=record.session,
    target_session=parent_session,
)
```

所以父 session 是跨 turn 保存断点状态的地方，task session 是单次 coding run 内消费和更新断点状态的地方。

这个机制不是从某个工具调用继续执行；它是把“已完成/待继续/停止原因”注入下一轮 coding 上下文，让模型基于这些状态续做。

## workspace snapshot 和 diff

如果传入了 `run_state` 和 `trace_store`，coding task 会记录 workspace 改动。

task session 启动时会先写：

```text
workspace.resolved
task_session_started
```

同时把父 run metadata 更新成：

```json
{
  "task_session": {
    "task_id": "coding-...",
    "task_session_id": "task:coding-...",
    "status": "running"
  }
}
```

开始前：

```python
workspace_before = capture_workspace_snapshot(workspace.root)
trace_store.append_event(..., WORKSPACE_SNAPSHOT_CAPTURED, {"phase": "before", ...})
```

结束后：

```python
workspace_after = capture_workspace_snapshot(workspace.root)
workspace_diff = write_workspace_artifacts(...)
trace_store.append_event(..., WORKSPACE_DIFF_WRITTEN, ...)
```

这会在 run 目录里写 workspace diff artifact，并在 trace 中记录摘要。

diff 能覆盖：

- created
- modified
- deleted

task 完成后，父 run metadata 会更新为 task 报告，包括：

- `task_id`
- `task_session_id`
- `status`
- `task_log_path`
- `conclusions_path`
- `promoted_count`
- `skipped_count`
- `rejected_count`
- `workspace_diff`

同时 trace 会写 `task_session_completed`。如果后续要补失败路径，应保持同样的父 run metadata 边界，避免只在 task session 内部留下状态。

## task artifacts

任务完成后会尝试写 task artifacts：

- `TASK_LOG.md`
- `CONCLUSIONS.json`

路径会写入 task session metadata：

- `task_log_path`
- `conclusions_path`

如果 artifact 写失败，会记录：

```python
record.session.metadata["artifact_error"]
```

## 结论抽取和晋升

coding task 完成后会执行：

```python
extraction = self.conclusion_extractor.extract(...)
promotion = TaskMemoryPromoter(global_memory).promote(...)
```

目标是从 task 过程里抽出可复用项目结论，然后进入 global pending memory。

这不是把所有 task memory 直接写入全局 memory，而是先筛选和标记。

## 当前边界

当前 coding workspace 已经能真实改文件并记录 diff，但还没有：

- git branch 自动隔离。
- 自动回滚。
- patch preview 审批。
- 多 workspace 并行锁。
- task 中断后从某个 exact reasoning step 或 tool call 重放。

现在的重点是：workspace 解析、路径防逃逸、task session 隔离、working memory 续做提示、workspace diff 证据。

## 总结

coding 功能的核心不是给普通聊天加写文件工具，而是建立一个隔离 task session，并把真实 workspace 绑定到这个 task。

`WorkspaceResolver` 管入口安全，文件工具管路径安全，task session 管上下文隔离，trace/workspace diff 管执行证据。
