# Runtime 平台化收束改进计划

日期：2026-06-09

## 目标

本轮改进不继续堆新功能，而是把当前项目收束成清晰的两层结构：

```text
taleclaw Agent Runtime Platform
  -> 多入口、多用户、模型路由、工具治理、记忆、插件、调度

taleclaw Coding Agent
  -> 基于平台能力构建的面向仓库代码任务的垂直 Agent
```

面试叙事目标：

> taleclaw 是一个 Agent Runtime 平台。它统一承载 Web、CLI、飞书、Telegram 和 scheduler。Coding Agent 是平台上的一个垂直应用，复用模型路由、工具治理、TaskSession、记忆和运行 trace。每次 Agent 执行都有 run_state、trace 和 report，可以审计、复盘和评估。

## 当前判断

当前 runtime 已经超过普通 demo agent loop，具备平台雏形：

- `AppRuntime`、`MessageBus`、`AgentLoop` 已经形成统一入口链路。
- `Pipeline`、`AgentRunner`、`ReasoningLoop` 已经承载模型/工具循环。
- `ModelPool` 已经支持多 provider、用途路由、fallback 和不同 wire API。
- `ToolRegistry`、`ToolExecutor`、hooks 已经有工具治理基础。
- `TaskSessionRunner` 已经能把 Coding 任务从主会话隔离出来。
- Web、CLI、Telegram、飞书、scheduler 都复用同一套 runtime。

主要不足：

- 缺少统一的 run/turn 抽象。
- 普通聊天、Coding Task、Scheduler Agent 的运行证据链不统一。
- `bootstrap.py` 承担过多装配细节，缺少 Runtime Kernel 抽象。
- 工具权限、模式权限、scheduler capability approval 分散在多个模块。
- `ModeRouter` 混合了承载命令、意图判断、权限和执行路径规划。
- Coding Agent 还没有作为平台上的垂直应用被单独文档化和评估。

## 第一阶段：RunState + TraceStore

优先级最高。

状态：已完成。完成记录见 [Runtime 第一阶段完成记录：RunState + TraceStore](2026-06-09_RUNTIME_PHASE1_RUNSTATE_TRACESTORE_COMPLETION.md)。

新增：

```text
core/run_state.py
core/trace_store.py
.runs/<run_id>/
  run_state.json
  trace.jsonl
  report.json
```

`RunState` 建议字段：

```text
run_id
session_id
channel
chat_id
user_id
user_role
mode
execution_path
status
reasoning_steps
tool_calls
last_tool
stop_reason
started_at
finished_at
final_answer
error
```

`TraceStore` 建议接口：

```python
start_run(run_state)
append_event(run_state, event_name, payload)
write_run_state(run_state)
write_report(run_state, report)
```

接入点：

- `core/agent_loop.py`：收到 inbound message 时创建 `run_id`。
- `core/reasoning_loop.py`：记录 `model_requested`、`model_returned`、`tool_executed`、`run_finished`。
- `tools/executor.py`：把工具执行结果和 hook trace 写入 trace。
- `tasksessions/runner.py`：Coding task 绑定 parent run，并写 task report。
- `plugins/scheduler/agent_runner.py`：Scheduled agent run 也写统一 trace/report。

收益：

- 每轮 Agent 执行可审计。
- Debug 不再只依赖 session messages。
- 面试时能展示 `.runs/<run_id>/trace.jsonl` 和 `report.json`。

## 第二阶段：RuntimeKernel

状态：已完成。完成记录见 [Runtime 第二至第五阶段完成记录](2026-06-09_RUNTIME_PHASE2_TO_5_COMPLETION.md)。

新增：

```text
core/kernel.py
```

建议结构：

```python
from dataclasses import dataclass

@dataclass
class RuntimeKernel:
    bus: MessageBus
    sessions: SessionManager
    model_pool: ModelPool
    tools: ToolRegistry
    tool_executor: ToolExecutor
    memory_store: ScopedMemoryStore
    plugin_manager: PluginManager
    router: ModeRouter
    pipeline: Pipeline
    task_session_runner: TaskSessionRunner
```

改造目标：

- `core/bootstrap.py` 成为 composition root，只负责装配依赖。
- `AppRuntime` 持有或引用 `RuntimeKernel`。
- gateway、web、scheduler 不再需要理解 runtime 内部组成。

面试表达：

> bootstrap 是 composition root，RuntimeKernel 是平台核心对象。

## 第三阶段：ToolPolicy

状态：已完成。完成记录见 [Runtime 第二至第五阶段完成记录](2026-06-09_RUNTIME_PHASE2_TO_5_COMPLETION.md)。

新增：

```text
tools/policy.py
```

目标是把“工具定义”和“工具权限”分开：

```text
ToolRegistry：有哪些工具，工具 schema 是什么，handler 是什么。
ToolPolicy：当前用户/模式/run 能看到什么工具，能不能执行，是否需要审批。
```

建议接口：

```python
visible_tools(session, mode, run_context)
can_execute(tool_name, args, session, mode, run_context)
requires_approval(tool_name, args, session, mode, run_context)
```

迁移来源：

- `ToolSpec.enabled_for`
- `ToolRegistry.visible_names_for_turn`
- `ModeRouter._coding_allowed`
- scheduler capability approval 的部分判断
- high-risk tools 的 deferred unlock 逻辑

