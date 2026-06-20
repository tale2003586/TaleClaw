# TaskSession Memory Scope 修复记录

## 一、问题背景

项目已经为 coding task 提供 TaskSession 隔离机制：

```text
主聊天 session
  -> 创建 task:<task-id>
  -> 使用 .task_sessions/<task-id>/memory/
  -> 执行 coding pipeline
  -> 将候选记忆提升到全局 memory/PENDING.md
```

TaskSession 的 `ContextBuilder` 和 `MemoryLifecycle` 已经使用任务局部
`MemoryStore`。但是模型主动调用 `memorize` 或 `recall_memory` 工具时，
仍然会走 `tools/handlers.py` 中预先创建的全局 `MemoryStore` 单例。

修复前的实际路径：

```text
TaskSession 调用 memorize
  -> ToolRegistry.execute(...)
  -> MEMORY_HANDLERS["memorize"]
  -> 全局 MEMORY = MemoryStore()
  -> memory/MEMORY.md
```

这会绕过任务隔离。任务执行过程中的临时结论、误判或局部约定可能直接进入
全局长期记忆。

## 二、修复目标

记忆工具需要根据当前 session 自动选择 scope：

```text
普通聊天 session
  -> memory/

task:* 内部任务 session
  -> .task_sessions/<task-id>/memory/
```

TaskSession 结束后，仍然由 `TaskMemoryPromoter` 将值得保留的候选项提升到：

```text
memory/PENDING.md
```

任务内记忆不会直接写入全局 `MEMORY.md`。

## 三、实现方案

### 1. ToolRegistry 注入内部 session 上下文

修改文件：

```text
tools/tool_registry.py
```

新增：

```python
SESSION_SCOPED_TOOLS = {
    "memorize",
    "recall_memory",
}
```

执行这两个工具时，`ToolRegistry` 会将当前 session 作为内部参数
`_session` 传给 handler：

```python
handler_args = dict(args)
if name in SESSION_SCOPED_TOOLS:
    handler_args["_session"] = session
return tool.handler(**handler_args)
```

`_session` 不属于工具 schema，因此不会暴露给模型，也不需要模型主动传递。

### 2. 根据 session 解析 MemoryStore

修改文件：

```text
tools/handlers.py
```

新增：

```python
memory_store_for_session(session=None)
run_memorize(...)
run_recall_memory(...)
```

处理规则：

```text
metadata.kind != "task_session"
  -> 返回全局 MEMORY

metadata.kind == "task_session"
  -> 从 metadata.memory_root 读取任务局部目录
  -> 缺少 memory_root 时，根据 task_id 回退推导目录
  -> 校验目录必须位于 .task_sessions/ 内
  -> 返回任务局部 MemoryStore
```

目录校验用于阻止错误或被污染的 session metadata 将记忆写到
`.task_sessions/` 之外。

### 3. TaskSession 显式保存 memory_root

修改文件：

```text
tasksessions/session.py
```

创建 TaskSession 时，将局部记忆目录写入 metadata：

```python
session.metadata["memory_root"] = ".task_sessions/<task-id>/memory"
```

工作区内的目录优先保存为相对路径，避免 Docker `/app` 和本机工作区路径不同
导致 metadata 失效。这样工具处理器也不需要依赖隐式约定来猜测目录。对于修复前
创建的旧 TaskSession，处理器仍然会根据 `task_id` 回退推导目录，保留兼容性。

## 四、修复后的运行路径

### 普通聊天调用 memorize

```text
web:default
  -> ToolRegistry.execute("memorize", ..., session=web:default)
  -> run_memorize(..., _session=web:default)
  -> memory_store_for_session(web:default)
  -> 全局 memory/MEMORY.md
```

### Coding TaskSession 调用 memorize

```text
task:coding-12345678
  -> ToolRegistry.execute("memorize", ..., session=task:coding-12345678)
  -> run_memorize(..., _session=task:coding-12345678)
  -> memory_store_for_session(task:coding-12345678)
  -> .task_sessions/coding-12345678/memory/MEMORY.md
```

### Coding TaskSession 调用 recall_memory

```text
task:coding-12345678
  -> 局部 .task_sessions/coding-12345678/memory/
```

`recall_memory` 与 `memorize` 使用同一 scope。任务启动时，全局记忆快照仍然会由
`TaskSessionRunner` 注入任务请求，因此 coding task 可以读取初始全局背景，同时
主动追加的任务记忆保持局部隔离。

## 五、新增测试

新增文件：

```text
tests/test_memory_scope.py
```

覆盖场景：

1. 普通 session 调用 `memorize`，内容写入全局 `MEMORY.md`。
2. TaskSession 调用 `memorize`，内容只写入任务局部 `MEMORY.md`。
3. TaskSession 调用 `recall_memory`，读取任务局部记忆，不混入全局记忆。
4. TaskSession metadata 指向 `.task_sessions/` 外部时，拒绝写入。

验证命令：

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile tools/handlers.py tools/tool_registry.py tasksessions/session.py
git diff --check
```

## 六、仍未处理的问题

本次只修复 scope，不改变现有记忆策略。以下问题仍然存在：

1. `recall_memory(query)` 暂时忽略 query，仍然返回当前 scope 的全部可注入记忆。
2. `PENDING.md` 的任务摘要和 recent context 仍然可能偏长。
3. 去重仍然是规范化后的精确文本匹配，不是语义去重。
4. TaskSession 提升到全局 `PENDING.md` 后，还缺少 approve / reject 审核入口。
