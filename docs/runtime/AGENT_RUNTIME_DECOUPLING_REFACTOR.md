# Agent Runtime 解耦改造记录

本文记录本轮围绕 `Pipeline`、`ReasoningLoop` 和 persistent teammate 的后续改造。

## 背景问题

之前系统里已经有多条 agent 执行路径：

- 普通聊天 / Bot：`AgentLoop -> Pipeline`
- Coding TaskSession：`AgentLoop -> TaskSessionRunner -> Pipeline`
- 定时 agent：`SchedulerWorker -> ScheduledAgentRunner -> Pipeline`
- persistent teammate：`spawn_teammate -> TeammateManager._run_member`

前三条路径已经逐渐收拢到 `Pipeline`，但 persistent teammate 仍然保留了一套独立的手写循环：

1. 直接调用 `client.chat.completions.create(...)`。
2. 自己维护 `messages`。
3. 自己解析 tool call。
4. 自己调用 `make_teammate_handlers(...)`。
5. 不经过 `ToolRegistry` 的工具可见性控制。
6. 不经过 `ToolExecutor` 的 hook 链。
7. 不复用不可见工具循环保护、重复工具调用保护、模型池路由等能力。

这导致 teammate 和主 agent 的行为边界不一致。主 agent 已有的安全、审计、模型路由和循环保护能力，persistent teammate 无法自动继承。

## 第一阶段：抽出 ReasoningLoop

第一阶段已经完成：从 `core/pipeline.py` 中抽出了 `core/reasoning_loop.py`。

新的职责划分：

- `Pipeline`：负责一轮 turn 的外层流程。
- `ReasoningLoop`：负责模型调用、工具执行、继续推理、循环保护。

`Pipeline` 现在保留的事情：

- turn 开始时重置工具解锁状态。
- micro compact / auto compact。
- 构造上下文。
- 决定模型用途，例如 `chat`、`coding`、`scheduled_agent`。
- 一轮结束后触发记忆生命周期。

`ReasoningLoop` 接管的事情：

- 调用 provider 的 `chat` 或 `stream_chat`。
- 把 assistant 原始消息写回 session。
- 执行 tool calls。
- 处理 `compact` 工具。
- 处理工具执行 hook trace。
- 检测不可见工具反复调用。
- 检测 `tool_loop_guard` 拒绝。
- 检测普通推理步数上限。
- 检测 scheduled agent 的自动化预算。

这个阶段的关键收益是：模型-工具循环成为可复用模块，而不是被锁死在 `Pipeline` 内部。

## 第二阶段：给 ReasoningLoop 增加工具结果回调

为了让 teammate 也能复用 `ReasoningLoop`，本轮给 `ReasoningLoop.run(...)` 增加了一个可选参数：

```python
after_tool_calls: Callable | None = None
```

每批工具执行完成后，`ReasoningLoop` 会把 `ToolExecutionSummary` 交给调用方。

`ToolExecutionSummary` 新增：

```python
tool_results: list[dict]
```

每一项包含：

- `name`
- `output`
- `status`
- `final_arguments`

这个回调的用途是让外部 runtime 能监听工具协议事件。例如 teammate 调用 `idle` 后，外层调度器应该停止当前推理 cycle，进入空闲轮询状态；调用 `shutdown_response` 并通过后，外层调度器应该结束线程。

主 `Pipeline` 不传这个回调，因此普通聊天、Coding TaskSession 和定时 agent 的行为保持不变。

## 第三阶段：为 teammate 增加独立工具模式

本轮在 `ToolRegistry` 中新增了 `teammate` 工具模式。

teammate 模式下默认可见的工具包括：

- `read_file`
- `load_skill`
- `task_create`
- `task_update`
- `task_list`
- `task_get`
- `claim_task`
- `check_background`
- `send_message`
- `read_inbox`
- `idle`
- `shutdown_response`
- `plan_approval_request`
- `tool_search`

高风险或容易造成副作用的工具仍然通过 deferred 机制延迟解锁：

- `bash`
- `write_file`
- `edit_file`
- `background_run`

因此 teammate 如果要运行 shell 或写文件，需要先调用：

```text
tool_search(query="select:bash")
tool_search(query="select:write_file")
```

