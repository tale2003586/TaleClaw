# 会话类型、Profile 与工作记忆边界

> 历史说明：本文的 WorkingMemory/CodingContextState 双状态、metadata 双持久化和固定近期组描述
> 已被 TaskState 架构替代。当前只有 `TaskState` 是任务事实的可变权威；WorkingMemory 是兼容投影，
> CodingContextState 是 renderer/checkpoint metadata。请以
> [`TASK_STATE_CONTEXT_ARCHITECTURE.md`](../architecture/TASK_STATE_CONTEXT_ARCHITECTURE.md)
> 为准。本文以下内容仅用于理解迁移来源。

这篇文档专门说明三个容易混在一起的概念：

- session 是运行时状态容器。
- profile 是本轮 agent 身份和工具模式的静态/临时配置。
- working memory 是 coding 任务的跨 turn 状态，存放在 session metadata 中。
- coding context state 是 coding task turn 内的上下文压缩状态，也存放在 session metadata 中，但不承担跨 turn 任务队列职责。

## 关键代码入口

- `sessions/session.py`
- `sessions/session_store.py`
- `runtime/agent_loop.py`
- `runtime/routing/router.py`
- `runtime/routing/execution_plan.py`
- `modes/base.py`
- `modes/bot.py`
- `modes/coding.py`
- `runtime/context.py`
- `runtime/working_memory.py`
- `runtime/coding_context_state.py`
- `runtime/tool_signature.py`
- `runtime/coding_handoff.py`
- `agents/coding/session.py`
- `agents/coding/runner.py`
- `agents/subagent/runner.py`
- `coding_runtime/teammate.py`

## Session 的基础结构

当前系统只有一个通用 `Session` 数据结构：

