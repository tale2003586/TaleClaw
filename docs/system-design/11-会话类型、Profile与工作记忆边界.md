# 会话类型、Profile 与工作记忆边界

这篇文档专门说明三个容易混在一起的概念：

- session 是运行时状态容器。
- profile 是本轮 agent 身份和工具模式的静态/临时配置。
- working memory 是 coding 任务的断点式任务状态，存放在 session metadata 中。

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
- `last_checkpoint_step`
- `status`
- `updated_at`

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

<working-memory-instruction critical="true">
基于已完成部分继续完成任务；不要重做已完成线索...
</working-memory-instruction>
```

在 context frame 中，它位于 `security_knowledge` 之后、`task_runtime_events` 之前。

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

- 保存已分派/已完成/待继续的子任务信息。
- 保存最近停止原因和部分进展。
- 下次 coding context 注入 working memory，提示模型不要重做已完成线索。

当前还不能做到：

- 从某个 exact reasoning step 或 tool call 继续执行。
- 持久化一个可重放的任务队列。
- 自动判断所有任务单元的真实完成度。
- 把 working memory 从 session metadata 拆成独立 schema。

所以它是“断点式工作记忆和续做提示”，不是完整 workflow engine。

## 总结

当前边界可以这样理解：

```text
session = 运行时状态和消息容器
profile = 本轮 agent 身份、system prompt 和 tool_mode
instruction md = 可调策略层
working memory = coding 续做状态，存于 session metadata
```

这四者一起决定模型每个 reasoning step 看到什么、能调用什么工具，以及 coding 任务在停止后能如何继续。