这让 teammate 和主 Coding agent 一样遵守工具可见性规则。

同时新增：

```python
build_teammate_tool_registry(name: str)
```

它会根据 teammate 名称构造专属工具注册表，并绑定 `make_teammate_handlers(name)`。

## 第四阶段：persistent teammate 接入 ReasoningLoop

旧版 `TeammateManager._run_member(...)` 内部直接调用 OpenAI SDK。

现在改为：

```text
TeammateManager._run_member()
  -> build_teammate_tool_registry(name)
  -> TeammateContextBuilder
  -> ReasoningLoop.run(...)
```

新的 teammate cycle 流程：

1. 创建 teammate 专用 `Session`。
2. 创建 teammate 专用 `ModeProfile`，其 `tool_mode` 为 `teammate`。
3. 创建 teammate 专用 `ToolRegistry`。
4. 每个工作 cycle 开始时重置本轮工具解锁状态和 hook 循环状态。
5. 调用 `ReasoningLoop.run(...)`。
6. `ReasoningLoop` 调模型。
7. 模型如需工具，则通过 `ToolExecutor` 执行。
8. 工具执行结果进入 `after_tool_calls` 回调。
9. 如果工具是 `idle`，当前 cycle 结束，外层进入 idle 轮询。
10. 如果工具是通过的 `shutdown_response`，teammate 状态变为 `shutdown`。
11. 如果没有特殊协议工具，cycle 正常结束，然后进入 idle 轮询。
12. idle 阶段继续检查 inbox 和任务看板。
13. 发现未认领任务后，自动 claim，并进入下一轮工作 cycle。

这样，persistent teammate 的“长期驻留 + 自动认领任务”能力保留了，但内部推理循环不再是另一套实现。

## 第五阶段：接入主系统运行时依赖

`core/bootstrap.py` 现在会把主系统的模型池和工具执行器注入给全局 `TEAM`：

```python
TEAM.configure(
    model_pool=MODEL_POOL,
    tool_executor=executor,
    max_tokens=pipeline.max_tokens,
    max_reasoning_steps=50,
)
```

这意味着 teammate 会复用：

- `ModelPool`
- provider fallback
- `FileWriteScopeHook`
- `ToolLoopGuardHook`
- `ToolTraceHook`
- 插件提供的 tool hooks

如果某些测试或轻量运行场景不经过 `bootstrap`，`TeammateManager` 仍然有默认运行时配置，可以独立构造和测试。

## 第六阶段：模型路由支持 teammate

`core/model_pool.py` 新增了 `teammate` purpose。

它默认 alias 到 `coding`，因此不额外配置时 teammate 会走 coding 路由。

可选配置：

```env
LLM_ROUTE_TEAMMATE=deepseek
```

如果想让 persistent teammate 使用便宜模型、快模型或更强 coding 模型，可以单独配置这个路由。

## 第二轮：第二阶段到第五阶段

用户指出上一轮还没有真正抽出 `AgentRunner`。本轮继续完成后续阶段。

### 第二阶段：抽出 AgentSpec 和 AgentRunner

新增：

- `core/agent_spec.py`
- `core/agent_runner.py`

`AgentSpec` 只描述 agent 的身份和运行选择：

```python
AgentSpec(
    name="teammate:alice",
    role="tester",
    profile=profile,
    model_purpose="teammate",
    max_tokens=8000,
    max_reasoning_steps=50,
)
```

它不负责执行，不持有线程，也不保存 session。

`AgentRunner` 负责把 `AgentSpec`、`session`、`context_builder` 和 `ReasoningLoop`
连接起来：

```text
AgentRunner.run_turn()
  -> 根据 AgentSpec.model_purpose 选择模型
  -> 创建 ReasoningLoop
  -> 调用 ReasoningLoop.run(...)
```

这样 `Pipeline` 和 `TeammateManager` 不再直接拼 `ReasoningLoop` 的全部参数。

### 第三阶段：Pipeline 接入 AgentRunner

`Pipeline` 现在不再直接持有 `ReasoningLoop`，而是持有 `AgentRunner`。

新的主会话路径：

```text
AgentLoop
  -> Pipeline.run()
  -> Pipeline._before_turn()
  -> Pipeline._agent_spec()
  -> AgentRunner.run_turn()
  -> ReasoningLoop.run()
```

