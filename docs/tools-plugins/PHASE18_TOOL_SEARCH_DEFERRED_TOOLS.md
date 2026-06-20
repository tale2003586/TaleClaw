# Phase 18：Tool Search / Deferred Tools 说明

Phase 18 做的是工具可见性治理。

之前的设计是：

```text
某个 mode 能用哪些工具
  -> 这一轮就把这些工具 schema 全部发给模型
```

这在工具少的时候没问题。但工具一多，会有几个问题：

```text
1. prompt/schema 变长，浪费上下文
2. 模型更容易误调用高风险工具
3. bash/write/edit 这类工具默认暴露，行为太激进
4. 后续加插件后，工具列表会越来越不可控
```

所以 Phase 18 引入：

```text
always_on tools
preloaded tools
deferred tools
tool_search 解锁
```

---

## 一、本次改了什么

### 1. 新增 `tool_search`

在 `tools/schema.py` 里新增了工具：

```text
tool_search(query)
```

它有两种用法：

```text
tool_search("shell")
  -> 搜索当前 mode 允许、但本轮还没有暴露的工具

tool_search("select:bash")
  -> 解锁 bash，让下一次 reasoning step 可以看到 bash schema
```

注意：`tool_search` 本身不在 `tools/handlers.py` 里实现，而是在 `ToolRegistry` 里特殊处理。

原因是它需要访问：

```text
当前有哪些工具
当前 mode 是什么
当前 session 解锁了哪些工具
```

这些信息都属于 registry/runtime 层，而不是普通业务工具层。

### 2. System prompt 增加工具搜索提示

`ContextBuilder` 现在会追加一条通用提示：

```text
Some tools are deferred. Use tool_search to find or unlock tools that are not currently visible.
```

这句话的作用是告诉模型：

```text
如果想用某个工具但当前看不到，不要硬猜函数名，先调用 tool_search。
```

---

## 二、工具可见性现在怎么计算

核心代码在 `tools/tool_registry.py`。

现在一轮里真正发给模型的工具是：

```text
always_on
  + 当前 mode 的 preloaded
  + 本轮 tool_search 解锁的工具
```

但最后还会经过一层硬过滤：

```text
必须是当前 mode 允许的工具
```

这就保证了：

```text
chat/bot mode 不能通过 tool_search 解锁 bash/write_file/edit_file
```

也就是 Phase 7 里的 mode 硬边界仍然存在。

---

## 三、当前策略

### always_on

```text
recall_memory
memorize
tool_search
task_list
```

这些是低风险、常用、对上下文维护有帮助的工具。

### coding preloaded

```text
read_file
load_skill
task_get
check_background
compact
```

编程模式下默认可以读文件、写文件、编辑文件、运行 shell、查任务、查后台任务、压缩上下文。

当前 coding 模式下这些核心工作工具会默认暴露：

```text
bash
write_file
edit_file
list_files
read_file
git_status
git_diff
git_log
git_branch
```

但下面这些更有副作用或更偏协作编排的工具不会默认暴露：

```text
background_run
git_add
git_commit
spawn_teammate
```

模型需要先调用：

```text
tool_search("select:git_add")
```

然后下一次 reasoning step 才能调用：

```text
git_add(...)
```

### bot preloaded

```text
load_skill
task_create
task_update
task_get
check_background
send_message
read_inbox
```

聊天模式保留协作、任务、记忆类能力，但不会开放代码执行和文件写入。

---

## 四、本轮解锁是怎么保存的

解锁状态存在：

```text
session.metadata["unlocked_tools"]
```

每一轮开始时：

```python
ToolRegistry.reset_turn_unlocks(session)
```

会清空本轮解锁。

这意味着：

```text
用户发来一条消息
  -> 开始新 turn
  -> unlocked_tools 清空
  -> 模型只能看到 always_on + preloaded
  -> 模型调用 tool_search("select:bash")
  -> registry 把 bash 加进 session.metadata["unlocked_tools"]
  -> 下一次 provider.chat 可以看到 bash
  -> 本轮结束
  -> 下一轮重新清空
```

这个设计比“永久开启 bash”更稳。

---

## 五、为什么不是直接在 prompt 里说“少用工具”

因为 prompt 约束是软约束。

工具 schema 可见性是硬约束。

如果模型根本看不到 `write_file` 的 schema，它就不能正常调用 `write_file`。

所以大模型应用工程里，安全性不要只靠提示词，要靠：

```text
工具可见性
权限边界
hook 检查
执行器拦截
审计 trace
```

Phase 18 做的是第一层：工具可见性。

Phase 14 做的是执行器和 hook。

这两层是配合关系。

---

## 六、这次还顺手修了一个工具循环问题

`compact` 是一个工具调用。

OpenAI Chat Completions 要求：

```text
assistant message 里有 tool_calls
后面必须跟对应 tool_call_id 的 tool message
```

所以现在 `compact` 被调用后，也会向 `session.messages` 追加一个 tool result。

否则下一次请求可能出现：

```text
Messages with role 'tool' must be a response to a preceding message with 'tool_calls'
```

或者相反的 tool_call 没有 response 的问题。

---

## 七、你需要掌握什么

面向大模型应用开发实习，Phase 18 的重点不是 `tool_search` 这个名字，而是：

```text
Tool visibility management
```

你要能讲清楚：

```text
1. 不是所有工具都应该每轮暴露给模型
2. 工具可见性可以按 mode、risk、turn state 动态计算
3. 高风险工具应该 deferred，需要显式解锁
4. prompt 是软约束，schema visibility 是硬约束
5. tool_call 和 tool result 必须严格配对
```

可以这样介绍：

> 我实现了一个 deferred tools 机制：runtime 每轮只暴露 always-on 和 mode preloaded 工具；高风险工具通过 tool_search 在当前 turn 内显式解锁；同时保留 mode hard boundary，聊天模式无法解锁代码执行工具。这降低了工具误调用风险，也减少了每轮传给模型的 schema 体积。
