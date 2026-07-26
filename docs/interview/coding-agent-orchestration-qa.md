# 多层编排 Coding Agent 面试追问

这份回答基于当前仓库代码整理，重点对应这些实现：

- `agents/coding/runner.py`：coding task session 主流程。
- `agents/coding/session.py`：task session 创建和元数据。
- `runtime/workspace.py`、`runtime/trace/workspace.py`：workspace 解析、快照和 diff。
- `runtime/reasoning_loop.py`、`runtime/working_memory.py`：推理循环、checkpoint、工作记忆。
- `tools/policy.py`、`tools/handlers.py`、`tools/hooks.py`、`tools/schema.py`：工具可见性、工具实现、安全 hook。
- `agents/subagent/`：短生命周期 subagent。
- `coding_runtime/teammate.py`、`bus/protocol.py`、`bus/reliable.py`：持久 teammate 和消息协议。
- `evaluation/harness.py`、`evaluation/verifiers.py`、`evaluation/swebench_adapter.py`：benchmark、verifier、SWE-bench adapter。

## 1. Coding Agent 的完整执行流程是什么？

从用户输入到最终报告，大体是：

1. 主会话收到 coding 请求，进入 `task_session` 执行路径。
2. `TaskSessionRunner.run_coding_task()` 用 `WorkspaceResolver` 解析 workspace，并把 workspace 信息绑定到 parent session。
3. 创建独立 task session，session id 形如 `task:<slug>-<uuid>`，并写入 `kind=task_session`、`task_id`、`parent_session_id`、`memory_root`、`user_request` 等元数据。
4. 执行前捕获 workspace snapshot，写 trace 事件 `workspace.resolved`、`workspace.snapshot.captured`。
5. 为 task session 构造 task-local memory，并把用户需求、workspace 说明、handoff、执行约束、全局记忆摘要放进 task session 的 user message。
6. fork 一条 task pipeline：使用 task-local `ContextBuilder`、`TaskMemoryLifecycle`，并根据任务复杂度调整 reasoning step budget。
7. 进入 `runtime/reasoning_loop.py`：循环执行 `context -> model -> tool calls -> tool results -> context -> model`。
8. 工具执行经过 `ToolExecutor` 和多个 hook，例如 shell 安全、workspace 范围、写文件范围、重复工具调用 guard、trace hook。
9. 推理过程中会通过 checkpoint callback 保存 session，并把 step checkpoint 写入 working memory。
10. 模型完成后读取最后 assistant 回复，标记 task session 状态。
11. 抽取 task conclusions，做 task memory promotion，把可靠结论晋升回父级记忆。
12. 写 task artifacts：`TASK_LOG.md` 和 `CONCLUSIONS.json`。
13. 执行后再次捕获 workspace snapshot，和 before snapshot 做 diff，写 `workspace_snapshot_before.json`、`workspace_snapshot_after.json`、`workspace_diff.json`。
14. 在 run_state 里写 `task_session` report，追加 `task_session_completed` trace 事件。
15. 返回给 parent session 一段面向用户的总结，包括 task id、状态、回复、记忆晋升数量和 artifact 路径。

所以它不是“直接让模型改代码”，而是一次带 workspace 边界、task session、trace、checkpoint、artifact 和 verifier 接口的闭环执行。

## 2. “读仓库 -> 改实现 -> 跑验证 -> 输出报告”最难的是哪一步？

最难的是“读仓库和定位”，不是写 patch 本身。

原因是 coding agent 的失败多数不是语法不会改，而是：

- 一开始读错文件，后续所有修改都偏了。
- 搜索范围太宽，context 被无效信息占满。
- 只看局部实现，没看测试、调用方、配置和工具边界。
- 修改后没有用正确的 verifier 验证，导致“看起来修了，其实契约没满足”。

当前实现里专门为这一步做了几类支撑：`rg/list_files/repo_map/code_outline/read_file/read_files` 用于定位，working memory checkpoint 保存已读线索，subagent 可以并行做只读探索，workspace diff 和 verifier 再检查最终改动是否偏离预期。