`Pipeline` 继续负责：

- turn 前 reset。
- compact。
- 构造上下文。
- 判断 `model_purpose`。
- 触发 memory lifecycle。

`AgentRunner` 负责：

- 根据 `AgentSpec` 选择 provider/model。
- 创建并运行 `ReasoningLoop`。
- 统一 reset tool unlock 和 hook turn state。

### 第四阶段：Persistent Teammate 接入 AgentRunner

`TeammateManager` 上一轮已经接入 `ReasoningLoop`，但仍然自己拼 runner 参数。

本轮改成：

```text
TeammateManager._run_reasoning_cycle()
  -> AgentSpec(model_purpose="teammate")
  -> AgentRunner.run_turn()
  -> ReasoningLoop.run()
```

teammate 的生命周期仍然由 `TeammateManager` 管：

- 后台线程。
- idle 状态。
- inbox 轮询。
- 自动 claim task。
- shutdown 协议。

但单次模型-工具推理已经统一进入 `AgentRunner`。

### 第五阶段：ReflectionAgent 可选监督层

新增：

- `core/reflection.py`

Reflection agent 是 evaluator / supervisor，不是执行型 agent。

它只在工具执行后判断当前状态是否需要干预：

```text
ReasoningLoop
  -> tool calls
  -> ToolExecutor
  -> ToolExecutionSummary
  -> ReflectionAgent.should_reflect(...)
  -> ReflectionAgent.reflect(...)
  -> continue / revise / ask_user / stop
```

支持的 decision：

- `continue`：不干预。
- `revise`：追加一条 `<reflection-instruction>` 给主 agent 下一步参考。
- `ask_user`：停止本轮并把 message 返回给用户。
- `stop`：停止本轮并写入 loop guard assistant 消息。

Reflection 默认关闭，避免增加线上成本：

```env
REFLECTION_ENABLED=0
REFLECTION_MAX_TOKENS=500
REFLECTION_MIN_REASONING_STEPS=6
LLM_ROUTE_REFLECTION=
```

开启后，`core/bootstrap.py` 会创建 `ReflectionAgent`，并注入：

- 主 `Pipeline`
- 全局 persistent teammate `TEAM`

`reflection` 模型路由默认 alias 到 `summary`，也可以单独配置。

### 第六阶段：抽出 ModelTaskRunner

进一步检查后发现，`HistorySummarizer` 仍然直接调用 `provider.chat(...)`。

它和 `Pipeline` / teammate 不同，不需要工具循环：

```text
assistant_text
  -> 单次模型总结
  -> compact summary
```

因此本轮新增：

- `core/model_task_runner.py`

`ModelTaskRunner` 是 `AgentRunner` 的轻量兄弟：

| Runner | 用途 |
|---|---|
| `AgentRunner` | 工具循环型 agent：`model -> tool -> model` |
| `ModelTaskRunner` | 单次模型任务：`messages -> model -> text` |

现在 `HistorySummarizer` 可以通过：

```python
HistorySummarizer(
    runner=ModelTaskRunner(model_pool=MODEL_POOL),
    spec=AgentSpec(
        name="history_summarizer",
        profile=None,
        model_purpose="summary",
        max_tokens=220,
    ),
)
```

这样它不再自己关心 provider/model，只声明自己是 `summary` 类型的轻量 agent 任务。

为了兼容旧测试和旧调用方式，下面的构造仍然可用：

```python
HistorySummarizer(provider=provider, model="test-model")
```

内部会自动包装成 `ModelTaskRunner(provider=provider, model="test-model")`。

## 当前调用链对比

改造前：

```text
spawn_teammate
  -> TeammateManager._run_member
  -> client.chat.completions.create
  -> execute_tool_call
  -> make_teammate_handlers
```

改造后：

```text
spawn_teammate
  -> TeammateManager._run_member
  -> TeammateManager._run_reasoning_cycle
  -> AgentRunner.run_turn
  -> ReasoningLoop.run
  -> ModelPool / Provider
  -> ToolRegistry.schemas_for_turn
  -> ToolExecutor
  -> ToolRegistry.execute
  -> teammate handlers
```