```python
@dataclass
class Session:
    id: str
    messages: list[dict[str, Any]]
    current_mode: str = "hybrid"
    created_at: str = ...
    updated_at: str = ...
    last_compacted: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

也就是说，session 类型不是靠不同 class 区分，而是靠这些字段区分：

- `id`
- `current_mode`
- `metadata["kind"]`
- `metadata` 里的 task/workspace/user/working memory 信息

落库层只保存 session 元信息和消息 JSON：

- `sessions` 表：`id`、`current_mode`、时间戳、`metadata`
- `messages` 表：`session_id + seq`、`role`、`timestamp`、`message_json`

## 当前的 session 类型

| 类型 | id 形态 | current_mode | metadata.kind | 是否常规持久化 | 作用 |
| --- | --- | --- | --- | --- | --- |
| 父级用户 session | `web:<...>` / `cli:<...>` / `telegram:<...>` | `hybrid` / `bot` / `coding` | 通常为空 | 是 | 保存用户可见主对话、身份、workspace、last route、working memory |
| coding task session | `task:coding-<id>` | `coding` | `task_session` | 是 | 保存一次 coding task 的中间推理、工具结果、task-local memory |
| subagent session | `subtask:<agent_type>:<id>` | `coding` | `subagent` | 通常不经 `SessionManager.save()` | 短生命周期局部事实抽取或 scout 任务 |
| teammate session | `teammate:<name>` | `teammate` | `teammate` | 不走普通 ContextBuilder 持久化路径 | 长期协作角色，通过 team bus 接收任务 |

## 父级用户 session

入口消息进入 `AgentLoop.run_inbound()` 后，会通过：

```python
session = self.sessions.get_or_create(inbound.session_key)
```

拿到父级用户 session。

这个 session 负责保存：

- 用户可见消息历史。
- `current_mode`。
- `metadata.user_id` / `metadata.user_role`。
- `metadata.last_route`。
- Web/CLI 传入的 `workspace_root`。
- 当前 run id，例如 `active_run_id` / `last_run_id`。
- coding 断点状态 `metadata["working_memory"]`。
- coding 父会话摘要，例如 `metadata["coding_conversation_summary"]` 和待写入历史的 `metadata["pending_coding_task_summary"]`。

普通 bot 路径会直接用这个 session 调 `Pipeline.run()`。

coding 路径会先把用户请求记录在父 session，再创建隔离 task session 执行真实 coding 循环；父 session 最后只追加 task 摘要回复。

## Coding task session

coding 任务由 `TaskSessionFactory.create()` 创建：

```python
task_id = f"{_slug(task_type)}-{uuid4().hex[:8]}"
session_id = f"task:{task_id}"
session.current_mode = task_type
session.metadata["kind"] = "task_session"
```

task session 的职责是：

- 承载 coding agent 的完整中间消息。
- 保存工具调用和工具结果。
- 绑定真实 workspace metadata。
- 使用 task-local memory。
- 结束后写 task artifacts、结论抽取、可晋升记忆和 workspace diff。
- 持有 task turn 内的 `coding_context_state`，用于压缩长 active turn。

task session 的向量历史 scope 是：

```text
task:<task_id>
```

这样 coding 中间过程不会直接进入父级用户 session 的普通历史上下文。

## Subagent session

短生命周期子 agent 由 `TaskSubagentRunner._new_session()` 直接构造：

```python
Session(
    id=f"subtask:{agent_type}:{uuid}",
    current_mode="coding",
    metadata={"kind": "subagent", ...},
)
```

subagent 会复制父 session 的部分 metadata：

- `user_id`
- `user_role`
- `workspace_root`
- `workspace_display_name`
- `workspace_allowed_root`
- `workspace_source`
- `workspace_requested`

如果父 task session 有 working memory，subagent 会拿到一个快照视图：继承 completed evidence 和 observed calls，但不继承父 pending queue。这样子任务可以复用证据、避免重复读取，同时不会接管父 agent 的整体任务计划。

它使用临时 profile：

```python
ModeProfile(
    name=f"subagent:{agent_type}",
    tool_mode="coding",
    system_prompt=SUBTASK_SYSTEM_PROMPTS[agent_type],
)
```

subagent session 的结果通过 `SubagentResult` 和 trace 回传。它不是长期会话，也通常不会通过 `SessionManager.save()` 落到主 session store。

## Teammate session

teammate 由 `coding_runtime/teammate.py` 创建：

```python
Session(
    id=f"teammate:{name}",
    current_mode="teammate",
    metadata={"kind": "teammate", ...},
)
```

teammate 不使用普通 `ContextBuilder`。它有自己的 `TeammateContextBuilder`：

```text
system prompt
+ teammate session.messages
+ team bus inbox
```

它的 profile 也是运行时临时构造的，`tool_mode="teammate"`。

## Profile 存在哪里

主 profile 是代码里的静态对象，不是数据库记录：

- `modes/base.py` 定义 `ModeProfile`
- `modes/bot.py` 定义 `BOT_PROFILE`
- `modes/coding.py` 定义 `CODING_PROFILE`

`ModeProfile` 当前只有：

```python
name: str
system_prompt: str
tool_mode: str
```

每轮 inbound 会由 `ModeRouter` 和 `ExecutionPlanner` 重新选择 `route.profile`。session 里不会保存完整 profile，只会保存：

- `current_mode`
- `metadata["last_route"]` 里的 profile 名、tool mode、intent、execution、confidence、reason

所以恢复 session 后，系统会基于当前 `current_mode` 和新用户消息重新路由，而不是从数据库反序列化一个旧 profile 对象。

## Profile、instruction md 和 system prompt

`ContextBuilder` 每次 build context 时会组合：

```text
profile.system_prompt
instruction_block
runtime_guidance
```

instruction 文件按 `profile.tool_mode` 选择：

- `tool_mode == "coding"`：读取 `.agent/coding.md` 和 `AGENTS.md`
- 其他模式：读取 `.agent/assistant.md`

这些文件会被包装为：

```xml
<instructions section="mode_instructions" sources=".agent/coding.md">
...
</instructions>
```

当前设计意图是：

- `modes/*.py` 里的 `system_prompt` 保持短而稳定，描述身份、权限和硬边界。
- `.agent/*.md` 放可迭代策略，例如工作流、子 agent 编排、汇报标准。
- `runtime_guidance` 放 runtime 层必须注入的通用提示，例如 memory 和 deferred tools 规则。

## 上下文内的 active turn 切分

`Pipeline._run_turn()` 在 turn 开始时记录最新 user message 的 index：

```python
active_turn_start_index = _last_user_message_index(session.messages)
```

之后每个 reasoning step 调 `ContextBuilder.build()` 时都会传入这个 index。

`ContextBuilder._split_active_turn()` 会把 session messages 分成：

- `conversation_history`：当前 turn 之前的历史。
- `active_turn`：本轮用户消息之后的 assistant/tool 消息。
- `current_request`：active turn 内第一条 user message。

这让最近工具调用链在裁剪时得到保护，避免 provider 协议要求的 assistant tool call / tool result 对应关系被破坏。

## Working memory 存在哪里

working memory 存在 session metadata 里，不是独立表：

```text
session.metadata["working_memory"]
session.metadata["working_memory_resume_requested"]
```

数据结构由 `runtime/working_memory.py` 的 `WorkingMemory` 定义：

- `task_id`
- `objective`
- `completed_units`
- `pending_units`
- `archived_findings`
- `step_checkpoints`
- `observed_calls`
- `last_checkpoint_step`
- `status`
- `updated_at`

`pending_units` 当前会规范化为带优先级和状态的动作项：

- `unit_id`
- `description`
- `scope_files`
- `priority`：`P0` / `P1` / `P2`
- `state`：`todo` / `in_progress` / `blocked`
- `blocked_by`
- `last_failure_reason`

`observed_calls` 是 observation ledger：

- `signature`：由 `tool_call_signature(tool, arguments)` 生成。
- `tool`
- `arguments`
- `label`
- `gist`
- `step`
- `info_gain`
- `count`
- `result_hash`

它只保存短 gist 和 hash，不保存工具结果原文；原文留在 active turn 或 tool result store。

状态目前包括：

- `running`
- `suspended`
- `completed`

## Working memory 的生命周期

coding route 执行前，`AgentLoop._execute()` 会调用：

```python
prepare_working_memory_for_turn(
    session,
    objective=inbound.content,
    resume_requested=is_resume_request(inbound.content),
    task_id=session.id,
)
```

进入 task session 后，`TaskSessionRunner.run_coding_task()` 会调用：

```python
inherit_working_memory(source_session=parent_session, target_session=record.session, ...)
```

task 执行结束后再调用：

```python
sync_working_memory(source_session=record.session, target_session=parent_session)
```

也就是说，父 session 保存跨 turn 的 working memory，task session 在一次 coding run 内继承并更新它。

reasoning loop 运行中还会在自然边界调用：

```python
checkpoint_reasoning_step(...)
```

这会追加紧凑 step checkpoint，并把本步 tool_calls/tool_results 转写成 `observed_calls`。`parallel_tasks` 分派和结果也会分别调用 `checkpoint_subtasks_dispatched()` / `checkpoint_subtask_results()`，把子任务状态写入 pending/completed。

## Working memory 什么时候进入上下文

`ContextBuilder._build_working_memory_block()` 只在这些条件满足时注入：

- `WORKING_MEMORY_RESUME_ENABLED` 为真。
- `profile.tool_mode == "coding"`。
- session 中有未完成的 working memory。

渲染形式是：

```xml
<working-memory task_id="..." status="...">
...
</working-memory>

<working-memory-protocol critical="true">
每个推理步开始前，按顺序执行：
...
</working-memory-protocol>
```

在 context frame 中，它位于 `security_knowledge` 之后、`task_runtime_events` 之前。

渲染内容包括：

- 下一步动作队列：标出 `NEXT`、可并行的 `PARALLEL`、priority、state、scope 和 blocked 信息。
- 已观察：列出不要重复的工具调用 gist，重复结果会标注 `seen` 或 `info_gain=false`。
- 已完成：列出结论、证据、未解问题和是否需要父级复核。
- 归档发现：包含最近停止原因、最终回答或最近 reasoning step。

如果 `CODING_CONTEXT_STATE_ENABLED=1`，working memory 不再以完整 block 直接进入 context frame，而是先被 `runtime/coding_context_state.py` 消费，合并进 `<coding-context-state>`。

## Coding context state 存在哪里

coding context state 也存在 session metadata：

```text
session.metadata["coding_context_state"]
```

它由 `runtime/coding_context_state.py` 的 `CodingContextState` 定义，核心字段包括：

- `task_id`
- `objective`
- `workspace_root`
- `active_turn_start_index`
- `prompt_tail_start_index`
- `compacted_until_index`
- `generation`
- `phase`
- `finish_condition`
- `coverage`
- `findings`
- `pending_actions`
- `open_questions`
- `do_not_repeat`
- `evidence_index`
- `observations`
- `last_compaction`
- `metrics`

它只在 coding profile/session 的 active-turn-only history 路径下使用，主场景是 coding task session。它用于把过长 active turn 压缩成状态消息，并保留最近几组原始 tool chain。它会从 working memory 同步 completed/pending/observed，但不会替代 working memory 的跨 turn 保存职责。

## 停止和恢复的真实边界

用户主动停止或保护性停止时，`ReasoningLoop` 会把 working memory 标记为 suspended，并把可用进展写入 `archived_findings`。

用户下一轮发送包含这些标记的文本时，会被识别为 resume request：

```text
/resume
resume
继续
续做
断点
接着
```

当前实现能做到：

- 保存已分派/已完成/待继续的子任务信息和动作队列。
- 保存已观察工具调用账本，减少重复读取和无信息增益调用。
- 保存最近停止原因和部分进展。
- 下次 coding context 注入 working memory，提示模型不要重做已完成线索。
- 在 coding context state 开启时，把旧 active turn 压缩成状态、观察和 evidence index。

当前还不能做到：

- 从某个 exact reasoning step 或 tool call 继续执行。
- 持久化一个可重放的任务队列。
- 自动判断所有任务单元的真实完成度。
- 把 working memory 从 session metadata 拆成独立 schema。
- 把 coding context state 当作可回放日志；它是压缩状态，不是原始 trace。

所以它是“状态化工作记忆和续做提示”，不是完整 workflow engine。

## 总结

当前边界可以这样理解：

```text
session = 运行时状态和消息容器
profile = 本轮 agent 身份、system prompt 和 tool_mode
instruction md = 可调策略层
working memory = coding 跨 turn 任务状态，存于 session metadata
coding context state = coding turn 内上下文压缩状态，存于 session metadata
```

这些状态一起决定模型每个 reasoning step 看到什么、能调用什么工具，以及 coding 任务在停止后能如何继续。