## 3. 需求理解阶段怎么判断读哪些文件？

现在主要是模型规划 + 工具搜索 + 代码结构工具组合，不是完整静态依赖图，也没有接 LSP。

实际策略是：

- 用户明确提到文件时，优先读这些文件。
- 用户只描述功能时，先用 `rg` 搜关键词、函数名、错误信息、CLI 参数、测试名。
- 对仓库陌生时，用 `list_files` 或 `repo_map` 了解目录结构。
- 对大文件，先用 `code_outline` 看类、函数、符号轮廓，再读具体窗口。
- 改 bug 时，通常会同时读实现、测试、调用方和配置。
- 如果是多条独立线索，可以早期用 `parallel_tasks` 派发 subagent 做并行探索。

这里的判断是 runtime 给出工具和边界，具体选择仍由 Lead 模型决策。代码里还没有 LSP 跳转、全量依赖图、AST call graph 这类更重的定位能力。

## 4. 代码定位用了哪些工具？

主要工具是：

- `rg`：首选全文搜索。
- `grep`：兼容型搜索工具。
- `list_files`：列目录和文件。
- `repo_map`：生成仓库结构视图。
- `code_outline`：提取单文件结构轮廓，适合大文件先粗看。
- `nl`：按行号读取片段。
- `read_file` / `read_files`：读取文件或多个文件窗口。
- `git_status` / `git_diff` / `git_log` / `git_branch`：查看变更和历史。
- `bash`：必要时执行项目命令、测试命令或更灵活的 shell 查询。

没有专门接 LSP。`find/tree` 不是一等工具，但可以通过 `bash` 执行；常规场景更推荐 `list_files/repo_map/rg`。

## 5. 为什么每个 coding 请求要创建独立 task session？

因为 coding 任务的中间过程很脏、很长、很具体，不适合直接塞进主聊天会话。

隔离的好处是：

- 主会话只保留用户可读的总结，不被大量 tool result、diff、错误输出污染。
- task session 可以有自己的 memory root：`.task_sessions/<task_id>/memory`。
- task session 可以独立保存 checkpoint、工作记忆、工具结果和最终 artifacts。
- 失败或中断后，可以用 task session 的状态恢复，而不是从主会话里翻长日志。
- 同一个用户主会话可以连续发起多个 coding task，彼此边界清楚。

代码上，`TaskSessionFactory.create()` 会生成 `task:<slug>-<uuid>`，元数据里保存 parent session、task type、user request、memory root 和用户身份。

## 6. task session 里保存哪些内容？

task session 保存：

- 完整 task 内部消息，包括模型消息、tool call、tool result。
- task metadata：`kind`、`task_id`、`task_type`、`parent_session_id`、`status`、`user_request`、`memory_root`、用户信息。
- workspace metadata：`workspace_root`、`workspace_display_name`、`workspace_allowed_root`、来源和请求值。
- working memory：目标、完成单元、待处理单元、step checkpoint、已观察工具调用等。
- task artifacts 路径：`TASK_LOG.md`、`CONCLUSIONS.json`。
- task reply 和 promotion 统计。

workspace diff 不是直接塞进 session message，而是写到 run 目录下的 `workspace_diff.json`，并在 `run_state.metadata["task_session"]` 里保留摘要。

## 7. workspace diff 怎么记录？

不是简单依赖 `git diff`，而是 runtime 自己维护 before/after 文件快照。

`runtime/trace/workspace.py` 会：

- 遍历 workspace 文件。
- 排除 `.git`、缓存目录、`.runs`、`.sessions`、`.task_sessions`、`node_modules` 等运行产物或大目录。
- 对 10MB 以内文件记录 `sha256`、size、mtime。
- 执行后再次 capture。
- 对比 before/after，得到 `created`、`deleted`、`modified` 和 summary。

这样即使 fixture 不是干净 git 仓库，也能判断 agent 实际动了哪些文件。benchmark 仍会初始化 git，方便人工看 diff，但判定主要使用 workspace snapshot diff。

