# Agent Loop 不可见工具循环保护修复记录

## 一、问题

当模型请求一个当前轮不可见的工具时，`ToolRegistry` 会返回提示：

```text
Tool '<name>' is not visible in this turn.
Call tool_search with query='select:<name>' first.
```

但普通 Bot 会话和 Coding TaskSession 的 `Pipeline` 此前没有推理步数上限，也不会因为
重复请求不可用工具而结束本轮。如果模型没有改为调用 `tool_search`，而是继续请求原工具，
就会持续进入下一次 reasoning step。

原有 `ToolLoopGuardHook` 会在相同工具参数重复三次时拒绝调用：

```text
Error: Repeated tool call blocked by tool_loop_guard.
```

但拒绝结果仍然只是作为工具消息返回给模型，`Pipeline` 不会停止，因此它并不是完整的
循环终止机制。

## 二、修复策略

本次加入三层保护。

### 1. 工具可用性检查

`ToolRegistry.execution_error_for_turn()` 统一区分：

```text
Unknown tool
Tool is not allowed in this mode
Tool is not visible in this turn
```

`ToolRegistry.execute()` 和 `Pipeline` 使用同一检查方法，避免错误判断和用户实际看到的
工具结果不一致。

### 2. 重复请求不可用工具时停止

一次直接请求未解锁工具仍然允许继续，模型可以在下一步纠正为：

```text
tool_search(query="select:bash")
```

如果同一个不可用工具在本轮中连续累计请求两次，`Pipeline` 会停止本轮并写入一条
`agent_loop_guard` 助手消息。

### 3. 普通会话推理步数上限

普通 Bot 会话和 Coding TaskSession 默认最多允许：

```text
24
```

个 reasoning step。即使模型不断更换参数来避开重复调用判断，也不会无限循环。

Scheduled Agent 已经拥有独立的 `automation_limits.max_reasoning_steps`，继续沿用原有审查
和报错流程。

### 4. 重复调用历史按轮清空

`ToolLoopGuardHook` 的重复调用历史此前会跨用户消息保留。现在 `Pipeline` 每次开始处理新
消息时都会调用 `ToolExecutor.reset_turn()`，清理当前 Session 的 hook 计数。用户在不同
消息中重复执行同一个正常操作不会被误判为循环。

## 三、保留的正常流程

延迟工具的正常使用方式保持不变：

```text
1. tool_search(query="select:bash")
2. bash(command="pwd")
3. 返回最终回复
```

工具一旦通过 `tool_search` 解锁，就不会被识别为不可用工具。

## 四、用户可见提示

重复请求不可用工具：

```text
本轮已停止：模型重复请求当前不可用的工具 `<tool>`。请切换到允许该工具的模式，
或让助手使用 `tool_search` 选择当前模式可用的工具。
```

重复调用同一个工具：

```text
本轮已停止：模型重复调用同一工具，已触发循环保护。请调整请求后重试。
```

超过总步数：

```text
本轮已停止：工具推理步骤超过上限 (24)，已触发循环保护。
```

## 五、文件改动

新增：

```text
tests/test_pipeline_tool_loop_guard.py
docs/AGENT_LOOP_UNAVAILABLE_TOOL_GUARD.md
```

修改：

```text
core/pipeline.py
tools/tool_registry.py
tasksessions/runner.py
plugins/scheduler/agent_runner.py
```

## 六、测试覆盖

新增测试覆盖：

- 连续两次请求当前轮不可见工具后停止
- `tool_search` 解锁后可以正常调用延迟工具并完成任务
- `ToolLoopGuardHook` 拒绝重复调用后停止本轮
- 参数持续变化时由总 reasoning step 上限停止
- 不同用户消息之间不会共享重复调用计数
- 注册表能够区分不可见、不允许和未知工具

运行完整测试：

```bash
python -m unittest discover -s tests -v
```