收益：

- 安全策略集中。
- 工具系统更平台化。
- 更容易解释普通用户、admin、scheduler agent 的能力差异。

## 第四阶段：拆分 ModeRouter

状态：已完成。完成记录见 [Runtime 第二至第五阶段完成记录](2026-06-09_RUNTIME_PHASE2_TO_5_COMPLETION.md)。

当前 `ModeRouter` 同时负责：

- 显式模式命令。
- hybrid 关键词预筛。
- LLM classifier 判断。
- coding 权限检查。
- scheduler/storage/memory/coding intent 判断。
- execution path 选择。

建议拆成：

```text
modes/intent.py
  IntentClassifier

modes/execution_plan.py
  ExecutionPlanner
  ExecutionPlan

modes/router.py
  只做协调和兼容现有调用
```

目标流程：

```python
intent = classifier.classify(user_text, session)
plan = planner.plan(intent, session)
policy.check(plan, session)
```

`ExecutionPlan` 建议字段：

```text
intent: chat / coding / scheduler / storage / memory / mode_switch
execution: direct_reply / pipeline_bot / task_session / scheduled_agent
profile: bot / coding
confidence
reason
switch_message
```

收益：

- Bot、Coding Agent、Scheduled Agent 可以被解释成平台上的应用。
- Router 不再是大型 if-else。
- 后续增加新应用时只需新增 classifier/planner 规则。

## 第五阶段：Coding Agent 产品化

状态：已完成。完成记录见 [Runtime 第二至第五阶段完成记录](2026-06-09_RUNTIME_PHASE2_TO_5_COMPLETION.md)。

新增文档：

```text
docs/overview/PLATFORM_AND_CODING_AGENT.md
docs/overview/CODING_AGENT.md
```

Coding Agent 定位：

> 基于 taleclaw Runtime 的仓库级代码助手。

它复用平台能力：

```text
ModelPool
ToolRegistry
ToolPolicy
TaskSession
MemoryLifecycle
TraceStore
SessionStore
ScopedMemoryStore
```

它自己的能力：

```text
读仓库结构
搜索代码
读取文件
运行测试
编辑文件
任务隔离
任务日志
结论提取
记忆提升
```

建议 demo：

1. Web 登录 admin。
2. 进入 `/hybrid`。
3. 提出一个代码分析或小修复任务。
4. Router 进入 Coding Agent。
5. TaskSession 创建独立上下文。
6. Coding Agent 调工具读文件/运行测试。
7. 生成 `.runs/<run_id>/report.json`。
8. 任务结论进入 `memory/PENDING.md`。

## 第六阶段：Evaluation

新增：

```text
benchmarks/
  hybrid_routing_cases.json
  tool_calling_cases.json
  memory_cases.json
  coding_agent_cases.json

scripts/run_evaluation.py
docs/overview/EVALUATION.md
```

先做小规模，不追求大而全：

- 10 条 hybrid routing case。
- 10 条 tool calling case。
- 5 条 memory recall case。
- 5 条 coding task case。

建议指标：

```text
routing_accuracy
tool_call_success_rate
memory_recall_success_rate
coding_task_pass_rate
avg_reasoning_steps
avg_tool_calls
failure_categories
security_rejection_count
```

收益：

- 证明项目不是只会接 API。
- 面试时能讲“我如何评估大模型应用效果”。
- 后续修改 runtime 时有回归依据。

## 一周执行建议

### Day 1

- 新增 `RunState`。
- 新增 `TraceStore`。
- 普通 Web/CLI turn 能生成 `.runs/<run_id>/trace.jsonl`。

### Day 2

- `ReasoningLoop` 写 model/tool/run events。
- `ToolExecutor` 输出标准 tool metadata。

### Day 3

- `TaskSessionRunner` 接入 run_id。
- Coding task 生成统一 report。

### Day 4

- 抽 `RuntimeKernel`。
- 简化 `core/bootstrap.py`。

### Day 5

- 写 `PLATFORM_AND_CODING_AGENT.md`。
- 写 `CODING_AGENT.md`。
- 准备 5 分钟 demo 脚本。

### Day 6-7

- 补 20 条左右 evaluation case。
- 生成 `docs/overview/EVALUATION.md`。

## 暂时不做

这些工作有价值，但不是本轮优先级：

- 不继续新增网关。
- 不继续新增插件。
- 不马上把 Web server 改成 FastAPI。
- 不重写 memory 系统。
- 不大规模重构所有目录。

本轮重点是：

```text
命名核心抽象
统一运行证据链
集中安全策略
把 Coding Agent 作为平台应用讲清楚
补最小评估闭环
```

## 最终验收标准

完成后，项目应该可以清楚回答：

1. taleclaw 平台核心 Runtime 是什么？
2. 一条用户消息从 Web/飞书/Telegram 进入后如何流转？
3. 模型如何按用途路由？
4. 工具为什么安全？
5. Coding Agent 如何依托平台运行？
6. 每次 Agent 执行在哪里审计？
7. 如何评估 routing、tool calling、memory 和 coding task 的效果？

如果这 7 个问题能被 README、架构图、trace/report 和 evaluation 一起回答，这个项目就会更像一个成熟的大模型应用开发实习项目。