## 8. 任务执行到一半失败，怎么恢复？

当前是“语义恢复”，不是精确 replay。

失败或中断时，能帮助恢复的字段主要来自 working memory 和 task session：

- `objective`：本次任务目标。
- `completed_units`：已经完成或验证过的线索。
- `pending_units`：未完成子任务、阻塞原因、上次失败信息。
- `step_checkpoints`：每个 reasoning step 的摘要、工具调用和工具结果摘要。
- `observed_calls`：已观察到的工具调用签名。
- `last_checkpoint_step`：最近保存到哪一步。
- `status`：running、suspended、completed 等。
- `archived_findings["last_stop"]`：停止原因和最后回答。

`reasoning_loop` 会在关键节点调用 checkpoint callback 保存 session。恢复时，Lead 可以读取这些结构化摘要继续推进。短板是目前还不是“从第 N 步完全重放模型状态”，也没有事务式 patch queue。

## 9. checkpoint 里的“已完成线索、待处理线索、失败原因和证据”是什么格式？

`runtime/working_memory.py` 里比较核心的格式是：

- `completed_units`：包含 `unit_id`、`description`、`conclusion`、`evidence_refs`、`covered_scope`、`open_questions`、`needs_parent_verification`、`agent_type`、`status`。
- `pending_units`：包含 `unit_id`、`description`、`scope_files`、`agent_type`、`status`、`priority`、`state`、`blocked_by`，失败时还会补 `last_failure_reason`、`last_failure_message`。
- `step_checkpoints`：包含 `step`、`phase`、`message_count`、`assistant_summary`、压缩后的 `tool_calls`、压缩后的 `tool_results`、`note`、`timestamp`。
- `archived_findings`：放较长期的过程结论，例如继承父 working memory、最后停止原因、最终答案。

subagent 结果也会结构化写入，例如 `findings`、`evidence`、`covered_scope`、`open_questions`、`failure_reason`、`retry_hint`。

## 10. Lead / Teammate / Subagent 分别是什么？

Lead 是主 coding task session 里的决策者。它负责理解需求、决定读哪些文件、是否分派子任务、最终合成结论、控制改动范围和输出最终报告。

Subagent 是短生命周期的局部执行单元。它通过 `task` 或 `parallel_tasks` 创建，session id 类似 `subtask:<agent_type>:<uuid>`，有独立 context、受限工具集和大约 16 步 reasoning budget。适合做 bounded 的 locate/list/extract/plan/focused code 任务，返回结构化 `SubagentResult`。

Teammate 是持久协作者。它由 `spawn_teammate` 创建，运行在独立线程里，session id 类似 `teammate:<name>`，通过 inbox/bus 接收消息，可以 idle、claim task、请求 plan approval 或 graceful shutdown。它更像长期协作成员，不是一次性工具调用。

## 11. Lead 怎么决定要不要派发 Subagent？

决策主要由 Lead 模型做，runtime 用工具 schema、prompt guidance 和 guard 限制边界。

当前建议的使用场景是：

- 多个独立文件或模块需要并行调查。
- 大范围架构阅读，但每条线索可以被切成 bounded scope。
- 只读探索、定位、提取证据。
- 需要一个短计划，但不需要多轮反馈。

避免过度并行的机制包括：

- `parallel_tasks` schema 限制最多 8 个任务。
- `agents/subagent/parallel.py` 的 `MAX_PARALLEL_SUBTASKS = 8`。
- `guard_subagent_dispatch()` 限制每次 run 的 fan-out 次数。
- 同一 clue 多次失败后进入降级阶梯，禁止继续重复派 subagent。
- subagent 有自己的 reasoning step 上限、timeout、自动重试预算。

所以“要不要派发”是模型决策，“派发不能失控”由 runtime guard 兜底。

## 12. Teammate 和 Subagent 为什么不统一叫 Subagent？

因为生命周期和通信模型不同。

Subagent 是一次性调用：父 agent 给 prompt，subagent 跑完，返回 JSON 结果。它的核心目标是降低主上下文压力，快速拿到局部事实或局部 patch。

