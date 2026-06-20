# taleclaw Coding Agent

日期：2026-06-09

## 定位

taleclaw Coding Agent 是基于 taleclaw Runtime Platform 构建的仓库级代码助手。

它不是独立脚本，而是平台上的一个垂直 Agent 应用：

- 入口复用 Web、CLI、飞书、Telegram。
- 模型复用 ModelPool 的 coding 路由。
- 工具复用 ToolRegistry、ToolPolicy、ToolExecutor 和 hooks。
- 任务隔离复用 TaskSession。
- 记忆复用 task-local memory 与全局 memory promotion。
- 运行审计复用 RunState 和 TraceStore。

## 核心流程

```text
用户提出代码任务
  -> ModeRouter 判断为 coding intent
  -> ExecutionPlanner 选择 task_session execution
  -> TaskSessionRunner 创建 task session
  -> 注入仓库任务提示和全局记忆快照
  -> Pipeline / ReasoningLoop 执行模型工具循环
  -> 工具读取、搜索、编辑、测试
  -> TaskArtifactWriter 写 TASK_LOG.md 和 CONCLUSIONS.json
  -> TaskConclusionExtractor 提取可复用结论
  -> TaskMemoryPromoter 将高价值结论推入全局 pending memory
  -> 父会话收到任务完成摘要
  -> TraceStore 写入父 run 的 trace/report
```

## 已具备能力

- 仓库级任务隔离：每个 coding task 有独立 task session 和 task-local memory。
- 工具治理：高风险工具需要通过延迟解锁或策略检查。
- 代码操作：可读文件、运行命令、编辑文件、后台任务等。
- 测试闭环：任务中可运行测试，并把输出写入 task transcript。
- 结论沉淀：任务完成后抽取可复用项目结论，进入全局 pending memory。
- 审计回放：父 run 记录 task session id、模型事件、工具事件、artifact 路径。

## 与普通聊天的区别

普通聊天直接走 `pipeline_bot`：

```text
AgentLoop -> Pipeline -> ReasoningLoop
```

Coding Agent 走隔离任务：

```text
AgentLoop -> TaskSessionRunner -> task-local Pipeline -> ReasoningLoop
```

这个差异很重要：代码任务可能包含多轮工具调用、测试输出和中间记忆，如果直接污染主会话，会让长期上下文越来越乱。TaskSession 把“任务过程”和“主会话结果”分开，只把高价值结论提升到全局记忆。

## 面试展示建议

展示时可以按三层讲：

- 平台能力：ModelPool、ToolPolicy、TraceStore、MemoryLifecycle。
- Coding Agent 能力：TaskSession、工具调用、测试运行、结论提取。
- 工程证据：`.runs/<run_id>/trace.jsonl`、`.task_sessions/<task_id>/TASK_LOG.md`、`CONCLUSIONS.json`。

一句话总结：

> Coding Agent 是 taleclaw Runtime 上的一个垂直应用。它把仓库代码任务封装成可隔离、可审计、可沉淀结论的 agent run，而不是把所有代码操作直接塞进普通聊天上下文。
