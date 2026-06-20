# Task Session Isolation

这次实现的是第一版 **TaskSession 隔离机制**。

目标是解决一个真实 agent runtime 里很重要的问题：

```text
一次 coding task 会产生大量临时上下文、工具输出、失败尝试和中间结论。
这些东西不应该全部污染主聊天 session 和全局长期记忆。
```

所以现在 coding task 会进入一个独立的任务会话。

---

## 一、核心流程

```text
用户在主会话提出 coding 请求
  -> ModeRouter 判断本轮是 coding profile
  -> AgentLoop 创建 TaskSession
  -> TaskSession 有独立 session id
  -> TaskSession 有独立 memory 目录
  -> coding pipeline 在 TaskSession 内运行
  -> background/team/task planning tools 只在 coding task runtime 中可用
  -> 任务结束后生成回复
  -> 从 TaskSession memory 中抽取候选项
  -> 提升到全局 PENDING.md
  -> 主会话只保存最终任务报告
```

主会话负责“对用户交代结果”。

任务会话负责“完成具体 coding 工作”。

---

## 二、新增文件

```text
tasksessions/
  __init__.py
  session.py
  runner.py
  promotion.py
```

### `tasksessions/session.py`

负责创建任务 session：

```text
session id: task:{task_id}
metadata.kind: task_session
metadata.parent_session_id: cli:local
metadata.task_type: coding
metadata.status: running/completed
```

任务 session 仍然复用现有 `SessionManager` 和 SQLite 存储。

这意味着它和普通 session 一样可以持久化、恢复、检查。

### `tasksessions/runner.py`

负责运行任务：

```text
TaskSessionRunner.run_coding_task(...)
```

它会：

```text
1. 创建 task session
2. 创建 task-local MemoryStore
3. 把全局 memory snapshot 注入 task 请求
4. 构造一个 task-local Pipeline
5. 运行 coding profile
6. 保存 task session
7. 调用 promoter 做记忆提升
8. 返回给主 session 一个任务报告
```

### `tasksessions/promotion.py`

负责把任务内有价值的记忆候选提升到全局 memory：

```text
task-local PENDING.md
task-local RECENT_CONTEXT.md
task summary
  -> global memory/PENDING.md
```

第一版先写到全局 `PENDING.md`，而不是直接写 `MEMORY.md`。

这是为了避免任务中的临时结论直接污染长期记忆。

---

## 三、TaskSession 的独立上下文

任务 session 有自己的：

```text
messages
metadata
memory root
MemoryLifecycle
ContextBuilder
```

路径大致是：

```text
.task_sessions/{task_id}/memory/
  MEMORY.md
  SELF.md
  NOW.md
  PENDING.md
  HISTORY.md
  RECENT_CONTEXT.md
```

它可以读取全局 memory snapshot，但写入时先进自己的 task-local memory。

这体现一个关键原则：

```text
TaskSession can read global memory, but cannot directly pollute global memory.
```

---

## 四、Coding Runtime 能力收敛

这次又把下面这些能力从普通 chat 中拿掉，归到 coding task runtime：

```text
background_run / check_background
task_create / task_update / task_list / task_get / claim_task
send_message / read_inbox / broadcast
spawn_teammate / list_teammates
shutdown_request / shutdown_status
plan_approval
```

代码位置是：

```text
coding_runtime/
  background_task.py
  protocols.py
  task.py
  teammate.py
```

普通 chat 模式现在不再默认看到这些工具。

coding TaskSession 中可以使用这些能力，因为它们服务的是具体工程任务：

```text
长命令后台执行
任务拆分和认领
队友通信和计划审批
读取 task session 的 team inbox
```

这样主聊天就不会被工程执行细节污染。

---

## 五、主会话里保存什么

主 session 不保存任务过程的全部工具轨迹。

它只保存：

```text
用户原始 coding 请求
最终 task report
```

这样主聊天上下文更干净。

如果用户以后问“刚才那个任务做了什么”，主 session 能回答。

如果需要看详细过程，可以通过 task session id 去查：

```text
task:{task_id}
```

---

## 六、为什么这对找实习有价值

这不是普通功能，而是 agent runtime 里的核心工程问题：

```text
context isolation
state management
memory pollution control
task artifact promotion
```

面试时可以这样讲：

> 我实现了 coding task 的上下文隔离机制：主聊天 session 只保留用户请求和最终任务报告，具体 coding 过程运行在独立 TaskSession 中。TaskSession 有自己的 message history 和 task-local memory，可以读取全局记忆快照，但不会直接污染全局长期记忆。任务结束后通过 promoter 将有价值的候选记忆提升到全局 PENDING.md，等待后续确认或整理。

这比“我能调用工具改代码”更能体现你理解 agent 应用的长期运行问题。

---

## 七、当前版本的限制

第一版还是 MVP：

```text
1. 所有 coding profile 请求都会走 TaskSession
2. promoter 还是规则式提升，不是 LLM 结构化判断
3. task-local tool traces 还没有 dashboard/inspector
4. 没有 /task list /task show 命令
```

后续可以继续做：

```text
/task list
/task show task_id
LLM memory promotion decider
task artifact summary
task session inspector
```