Teammate 是持久成员：它有名字、角色、状态、线程、inbox、bus 协议，可以多轮接收消息、自动 claim task、发 plan request、被 graceful shutdown。它更适合长任务、持续协作、需要等待和复用身份的场景。

如果统一成一种概念，会混淆“短生命周期并行探索”和“长期协作成员”两个工程问题。

## 13. 多 Agent 之间怎么通信？

不是共享完整上下文，而是通过结构化摘要和消息协议通信。

Subagent 通信方式：

- parent session 给它 prompt、description、scope、budget、deliverable。
- subagent 新建隔离 session，只继承必要 workspace metadata 和 working memory snapshot。
- 返回 `SubagentResult`，字段包括 `success`、`summary`、`payload`、`findings`、`files_touched`、`tool_count`、`failure_reason`、`evidence`、`covered_scope`、`open_questions`、`needs_parent_verification`。

Teammate 通信方式：

- 使用 `AgentMessage`，字段包括 `sender`、`recipient`、`type`、`payload`、`id`、`correlation_id`、`timestamp`、`ttl_seconds`。
- message type 包括 `task_assign`、`task_result`、`task_progress`、`query`、`response`、`plan_request`、`plan_response`、`shutdown_request`、`shutdown_response`、`broadcast` 等。
- `ReliableMessageBus` 用 `correlation_id` 做请求响应关联。

这样设计的目的，是让父 agent 依赖“可检查的结构化结果”，而不是让多个 agent 混在同一个巨大上下文里互相污染。

## 14. 多个 Agent 同时修改同一个文件怎么解决冲突？

当前没有完整的自动合并锁或事务式 patch queue，这是一个明确短板。

现在主要靠三层约束降低冲突概率：

- `parallel_tasks` 更推荐用于只读探索和 bounded extraction。
- `task` / `parallel_tasks` 的 schema 要求传 scope，Lead 应该把任务切到不同文件或不同问题。
- 最终 workspace diff 会暴露所有 created/modified/deleted 文件，Lead 和 verifier 可以检查是否出现意外修改。

如果多个 code subagent 同时改同一个文件，目前没有自动三方合并机制，也没有文件锁。更稳的方式是让 subagent 做定位和建议，由 Lead 统一应用最终 patch。后续可以重构成“subagent 产出 patch proposal，Lead 串行 apply”的模型。

## 15. 多 Agent 协作中怎么避免幻觉另一个 Agent 的结果？

主要靠结构化结果、失败分类和 trace 证据。

Subagent 必须按 JSON protocol 返回。`TaskSubagentRunner` 会抽取结构化结果，失败时标记：

- `format_valid=false`
- `format_error`
- `incomplete=true`
- `failure_reason`
- `failure_message`
- `retry_hint`
- `evidence`

如果 subagent 达到 step limit，还会额外发一个 no-tool summary prompt，让它只总结已经完成的工作，并标记 partial。父 agent 不应该把“没有返回的内容”当成事实，只能使用 `findings/evidence/covered_scope` 里明确给出的东西。

Teammate 侧也通过 `AgentMessage` 和 `correlation_id` 做消息关联，避免凭空假设某个 teammate 已经回应。

这还不是形式化证明，但比自然语言互相转述可靠很多。

## 16. 任务分解失败有什么兜底？

有几类兜底：

- `guard_subagent_dispatch()` 会记录 clue 的 dispatch attempts 和 failure reasons。
- 同一 clue subagent 失败次数达到阈值后，禁止继续派同类 subagent，提示改用 teammate、父 agent 自己处理，或如实报告 incomplete。
- `run_parallel_tasks()` 对 timeout、内部异常、无效结果做结构化失败包装。
- 部分可恢复失败会自动 retry 一次。
- subagent step limit 后会触发 partial summary，让父 agent 至少拿到已完成线索。
- working memory 会把失败原因写入 `pending_units.last_failure_reason` 和 `last_failure_message`。