## 现在各 agent 路径的统一程度

| 路径 | 是否复用 ReasoningLoop | 是否经过 ToolRegistry | 是否经过 ToolExecutor hooks | 是否走 ModelPool |
|---|---:|---:|---:|---:|
| Bot Pipeline | 是 | 是 | 是 | 是 |
| Coding TaskSession | 是 | 是 | 是 | 是 |
| Scheduled Agent | 是 | 是 | 是 | 是 |
| Persistent Teammate | 是 | 是 | 是 | 是 |

现在各路径是否复用 `AgentRunner`：

| 路径 | 是否复用 AgentRunner | 外层生命周期 |
|---|---:|---|
| Bot Pipeline | 是 | `AgentLoop` / `Pipeline` |
| Coding TaskSession | 是 | `TaskSessionRunner` / `Pipeline` |
| Scheduled Agent | 是 | `ScheduledAgentRunner` / `Pipeline` |
| Persistent Teammate | 是 | `TeammateManager` |

## 仍然保留的差异

Persistent teammate 仍然有自己的外层调度器，这是合理的。

因为它和普通 turn 不同：

- 它是后台线程。
- 它会进入 idle 状态。
- 它会定期扫描任务看板。
- 它会自动 claim 未认领任务。
- 它通过 team inbox 和 lead / teammate 通信。

因此本轮没有把 `TeammateManager` 强行塞进 `AgentLoop`。更合理的边界是：

- 外层生命周期：`TeammateManager`
- 中间执行适配：`AgentRunner`
- 内层推理执行：`ReasoningLoop`

单次模型任务的合理边界是：

- 任务身份：`AgentSpec`
- 单次执行：`ModelTaskRunner`
- 具体任务逻辑：`HistorySummarizer`

## 本轮涉及文件

- `core/reasoning_loop.py`
- `core/agent_spec.py`
- `core/agent_runner.py`
- `core/model_task_runner.py`
- `core/reflection.py`
- `core/pipeline.py`
- `coding_runtime/teammate.py`
- `tools/tool_registry.py`
- `core/bootstrap.py`
- `core/model_pool.py`
- `.env.example`
- `docs/runtime/MODEL_PROVIDER_POOL_ROUTING.md`
- `tests/test_agent_runner_reflection.py`
- `tests/test_model_task_runner.py`
- `tests/test_teammate_reasoning_loop.py`

## 测试

新增测试：

```bash
python -m unittest discover -s tests -p 'test_agent_runner_reflection.py' -v
python -m unittest discover -s tests -p 'test_model_task_runner.py' -v
python -m unittest discover -s tests -p 'test_teammate_reasoning_loop.py' -v
```

覆盖内容：

1. `AgentRunner` 会根据 `AgentSpec.model_purpose` 选择模型路由。
2. reflection 的 `revise` decision 会追加 `<reflection-instruction>`。
3. reflection 的 `stop` decision 会停止本轮并写入 guard 消息。
4. `ReflectionAgent` 可以解析 markdown fenced JSON。
5. `ModelTaskRunner` 会根据 `AgentSpec.model_purpose` 选择模型路由。
6. `HistorySummarizer` 可以通过 `ModelTaskRunner` 执行总结。
7. teammate 工具通过 `ToolRegistry` 控制可见性。
8. `bash` 默认不可见，需要 `tool_search(select:bash)` 解锁。
9. lead-only 的 `spawn_teammate` 不会暴露给 teammate。
10. teammate 调用 `idle` 后，`ReasoningLoop` 会停止当前 cycle，并把外层状态切到 idle。

回归测试：

```bash
python -m unittest discover -s tests -p 'test_pipeline_tool_loop_guard.py' -v
```

用于确认主 Pipeline 的循环保护行为没有被新增回调破坏。

## 后续建议

后面如果继续重构，可以考虑三件事：

1. 把 `TeammateManager` 的 idle 轮询和 task auto-claim 抽成 `TeammateScheduler`。
2. 把 `ReasoningLoop` 的事件回调整理成正式事件对象，例如 `on_model_response`、`on_tool_result`、`on_stop`。
3. 把 `Session` 的持久化策略扩展到 teammate，让 persistent teammate 重启后能恢复上下文，而不是只恢复成员状态。
