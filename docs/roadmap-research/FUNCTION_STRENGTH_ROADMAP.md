# Function Strength Roadmap

你现在这个项目的架构已经比普通聊天机器人强很多，但“功能强度”还没完全起来。

问题不在于缺一个炫酷功能，而在于还缺几类能让它看起来像真实产品/真实 agent runtime 的能力：

```text
可验证
可观察
可恢复
可控
可展示
```

所以后续提升不要只说“再加功能”，而要分三层：

```text
优化：让现有功能更稳、更干净
改进：补齐工程闭环
增强：加入更像真实 agent 产品的高级能力
```

---

## 一、优化：把现有系统打磨稳

这些不一定显得酷，但对找实习很重要。

### 1. 测试

优先级最高。

建议先补：

```text
tests/test_tool_visibility.py
tests/test_task_session.py
tests/test_memory_dedup.py
tests/test_plugin_manager.py
```

要验证：

```text
chat mode 不能解锁 bash/task/team 工具
coding mode 可以解锁 background_run/write_file
TaskSession 会创建 task:* session
TaskSession memory 会 promote 到 global PENDING.md
重复 memory 不会写入
/status 不进入 LLM
```

面试价值：

> 我不仅实现了 agent runtime，还给工具权限、任务隔离和记忆生命周期写了关键测试。

### 2. README + 架构图

补一个 `README.md`，放：

```text
项目简介
架构图
核心能力
运行方式
测试方式
目录结构
设计亮点
未来计划
```

Mermaid 图建议：

```text
CLI
  -> MessageBus
  -> AgentLoop
  -> ModeRouter
  -> TaskSessionRunner
  -> Pipeline
  -> ToolRegistry / ToolExecutor
  -> MemoryLifecycle / Promotion
```

### 3. CLI inspector

先不用做 Web dashboard。

做几个命令就很有用：

```text
/session list
/task list
/task show <task_id>
/memory pending
/tools visible
```

这些命令可以作为 `status_commands` 插件继续扩展。

---

## 二、改进：补齐 agent runtime 闭环

这层是让系统从“能跑”变成“像真实 runtime”。

### 1. TaskSession Inspector

现在 TaskSession 已经有了，但还缺查看入口。

建议做：

```text
/task list
  展示 task session id、parent、status、updated_at

/task show task:coding-xxxx
  展示用户请求、最终回复、promoted memory、最近工具调用
```

面试价值：

> 我把 coding task 和 chat session 隔离，并提供 task inspector 查看任务执行记录。

### 2. Memory Promotion Decider

现在 promotion 是规则式：

```text
task pending + recent context + summary
  -> global PENDING.md
```

下一步可以加一个 LLM decider：

```json
{
  "action": "add|duplicate|supersede|ignore",
  "target": "memory|now|pending",
  "content": "...",
  "reason": "..."
}
```

关键原则：

```text
LLM 只做判断
程序负责落库
```

### 3. Tool Trace 持久化

现在 `ToolTraceHook` 只在内存里记录。

可以改成写：

```text
.task_sessions/{task_id}/tool_trace.jsonl
```

这样 task show 可以展示：

```text
调用了哪些工具
成功/失败
参数是什么
结果摘要是什么
有没有被 hook deny
```

这会显著增强“工程可信度”。

### 4. Error Recovery

现在 provider/tool 出错时只是返回字符串。

可以增加：

```text
ProviderError
ToolExecutionError
SessionRecovery
retry policy
```

第一版不用复杂，至少区分：

```text
模型 API 错误
工具执行错误
权限拒绝
上下文格式错误
```

---

## 三、增强：让项目更像产品

这些是锦上添花，但很容易在面试里形成记忆点。

### 1. Web 或 TUI Inspector

不需要大而全。

第一版页面展示：

```text
sessions
task sessions
memory pending
tool traces
plugins
```

技术可以很简单：

```text
FastAPI + HTML
或 textual TUI
或纯 CLI 命令
```

如果时间有限，CLI inspector 性价比更高。

### 2. Skill Marketplace

你已经有 `skill_runtime`。

可以增强为：

```text
/skills list
/skills show code-review
/skills load code-review
```

然后让 coding TaskSession 根据任务自动建议 skill。

### 3. Proactive MVP

不是闲聊主动提醒，而是任务型 proactive：

```text
background task 完成通知
task session 卡住提醒
pending memory 待确认提醒
每日项目总结
```

必须有 gate：

```text
用户开启
cooldown
coding mode 不闲聊
session busy 不打断
```

### 4. Evaluation Harness

这个对实习很加分。

做一个简单评测：

```text
eval_cases/
  tool_visibility.json
  memory_promotion.json
  task_session.json
```

跑：

```text
python -m evals.run
```

输出：

```text
pass/fail
失败原因
模型回复
工具调用轨迹
```

面试价值：

> 我不只写 agent，还考虑了 agent 行为如何评测。

---

## 四、最推荐的下一步

如果你想最快提升实习竞争力，顺序是：

```text
1. tests/test_tool_visibility.py
2. tests/test_task_session.py
3. /task list 和 /task show
4. ToolTraceHook 写入 task-local jsonl
5. README.md + 架构图
```

这五个做完，项目的功能强度会明显上一个台阶。

---

## 五、项目亮点应该怎么包装

不要说：

> 我做了一个聊天机器人。

要说：

> 我实现了一个轻量 LLM Agent Runtime。它支持异步 MessageBus、多模式路由、SQLite 会话持久化、TaskSession 上下文隔离、Markdown 长期记忆生命周期、deferred tools 权限控制、ToolExecutor 安全 hook 和 Plugin 扩展点。为了避免 coding 任务污染主聊天上下文，我把后台任务、team 协作和任务认领收敛到 coding runtime，并通过 memory promotion 将任务内有价值的信息提升到全局长期记忆候选。

这听起来就完全不是一个普通 demo 了。