也就是说，分错任务后不是无限重试，而是进入“缩小范围 -> teammate -> parent self handling -> honest incomplete”的降级路径。

## 17. shell 命令有什么安全限制？

当前有两层：工具 handler 内部的简单检查，以及 `ToolExecutor` 的 hook。

`run_bash()` 本身会拦截一些明显危险字符串，例如：

- `rm -rf /`
- `sudo`
- `shutdown`
- `reboot`
- `> /dev/`

`ShellSafetyHook` 更系统，会拦截：

- `sudo`
- `shutdown/reboot/halt/poweroff`
- `mkfs`
- `dd ... of=/dev/`
- 写入磁盘设备路径
- `chmod 777`
- fork bomb
- 对 `/`、`..`、`*`、`~`、`$HOME`、`/home`、`/etc`、`/usr`、`/var`、`/dev` 等危险目标执行 `rm -rf`

`ShellWorkspaceScopeHook` 会阻止 `cd` 到 workspace 外。`FileWriteScopeHook` 会阻止 `write_file/edit_file` 写出 workspace。

但这里要诚实说短板：当前没有完整 shell AST policy，`curl | sh` 没有被专门识别，`git reset` 也没有专门的 bash 级禁用规则。系统通过不暴露 `git reset` 这类专门工具、限制 workspace、trace 审计和危险正则降低风险，但离强沙箱还差一层容器/权限/命令语义策略。

## 18. verifier 怎么设计？

`evaluation/verifiers.py` 的 verifier 是任务合同式检查，不只看最终回答。

它支持：

- `must_pass_command`：在 workspace 下执行命令，要求返回码为 0。
- `modified`：指定文件必须被修改。
- `created`：指定文件必须被创建。
- `not_modified`：指定文件不能被修改。
- `file_contains`：指定文件必须包含某段文本。
- `trace_events`：trace 中必须出现某事件。
- `tool_called`：必须调用过某工具。
- `tool_denied`：某工具必须被拒绝过。
- `run_status_completed`：run_state 必须是 completed。

这让 verifier 同时检查结果、行为、边界和运行状态。

## 19. benchmark harness 怎么判断任务成功？

`evaluation/harness.py` 的判定是：

```python
row["passed"] = (
    row["run_status"] == "completed"
    and within_budget
    and verifier_passed
    and workspace_diff_passed
    and trace_passed
)
```

也就是说，全部满足才 pass：

- 运行完成。
- 没超过 reasoning step budget。
- verifier 所有检查通过。
- workspace diff 检查通过。
- trace 行为检查通过。

benchmark 还会写 `rows.json`、`summary.json`、`summary.md`、每条任务的 `run_state.json`、`trace.jsonl`、`report.json`、`metrics.json`、`context_metrics.json`，方便复盘。

## 20. 如果模型修了 bug 但引入额外改动，怎么检测？

主要靠 workspace diff 和 verifier 的 `not_modified`。

执行前后 snapshot diff 会列出所有 `created/modified/deleted` 文件。任务可以在 expected 里声明：

- 哪些文件必须修改。
- 哪些文件必须创建。
- 哪些文件不能修改。
- 文件内容必须包含什么。

如果模型碰了不该碰的文件，`not_modified` 会失败，failure diagnosis 可以归类到 `unexpected_file_modified` 或 workspace diff mismatch。

短板是：如果任务没有把“不该改的文件”列进 expected，harness 只会记录额外 diff，不一定自动判失败。更强的做法是增加 allowlist 策略，例如“除了 expected.modified/created 外，其余文件默认不允许改”。

## 21. 和 Claude Code / SWE-agent / OpenHands 相比，优势和不足是什么？

优势：

- 更强调 runtime 可观测性：trace、run_state、metrics、context_metrics、workspace diff 都是内建产物。
- 有独立 task session，主会话和 coding 执行隔离。
- 工具可见性、工具 hook、workspace scope、loop guard 是 runtime 层能力，不只是 prompt 约束。
- 有 working memory checkpoint 和 task memory promotion，方便长任务恢复和沉淀。
- subagent / teammate 两套多 Agent 协作模型，能分别处理短任务并行和长期协作。
- benchmark harness 把 verifier、trace、workspace diff、step budget 合在一起，适合做工程化评测。
- 同一个 runtime 可服务 CLI/Web/Telegram 等入口，coding 只是 execution path 之一。

不足：

- 生态和成熟度不如 Claude Code 这类产品级工具。
- 没有完整 LSP、符号索引、依赖图和语义代码导航。
- 文件冲突处理还不够强，没有事务式 patch proposal/merge queue。
- shell 安全还不是强沙箱，缺少容器级隔离和完整命令语义策略。
- SWE-bench 官方 resolved 判定链路已有 adapter，但还不是大规模稳定成绩体系。
- 人机审批、权限升级、补丁审查 UX 还比较基础。
- 对大型真实仓库的长期统计还不足。

一句话总结：它的优势是“runtime 可控、可观测、可复盘”，短板是“产品成熟度、强沙箱、代码智能索引和大规模实证还没到头部工具水平”。

## 22. 有没有真实跑过开源仓库任务？成功率、平均耗时、工具调用次数是多少？

从当前仓库可核实的状态看：

- 有 `benchmarks/coding_tasks.json`，当前 24 个本地 coding benchmark 任务，覆盖 `bugfix`、`feature`、`refactor`、`safety`、`observability`、`memory`、`loop-guard` 等类别。
- 有 `evaluation/coding_agent_smoke/tasks.json`，当前 30 个 smoke task。
- 有 `evaluation/harness.py` 支持 `scripted` 和 `real` runner。
- 有 `evaluation/swebench_adapter.py`，支持 SWE-bench Lite / Verified 数据集加载、workspace 准备、调用 `TaskSessionRunner`、导出 patch，并可选择调用官方 SWE-bench harness。
- 有 matrix 示例 `evaluation/coding_agent_matrix.example.json` 和 `evaluation/swebench_verified_matrix.example.json`，指标包括 pass_rate、wall_duration_ms、avg reasoning steps、avg tool calls、tokens 等。

但当前 `.evals/` 目录里能直接看到的是 security RAG 评测结果，没有可引用的 Coding Agent 真实开源仓库汇总报告。因此面试时不要编具体成功率。

比较稳的说法是：

> 目前我已经把真实任务评测链路接起来了，包括本地 coding benchmark、smoke set 和 SWE-bench adapter；scripted runner 用来验证 runtime 链路，real runner 用来评估模型能力。当前仓库没有一份稳定公开的真实开源任务成功率汇总，所以我不会硬报一个数字。真正要报数时，我会引用某次 `summary.json/summary.md`：pass_rate、平均 wall duration、平均 reasoning steps、平均 tool calls、平均 tokens、失败分类和 workspace diff 检查结果。

如果面试官追问“那你怎么证明不是玩具”，可以补：

> 我证明的重点不是单次 demo，而是评测闭环：任务定义、隔离 workspace、trace、metrics、workspace diff、verifier、失败分类、SWE-bench adapter 都在。缺的是大规模 real-model 批量跑出的稳定统计，这也是我下一阶段会补的工程项。

## 23. 最值得主动承认的短板

这组问题里最容易被追问的短板，可以主动讲清楚：

- 没有 LSP / 全仓符号索引，定位主要靠模型规划和搜索工具。
- 没有强容器沙箱，shell policy 还不是完整命令语义分析。
- 多 agent 并发写同一文件还没有自动合并机制。
- checkpoint 是语义恢复，不是 step-level deterministic replay。
- real open-source benchmark 还缺稳定公开统计。

这不是减分项，反而能体现你知道系统边界在哪里。面试里可以收束成一句：

> 我现在这套 Coding Agent 的核心价值，是把 agent 从“能调用工具的聊天模型”工程化成“有 session 隔离、工具治理、workspace diff、checkpoint、trace 和 verifier 的可复盘执行系统”。短板主要集中在强沙箱、代码智能索引、并发 patch 合并和大规模真实评测。
