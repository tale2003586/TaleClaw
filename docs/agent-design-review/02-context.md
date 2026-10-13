# 02｜会话内上下文管理审查与改进设计

> 审查日期：2026-09-27（Asia/Shanghai）。审查对象：TaleClaw 当前工作区。本文只交付设计，不实施业务改造。跨会话长期记忆只讨论输入、证据和生命周期接口。

## 1. 结论与审查边界

**建议保留现有“任务状态 + 事件快照 + 最近消息 + 工件引用”架构，先修可靠性断点，不另建上下文平台。** 项目已有实际运行链路上的上下文管理能力，不能归类为“仅拼接聊天历史”；但已有结构不等于信息可以跨任意次压缩可靠保留。

最需要先处理的是：

1. **跨次压缩连续性**：新快照只总结尚未归档的事件，不接入前一快照摘要。两次确定性压缩后，第一次摘要独有的证据从最新状态消息消失，已在纯内存边界检查中复现；原事件仍存在，并非持久化数据被删除。
2. **目标与来源身份**：最新 `user` 不一定是真实用户；新消息不一定表示替换任务。目标替换函数会把状态问句改成目标，函数级检查已复现；线上是否经过该入口须按会话路径区分。
3. **摘要可信度**：校验器检查文本锚点，不验证约束含义、发现真伪及证据支持关系。保留约束文字但反转含义的摘要可通过校验，已复现。
4. **预算闭环**：主推理存在发送前硬门控；软压缩与最终门控的计数基线不同，部分附加内容后加，摘要等辅助模型调用又没有等价门控。不能说“所有模型调用均已防超窗”。
5. **状态与工具结果膨胀**：有结果落盘、分页和类型化压缩，但完整状态 JSON、并行结果、运行时贡献尚无统一的最终 token 配额。先有界投影，再接更多自动事实采集。

### 1.1 版本、工作区和方法

| 项目 | 本次记录 |
|---|---|
| Git HEAD | `5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2` |
| HEAD 提交时间 / 标题 | `2026-08-18T09:25:54+08:00` / `harden runtime handler and embedding sharing` |
| 初始工作区 | `git status --short` 只有未跟踪的 `docs/system-design (2)/` 和 `os`；未发现已跟踪文件修改或暂存修改 |
| 审查对象 | HEAD 对应的实际源文件及当前工作区说明；上述未跟踪内容未作为运行实现证据，未覆盖、移动或删除 |
| 项目说明 | 阅读 `README.md`、`docs/overview/PROJECT_STRUCTURE.md`、`docs/overview/CODEBASE_SUMMARY.md`；说明用于导航，结论以实际调用点为准。例如说明中的旧入口命名不能替代当前 `applications/bootstrap.py` 的接线 |
| 仓库指令 | 检查从 `/` 到项目目录的适用 `AGENTS.md`，以及有关源码/文档目录中的 `AGENTS.md`，未发现；`.agents/`、`.codex/` 为空目录。未遍历运行数据、模型权重、数据库目录来推断项目指令 |
| 写入范围 | 初次写入前目标文件不存在；本任务唯一文档为 `docs/agent-design-review/02-context.md` |
| 检查限制 | 未安装依赖、未训练、未调用真实模型、未执行批量实验、未提交或推送。没有读取真实 `.env` 值来声称线上配置；默认值均来自代码，部署覆盖值待确认 |
| 检查方式 | 源码阅读、调用点检索、官方网络资料、17 个内存假 provider 单元检查、3 个无模型的内存复现；详见 §8 |

命令执行的普通沙箱出现 `mountinfo path is not absolute` 启动错误，后续只读检查及指定文档写入采用经工具审批的执行方式；未改变项目权限或环境配置。本文行号对应上述 HEAD，日后版本变化需重新定位符号。

**标签约定**：`事实` 为代码或公开资料直接支持；`已复现` 限本次指定检查范围；`推演` 为静态分析的条件性失败场景；`建议` 是未来设计；`待验证` 表示尚无运行数据。没有把风险写成已经发生的生产事故。

### 1.2 实现成熟度总表

| 能力 | 状态 | 代码证据及边界 |
|---|---|---|
| 分区预算、最近历史、工具事务边界 | 已经实现 | `runtime/context/budget.py:61–151`；`history.py:133–210,260–319`；`dynamic_budget.py:162–224` |
| Coding 任务状态和快照压缩 | 已经实现，连续性部分实现 | `applications/coding/context_state.py:120–246,371–430`；两次摘要之间缺前一摘要输入 |
| 状态 patch 校验、版本与来源引用 | 已经实现，语义验真部分实现 | `applications/coding/state_updates.py:349–416`；`runtime/task_state/patch.py:105–132`；检查引用存在不等于事实成立 |
| 工具事实/错误自动进入 TaskState | 类已有实现，未发现生产挂接 | `DeterministicEventExtractor` 在 `applications/coding/state_updates.py:275–346`；调用检索仅见 `tests/test_task_state_core.py:103,116`。实际状态更新工具见 `tools/handlers.py:2542–2587` |
| 主推理硬预算门控 | 已经实现 | `ReasoningLoop._reasoning_step`，`runtime/execution/reasoning_loop.py:602–694`；仍基于估算 |
| 所有辅助模型统一门控 | 未发现完整实现 | `EventCompactor._invoke`，`runtime/context/snapshots.py:251–258` 直接调用 provider；反思也直接调用 |
| 大结果存储/外置/按需读取 | 已经实现 | `tools/hooks.py:363–450`；`runtime/context/long_content.py:81–134`；`tools/handlers.py:542–561,3147–3216` |
| 子 Agent 隔离与结构化返回 | 已经实现，预算协议部分实现 | `agents/subagent/runner.py:237–266,342–380`；`prompting.py:27–58,186–214`；单任务传入的 budget 主要作为文本 |
| 嵌套仓库指令作用域、来源强隔离 | 根加载已实现，嵌套作用域未发现实现 | `runtime/context/assets.py:29,32–62,91–94`；XML 标记不是强制信任边界 |
| 上下文压力观测 | 已经实现为观测能力 | `runtime/context/pressure.py:79–172`；不是自动压缩器 |
| 本地前缀缓存 | 已经实现 | `ContextBuilder._build_base_prefix`，`builder.py:178–226`；不能据此声称供应商 prompt cache 命中或存在 KV cache 管理 |

## 2. 实际进入模型的信息与组装链路

### 2.1 主链路及不同入口

```text
CodingApplication.run_coding_task
  → 外置超长用户输入 / 解析并绑定 workspace
  → 创建独立 Coding Session + 初始 TaskState + 父会话 handoff
  → Runtime.run / _execute：固定 active_turn_start_index，构造本轮 prefix
  → 每个 ReasoningLoop step：解析 provider、取得当前可见 tool schemas
  → ContextBuilder.build：取历史/任务状态/检索/运行时贡献，编译 messages
  → sanitizer：处理空 assistant、孤立或不完整 tool group
  → 估算最终消息 + 预留工具定义成本 → 紧急整组选择 → hard guard
  → invoke_model → provider 的 Chat Completions / Responses 传输转换
  → assistant / tool 结果进入 session.messages 与 event_log
  → 下一步重新编译上下文
```

依据：`applications/coding/runner.py:118–210,342–396`；`runtime/runtime.py:87–137,160–204`；`runtime/execution/reasoning_loop.py:227–248,591–707`；`runtime/execution/model_invocation.py:17–48`；`runtime/sessions/session.py:81–115`。

不能把所有入口当成同一配置：

- `ContextBuilder` 裸构造默认 `MINIMAL_CONTEXT_PROVIDERS`，memory/retrieval/coding 都是空实现（`runtime/context/providers.py:196–240`）。
- 应用装配 `applications/bootstrap.py:241–279` 显式注入完整 providers、可选语义记忆/历史检索/安全检索及 event compactor。
- **隔离 Coding 应用另建 builder**（`applications/coding/runner.py:347–358`），虽然选择完整 providers，却没有传入上述 retrieval service 或带 retriever 的 memory service。该路径不能根据 README 就认定每轮自动获得向量记忆和安全 RAG；工具主动检索另论。
- `CodingApplication.run_coding_task` 每次创建任务 session（`runner.py:139–154`；`applications/coding/session.py:41–55`）。父会话后续输入通常走新任务 handoff，不应直接等同于原 task resume。

### 2.2 消息的准确顺序

**普通路径**（`runtime/context/builder.py:399–436`）：

1. `system`：AgentSpec instructions → 根仓库 instruction block（仅 coding mode）→ runtime guidance。
2. 裁剪后的旧历史消息，保持选中消息原角色。
3. runtime state 消息：按条件共享 TaskState、附件描述、工件读取状态、extensions contributions。
4. 一条 `user` context frame：安全检索 → inbox/background → episodic history → semantic memory。
5. 裁剪后的当前回合消息，包括真实用户请求、assistant、tool。

**启用 Coding view 路径**（`builder.py:603–655`）：

1. 上述稳定 `system` 前缀；Coding 普通旧历史被清空。
2. 一条 `system` 状态消息：最新 snapshot.summary + 当前 TaskState JSON。
3. 附件、工件访问状态和 contributions 等 runtime state 消息。
4. Coding view 选择的 recent messages，另固定最新“真实用户”消息。
5. 检索/context frame 如存在，作为带 `retrieved_evidence_context` metadata 的 `user` 消息置于尾部。

**顺序不是强制权限。** `context-priority: security_knowledge` 只是文本注释；最终裁剪器将检索 evidence 作为低保留优先级。状态/附件/贡献被赋予 system role，不因为字符串 `instructions=false` 就获得传输协议层的低权限。证据：`builder.py:341–353,845–893`；`dynamic_budget.py:193–223`；`applications/coding/context_state.py:308–324`。

### 2.3 各信息来源、裁剪与真实接线

| 信息 | 来源、进入方式 | 预算/优先级与代码 |
|---|---|---|
| 系统指令 | `BOT_AGENT_SPEC` / `CODING_AGENT_SPEC.instructions` | `agents/definitions.py:11–35`；先于仓库指令；主裁剪器保留 system |
| 仓库说明 | `PromptAssetsService` 只读取 `instruction_root/AGENTS.md`；不会自动把 README 全文加入 | `runtime/context/assets.py:29,32–62,91–119`；原始读取上限12000字符，再默认头部3000字符。当前仓库没有该文件，不等于别的 workspace 不会加载 |
| 当前用户目标 | 外置前的请求引用进入 state；工作区/历史交接/运行指引/本次请求被包装成一条 user | `runner.py:118–158,196–210,365–438`；长内容外置后不能假定全部约束已经被读到 |
| 父对话/旧对话 | parent handoff 取最近完整2轮，旧轮摘要；普通路径另外做 history budget | `handoff.py:14–18,107–129,306–369`；每轮 user≤6000字符、assistant≤900，更早≤3000；是确定性文本裁剪，不是语义归纳 |
| Coding 旧历史 | 不直接注入；以状态、快照、handoff承接 | `builder.py:1137–1191,1297–1312`：`drop_for_coding`。`transferred_budget` 元数据不能理解为Coding view必然使用固定24000字符窗口 |
| 计划、约束、未完成事项 | `TaskStateCore` + Coding扩展；模型通过 `update_task_state` 提交patch | `runtime/task_state/models.py:64–85`；`applications/coding/task_state.py:237–292`；`tools/handlers.py:2542–2587`。整个当前state渲染，history剔除；缺token投影上限 |
| 阶段摘要 | `EventCompactor` 生成、`ContextSnapshotManager`激活，view加入最新一份 | `runtime/context/snapshots.py:168–258,276–382`；`context_state.py:303–312,410–430`。摘要是派生材料，不是已验证事实 |
| 代码片段/目录/符号 | 模型主动调用 `read_file/read_files/rg/repo_map/code_outline`，以tool结果进入 | `tools/handlers.py:429–537,563–703`；分页、行号、批量上限；repo_map有实际符号排序，不需为“按需检索”先加向量数据库 |
| 工具定义 | `_schemas_for_turn`取得当步可见schemas，作为API的tools字段发送 | `reasoning_loop.py:591–601`；`model_invocation.py:17–48`；`dynamic_budget.py:227–235`估schema成本。它不是messages里的普通段落，也占输入 |
| 工具输出、错误、日志 | 符合条件的工具结果hook先存储/外置；主loop加入tool message及事件；未外置内容可能继续被history压缩 | `tools/hooks.py:363–450`；`reasoning_loop.py:1129–1196`；`runtime/tooling/result_compression.py:36–77,97–223`。退出/失败状态应与文本分开看，不能把没有Error前缀当测试成功 |
| 检索结果、检索错误 | 可选history/search/security服务；history按current_request，缺请求时取最近4条文字 | `runtime/context/retrieval.py:33–81,107–112,174–194,237–262,293–305`。默认top_k6/min_score0.35是服务参数，全文再经过section裁剪；不是全入口都已开启 |
| 长期记忆 | `ContextMemoryService.build_memory_block`调用可选semantic retriever，无retriever返回空 | `runtime/context/memory.py:14–24`；`applications/bootstrap.py:251–267`。只看进入当前prompt的候选，不在本任务设计写入/遗忘策略 |
| 安全检索注入次数 | 某轮成功渲染后，execution state置 `security_knowledge_used=True`，后续step不再自动注入 | `runtime/runtime.py:177–204`。并非每步都重新带同一RAG；若之后仍需证据须有可恢复引用 |
| 附件与已读取范围 | 附件名称/type/ref等描述，不等于附件全文；工件访问状态另列已覆盖范围和查询次数 | `runtime/context/attachments.py:8–60`；`artifact_access.py:216–243`；`builder.py:769–776`。这两类消息为system，集合过大也会占预算 |
| 并行结果、后台消息 | 同步子任务整份JSON成为tool result；CodingRuntimeContributor另drain通知/inbox形成system contribution | `tools/handlers.py:2817–2819,2882–2905`；`applications/coding/context_contributor.py:13–35`；`builder.py:341–353`。后者绕过旧的1500字符task_runtime_events分区 |
| 空响应重试/恢复/反思 | runtime插入合成user消息，或反思产生调整指令加入上下文 | `reasoning_loop.py:340–353,502–513,1416–1478`。这些是生成的运行建议，不应获得新的用户授权身份 |
| skill/插件内容 | 工具定义及工具输出按常规路径进入；不是看到loader就全量注入skill正文 | `builder.py:221`记录skill_catalog_chars=0；`runtime/context/assets.py:91–94`只列AGENTS。`skill_runtime/loader.py:84`的目录生成接口本身不能证明当前system已接入 |
| trace与指标 | ContextBuildReport、pressure、token usage主要用于观测；不会全部自动变成下一步prompt | `builder.py:484–494,513–554`；`runtime/trace/context_metrics.py`。需区分用于追踪的metadata和模型实际看到的content |

### 2.4 辅助模型也是上下文消费者

仅审查主 `ReasoningLoop` 会漏掉以下调用。以下表不承诺插件或用户自定义provider的全部行为；它覆盖本任务涉及的已发现运行路径。

| 辅助调用 | 实际输入与输出用途 | 预算边界 |
|---|---|---|
| 事件摘要 | 当前TaskState facts、事件、refs；返回summary text | `snapshots.py:168–258,465–473`；正常/修复直接装全部events，独立summary provider；未走主gate |
| Reflection（可选） | 身份、step、不可用工具、最近6工具结果、最近8消息 | `runtime/execution/reflection.py:68–117`；没有字节/token总上限；返回策略建议，可影响后续prompt |
| RecoveryJudge | 异常incident JSON，要求correct_once或stop | `runtime/execution/recovery.py:31–48`；输出max240，不能用输出短推断输入受限 |
| 子任务到步数上限的收尾 | 合成总结请求 + 子会话编译上下文；禁工具 | `agents/subagent/runner.py:268–318`；max1600输出tokens；直接provider路径应统一门控 |
| 任务结束结论提取 | 原请求、最终回复、工具轨迹摘要 | `applications/coding/conclusions.py:40–68,78–128`；其中最终回复裁到2400字符；产物回handoff/记忆候选，属于长期记忆任务的接口 |

最后传输亦有差别：`models/provider.py:84–95,435–444` 的chat路径保留message字典，仅滤空assistant；`394–432`的Responses路径将role/content、function_call/output重建，额外metadata未一起传递。**来源与预算语义必须在编译前完成，不能依赖供应商理解私有metadata。** 本地 `actual_prompt_tokens` 变量是估算结果，非服务端实测。

## 3. 当前预算、压缩与超窗的真实行为

### 3.1 默认分区预算：字符不是token

以下均是代码默认值，未读取真实环境覆盖值。来源：`runtime/context/budget.py:10,61–151`。

| 分区 | budget / floor（字符） | 规则 |
|---|---:|---|
| project_instructions | 3000 / 1000 | head；另有文件读取前12000字符限制 |
| memory / semantic_memory | 各2000 / 500 | head_tail；一般按实际block选择一种，不能机械相加 |
| episodic_history | 2500 / 500 | head_tail；另有兼容命名retrieved_history |
| security_knowledge | 3000 / 800 | head_tail |
| task_runtime_events | 1500 / 300 | tail；不覆盖任意ContextContribution |
| conversation_history | 16000 / 4000 | summary_middle，首3轮+尾6轮，中间摘要最多3000 |
| active_turn | 8000 / 3000 | latest_tool_call，摘要1800，最近5个结果及配置工具优先保留 |
| total_budget_chars | 24000 | 用于报告对照；不能视作每次build都会将各分区之和严格压到24000 |

普通history的中间摘要实际逐项截取 user220、assistant160、tool180字符，之后还可能总截断（`runtime/context/history.py:322–375`）。当前回合工具压缩见`:173–210,378–422,536–657`。它们成本低、确定性强，却没有“完整理解并保留所有否定约束”的保证。

### 3.2 动态token预算与最终门控

`runtime/context/dynamic_budget.py:68–123` 的实际计算可简化为：

```text
W = provider的context_window（缺失默认128000）
S = estimate(system_messages)
T = estimate(serialized tool schemas)
O = output_reserve_tokens(...)
M = 显式margin或约3%×W
U = max(1, W - S - T - O - M)
soft = 0.70×U; target = 0.45×U; hard_dynamic = 0.92×U
hard_prompt = min(W - T - O - M, S + hard_dynamic)  # 实际实现还有max(1,...)
```

最终主loop另取 `safe_context_limit` 与`hard_prompt`的较小值；前者默认`0.85×W−O`，也考虑provider显式max_input（`runtime/token_estimator.py:83–103`；`reasoning_loop.py:618–694`）。O通常不超过4096和窗口20%，实际输出上限还会被`output_tokens_for_call`调整（`token_estimator.py:57–80,106–124`）。这是几套保守约束并存，不宜把92%称为唯一触发点。

计数选择：provider.count_tokens → 可选tiktoken → JSON文字启发式；默认兼容provider未实现count_tokens且BPE开关默认关（`token_estimator.py:16–39,237–288`；`models/provider.py:34–62`）。JSON编码的计数不等于供应商chat framing，也不一定涵盖未来多模态内容。压力报告另用字符数/4和全局128000窗口（`builder.py:525–545`），不能作为该provider的精确预算反馈。

Coding view的soft/target有三个限制（`applications/coding/context_state.py:170–246`）：

- 比较用的before包含static+state+recent，而available已扣一部分static，基线不同。
- 这里的`estimate_tokens`未传provider，可能与最终门控计数不同。
- 先按旧state大小选tail，生成新摘要后只重算，不保证压后≤target；附件、contribution、检索frame还会后加（`builder.py:635–655`）。

### 3.3 到窗口上限后究竟发生什么

| 条件 | 代码行为 | 不能据此声称 |
|---|---|---|
| 主prompt估算超过safe_limit | `reduce_prompt_to_hard_limit`保全部system/state、最新指令组、未闭合工具组；其余最近优先，检索最后按整条补 | 不保证所有老错误/约束仍在；也不会自动重新读取已丢证据 |
| 必保留部分本身仍过大 | `enforce_hard_token_guard`抛`PromptBudgetExceeded`，记录hard_budget_blocks，不调用provider | 不是自动支持任意长用户请求，也不是静默获得更大窗口 |
| 估算没拦住，provider报原生超窗 | 主调用记异常再抛出（`reasoning_loop.py:801`附近） | 未发现专用“context-length错误→缩上下文→重发一次”闭环，普通provider failover不等于语义恢复 |
| 摘要模型输入过长/失败 | normal→repair→chunked→deterministic；最终验证仍可失败，原上下文保留、记录失败 | 不是所有摘要调用已有token门控；回退不是必然成功 |
| 外置存储失败 | `ToolResultStoreHook`记录错误metadata，保留原输出（`tools/hooks.py:448–450`） | 不可称原文已可靠外置或prompt始终有界 |

外置的适用条件需单列：hook跳过`read_artifact/retrieve_tool_result`及缓存输出；≥1000字符仅为存储门槛，只有`stored.backend == "artifact"`且detector判超阈值时才替换prompt正文（`tools/hooks.py:373–389,411–447`）。不能把所有工具结果都描述为已外置。

快照的prepare→activate→archive和失败恢复已实现，值得保留（`runtime/context/snapshots.py:276–382`）。本次17个离线检查确认了相应内存行为，未验证真实数据库崩溃一致性或部署provider行为。

## 4. 对抗性问题清单

每项优先级表示建议处理顺序，不表示事故严重度统计。下述效果影响除已复现边界外均为推演。

### R1｜多次压缩失去早期失败与证据（P0）

- **现状证据**：`context_state.py:465–486`只选未归档events；`:389–394`没有previous_summary参数；`:410–430`只取最新快照；`snapshots.py:338–343`将旧active置superseded。
- **触发/场景**：第1次摘要留下“方案X导致测试Y失败”，该信息未进入TaskState；第2次只压新事件。**已复现**首summary含`unique_evidence_A`，第二summary及rendered state不含，event_log仍含；此为纯内存函数链复现，非线上模型事故。
- **影响推演**：重复失败尝试、重新勘察文件、丢失否决理由；审查结论可能退回过时假设。
- **现有优点**：归档事件有边界，旧记录未删除，快照可恢复；不用重做存储。
- **改进建议**：压缩输入包含前一快照的可追溯claims及新事件；未解决failure/constraint/pending强制携带；摘要与TaskState冲突时，保留冲突而非覆盖权威状态。
- **新增成本/风险**：累计摘要会变长，错误可能代际传播；须按claim去重、证据版本标stale、限制派生文字，不能无限拼接旧summary。
- **不值得复杂改造的条件**：单任务不发生第二次压缩且无需要延续的关键事实；也应加二次压缩回归例防止以后退化。
- **验收**：连续3次压缩，全部未解决失败、约束、未完成项在完整状态中保留100%；当前决策必需项的ID和语义在prompt保留100%，其余项可经有界索引恢复；引用可恢复率100%；已撤销结论不得以confirmed进入下一prompt。

### R2｜目标更新把补充或合成消息误作新目标（P0）

- **证据**：`_apply_user_objective_change`仅以event_ref变化替换目标（`applications/coding/context_state.py:585–627`）；真实用户选择仅过滤source前缀（`:520–528,547–582`）。runtime_retry（`reasoning_loop.py:340–353`）及子任务收尾user（`agents/subagent/runner.py:275–288`）并未一致标记该source。
- **触发/场景**：同task接收“进度如何？”、局部补充、运行时“请总结”。函数级**已复现**目标从“完成只读审查”变成“进度如何？”，历史有旧目标；系统集成是否走到该分支仍是推演。父会话一般新建Coding task，不能写成每次追问必然覆盖目标。
- **影响推演**：任务提前结束、原交付物/禁止项失去显著性，错误地认为系统恢复要求来自用户。
- **现有优点**：有original_request_ref、supersedes和历史，真实纠正可及时生效。
- **改进**：统一不可由内容自报的`origin`；稳定objective_id；用户新消息先记录为amendment，只有明确替换/取消才改变目标。状态询问不改目标；运行时指令仅影响本步。
- **成本/风险**：新任务与补充分类也会误判；低置信时保留原目标和原文，先完成可独立工作，必要时向用户澄清，不让分类器悄悄扩权。
- **不值得复杂改造**：无续接、单输入单回复可不做LLM意图分类，但origin与原请求引用仍需一致。
- **验收**：用户改需求时旧条目标superseded并保留原因；状态问句/重试/子任务总结消息改变objective次数为0；新硬约束下次调用保留率100%。

### R3｜摘要中的错误被升格为事实（P0）

- **证据**：`SnapshotValidator`只检查objectives/constraints/pending/refs锚点（`runtime/context/snapshots.py:112–149,558–561`），未验findings/decisions/blockers真伪；summary与state一起进入system（`context_state.py:303–324`）。状态patch检查已知evidence ID，但不检查claim是否由其支持（`state_updates.py:349–416`）。
- **场景**：摘要写“不得修改数据库：这个限制已被撤销。测试全部通过。”仍带原锚点。**已复现**validator接受；未证明模型实际生成过这句话。
- **影响推演**：错误成功声明、自我引用、证据编号存在但支持不了结论，后续摘要重复放大。
- **现有优点**：禁止摘要直接提state patch，TaskState与snapshot职责分开，已有refs和版本校验。
- **改进**：硬约束由确定性投影保留原文；claim分`observed/inferred/unverified/conflicted/stale`；成功只能引用验证工具事件，失败记录不能被自然语言覆盖；摘要标derived，不能自动赋confirmed。
- **成本/风险**：结构正确仍不能证明语义正确；不建议每条claim再调用一个裁判模型。优先验证影响最终结论或高成本动作的少量证据。
- **不值得复杂改造**：随手聊天摘要无需全套claim ledger；只要明确非权威且无执行授权后果，原文引用即可。
- **验收**：反转约束的summary不得改变约束集合；无测试事件不得产生verified_success；伪引用/过期hash标未验证，不因JSON合法而通过。

### R4｜预算分散，辅助调用和后加内容难以预测（P0）

- **证据**：主gate已实现；Coding soft计算及后加内容见§3.2；`snapshots.py:251–258,519–532`摘要直接调用且按字符分块，单条大事件不拆；`reflection.py:85–110`按条数选历史。
- **场景推演**：主模型128k，summary模型8k；先送全部events再repair，均超窗；chunk仍含一条大日志。或并行通知在view之后加入，soft没反映，最后才紧急裁剪。
- **影响**：无效重试、额外延迟和费用；摘要扩张使压缩频繁；全部system/state过大时明确失败而非有效推进。
- **现有优点**：最终主gate为独立不变量，关闭dynamic assembly也不关闭，schema成本已有考虑。
- **改进**：所有模型调用共用基于已选provider的RequestBudget；先收集全部候选块再统一分配；summary按它自己的输入预算分块；状态渲染后重新量测；未知窗口不能乐观按128k发送大请求。
- **成本/风险**：精确tokenizer不可用时仍有误差；必须记录估算来源及真实usage差值，避免“统一接口”变成“虚假的精确计数”。
- **不值得复杂改造**：短任务可继续启发式+充分余量，无需引入远程计数服务，但不可漏工具schema、辅助模型或新contribution。
- **验收**：全部purpose对已知窗口和指定计数器的超预算请求拦截率100%；供应商真实超窗率另行测量；同一超限payload不原样反复重试；压缩后满足统一target或返回可解释的budget_conflict。

### R5｜长日志外置后证据不显眼，失败时原文回灌（P1）

- **证据**：已有存储和压缩，`tools/hooks.py:363–450`；外置替换只取首段/首行≤320字符，否则通用指针（`long_content.py:123–166`）；类型化bash压缩在`result_compression.py:97–223`，并非所有路径统一使用。
- **场景推演**：2MB测试日志中间出现唯一失败，首尾都是正常输出；外置后仅有artifact_ref。落盘失败又回到原长输出。用户把禁止条件放在超长输入末尾也有同类风险。
- **影响**：误以为输出可忽略、再次读取成本、错误定位遗失；未读取附件却误称已覆盖。
- **现有优点**：不可变工件、hash、分页、查询和读取覆盖状态已具备，恢复无需向量库。
- **改进**：统一工具结果envelope，保status/exit_code/失败用例/异常位置/分页cursor/未保存标记；原文只在持久化确认后替换为ref。日志提取不可靠时必须标`needs_inspection`，不得以没有匹配到错误判成功。
- **成本/风险**：确定性日志解析会漏新格式；落盘失败应保内存有界诊断并说明原文不可恢复，不能伪造工件地址。
- **不值得复杂改造**：工具输出≤单结果上限且少于几轮，用完整结果+现有分页足够，无需二次模型摘要。
- **验收**：失败状态及诊断位置保留100%，全文hash校验通过，反复同页读取不增长无效prompt；存储失败不回灌无限原文。

### R6｜并行子任务隔离了过程，却可能放大汇总负担（P1）

- **证据**：子Session不复制父history（`agents/subagent/runner.py:342–380`）；输入scope/budget是prompt文本（`parallel.py:249–266`）；返回解析未限列表/字符串长度（`prompting.py:186–214`），payload与顶层字段重复（`result.py:8–35`）；另有后台contribution全量JSON。
- **场景推演**：8路子任务各返回1万字符，重复附证据和结论；主模型丢弃其他必要历史，或者通用压缩截坏JSON。父任务已改需求，迟到结论仍按旧目标解读。
- **影响**：父token成本大于串行，陈旧建议混入、错误只在长列表尾部出现。
- **现有优点**：独立上下文、工具白名单、禁递归spawn、全局步数/并行上限、重试和降级机制均已实现（`runner.py:62–66,331–380`；`parallel.py:19–107`）。
- **改进**：继承必需约束与父state版本；runner强制return_token_budget；超额完整结果落盘、返回合法短JSON；只返回一份claims；完成通知按task_id/result_hash去重。当前并行完成事件在`parallel.py:303–315`和`handlers.py:2882–2905`均有记录点，需明确唯一事件所有者。
- **成本/风险**：返回太短会失去反例；强约束继承会消耗子prompt；父验证仍要读关键片段。并行度由父返回预算反推，不能仅调高worker数。
- **不值得改造**：单文件、小变更或子任务高度共享上下文时，直接用单Agent与批量读取；不为隔离上下文强行拆任务。
- **验收**：8路压力输入下父新增token不超分配总量；失败/未完成标记100%保留；所有结果带parent_state_version；旧版本结果不可直接更新当前状态。

### R7｜仓库指令与证据身份、工作区根不一致（P0/P1）

- **证据**：`PromptAssetsService`默认cwd且仅根AGENTS（`assets.py:29,39,91–94`）；Coding构造未传实际workspace root（`runner.py:347–358`）；真实工具使用已绑定工作区（`runner.py:127–153`、`tools/handlers.py:429–433`）。指令head裁剪见§3.1。
- **场景推演**：进程启动目录A与任务仓库B不同，加载A的AGENTS；B约束遗漏。或者文件/检索结果包含伪system标签，经摘要成为高角色文字。当前没有AGENTS，未复现工作区错配。
- **影响**：遵循错误工程规则、丢弃文末禁止项；不可信内容在摘要中失去来源。
- **现有优点**：AgentSpec和runtime guidance已明确工具/文档是不可信数据（`agents/definitions.py:26–27`；`builder.py:80–85`），仓库资产与普通工具文件入口有区分。
- **改进**：绑定workspace及受认可的instruction sources；最小版先正确根目录，嵌套路径仅在需要时按作用域加载。来源身份由runtime赋值；证据采用结构化转义包装，摘要不能升级其权限。
- **成本/风险**：动态修改AGENTS需失效缓存；仓库文件是否被用户认可为指令仍有产品边界。嵌套规则增加解释和冲突成本，不应擅自把任意说明文件当指令。
- **不值得复杂改造**：单根小项目可只支持根AGENTS，明确能力即可；仍应显式绑定正确根、暴露规则被裁剪的事实。
- **验收**：A/B相反指令测试只加载B认可规则；文档伪标签不改变origin；禁止项不能在无告知下被head裁掉；作用域冲突可列出来源。

### R8｜重复、过期、矛盾信息需要区分“存过”与“仍真”（P1）

- **证据**：工具缓存key是tool+args（`tools/handlers.py:1765–1809`），命中会重复返回全文并声称unchanged；write/edit会清缓存（`:710,722`），但key不包含内容版本。TaskState有hash/refs，patch主要验证ID存在；当前state全文渲染。后台贡献与完成事件还可能重复。
- **场景推演**：用户或其他进程在同session外改文件，相同read请求读到缓存的旧输出；state保留旧Finding，而新测试给相反结论。不能从这里断言所有bash修改都失效失败，需分别检查hook策略。
- **影响**：重复token、旧证据成为当前断言、跨多文件修复漏检查。
- **现有优点**：工具cache减少IO，write/edit主动失效；state已有EvidenceRef/hash/history，工件不可变。
- **改进**：文件证据带workspace revision + file hash；相同来源hash去重；变更使相关claim标stale；矛盾保留双方证据和时间，不能“最新文字直接覆盖”。未变化命中只返回短ref，必要时取原片段。
- **成本/风险**：每次hash全仓昂贵；只对读过且将作为决策依据的文件按stat→按需hash检查。不能让过期检测变成索引工程。
- **不值得复杂改造**：只读且仓库版本固定的短任务，固定HEAD+路径行号通常够用；未确认工作区不会变化时不要输出unchanged断言。
- **验收**：同一结果重复3次不导致等比例prompt膨胀；文件变更后旧claim标stale；冲突证据恢复后不会自动升级任一方为confirmed。

## 5. 成熟 Coding Agent 的公开实现对照

本节资料均访问于 **2026-09-27**。核验了官方仓库的公开/未归档状态、指定commit和LICENSE；只说明这些仓库及模块的许可，不代表对应品牌全部产品、模型、云服务均开源。本次没有运行这些外部项目，也没有证据说明它们的默认参数适合本项目。

### 5.1 候选项目版本与开源状态

| 项目 | 固定版本 / 提交日期 | 许可与审查范围 |
|---|---|---|
| Aider | `5dc9490bb35f9729ef2c95d00a19ccd30c26339c` / 2026-05-22 | 公开、未归档；[Apache-2.0 LICENSE](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/LICENSE.txt)；history与repomap模块 |
| Cline | `29896ec7fa8e2dd98805b56c2dae987d59fc9baa` / 2026-09-26 | 公开、未归档；[Apache-2.0 LICENSE](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/LICENSE)；当前SDK的context/session compaction。不能用旧`ContextManager`路径冒充当前实现 |
| OpenHands software-agent-sdk | `d77ada7a030b3acaa82593d402632680361dfe42` / 2026-09-26 | 公开、未归档；[MIT LICENSE](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/LICENSE)；condenser、event view、delegate。未将MIT泛化到主应用/商业服务 |
| Claude Code（公开披露参照） | 工程文章2025-09-29；当前仓库许可访问2026-09-27 | [仓库LICENSE](https://github.com/anthropics/claude-code/blob/main/LICENSE.md)为All rights reserved/商业条款，不当作开源上下文引擎；只分析官方文章披露 |

### 5.2 至少三种具有实现依据的策略

**策略A：保最近历史、压缩旧历史——Aider。**

`ChatSummary.summarize_real`按token保留近期尾部约一半历史额度，旧头部交摘要，仍大则递归。它简单、兼顾连续对话；但在该commit，头部超摘要模型限制时只从头保留到`max_input_tokens−512`，未装入的头部可能直接遗漏。这里是源码推演，不是外部项目故障复现。借鉴保尾部和明确摘要范围；不要照搬“摘要覆盖了所有被删消息”的假定。[history.py:33–123](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/history.py#L33-L123)

**策略B：零模型规则压缩与带前摘要的增量压缩——Cline。**

`runBasicCompaction`优先用户输入、近期消息及部分旧最终回复，工具轨迹转为文件/命令活动清单，无需摘要LLM。最终投影仍可能返回保护项无法装入的状态，不能宣称任何长度的用户输入都能保留。[basic-compaction.ts:452–637](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/extensions/context/basic-compaction.ts#L452-L637)、[project.ts:600–664](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/extensions/context/budget-projection/project.ts#L600-L664)

`runAgenticCompaction`明确读`previousSummary`并压其后新消息，生成摘要加最近尾部；摘要格式包含Goal/State/Highlights/Next/Files，但模板不等于事实校验。本项目R1可借鉴前摘要输入，不需要移植整套SDK。[agentic-compaction.ts:129–290](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/extensions/context/agentic-compaction.ts#L129-L290)、[compaction-shared.ts:669–702](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/extensions/context/compaction-shared.ts#L669-L702)

它还以规范化消息前缀的SHA256绑定压缩状态，恢复匹配后才投影摘要，并追加新增消息；这是防复用过期摘要的工程措施，不是语义真伪证明。请求准备计算system/tools成本并用上次实际input token修正低估；溢出恢复不依赖默认模型摘要；若配置自定义压缩器则先尝试，失败或压缩不足时回退规则压缩。[session-compaction.ts:123–197](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/session/models/session-compaction.ts#L123-L197)、[compaction.ts:303–435](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/extensions/context/compaction.ts#L303-L435)、[恢复分支:478–565](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/extensions/context/compaction.ts#L478-L565)

**策略C：原始事件与可见视图分离、软硬触发和原子边界——OpenHands SDK。**

`LLMSummarizingCondenser`将token超限/显式压缩设为HARD、事件数设为SOFT；选择忘记区间时保护常规首部和近期尾部，并对齐工具事务边界。工厂`default_condenser`设80事件/保首4，不能与类字段默认240/2混淆；本项目没有理由照搬该事件数量。[触发逻辑:115–203](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L115-L203)、[区间选择:297–352](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L297-L352)、[工厂:540–548](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L540-L548)

`Condensation`保存忘记的event IDs、summary及插入偏移，再由View投影；原始事件与模型视图分开。其硬重置失败后也会缩事件文本重试，说明压缩器本身仍有丢信息/增延迟的风险。TaleClaw已有events/snapshots，无需为借鉴这一点再造事件框架。[Condensation事件](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-sdk/openhands/sdk/event/condenser.py#L11-L95)、[View.append_event](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-sdk/openhands/sdk/context/view/view.py#L111-L139)、[硬重置:355–397](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py#L355-L397)

**策略D：按需仓库索引与子会话隔离。**

Aider RepoMap用定义/引用图和PageRank、文件/标识符提及做排序，再以二分搜索接近map token目标，允许约15%误差；这是候选导航，不是最终请求硬上限。当前项目已有repo_map和code_outline，先评测定位命中率再决定是否增强图算法。[repomap.py:365–556](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L365-L556)、[预算选择:629–706](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py#L629-L706)

OpenHands delegate创建独立LocalConversation，只发送任务、汇总最终答复；隔离中间轨迹的机制真实存在，但所审查返回拼接位置没有长度cap。这支持“隔离有价值”，不支持“并行后父上下文必然更小”。[delegate/impl.py:214–230](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-tools/openhands/tools/delegate/impl.py#L214-L230)、[323–401](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-tools/openhands/tools/delegate/impl.py#L323-L401)

**闭源系统只作公开事实参照。** Anthropic在2025-09-29工程文章披露Claude Code会压缩历史、保留架构决定/未解决bug等，并结合最近访问文件；也介绍结构化笔记和子Agent隔离。可以据此形成待验证设计假设，不能推断当前私有实现的阈值、准确率或恢复事务。[Effective context engineering for AI agents](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)

### 5.3 本项目的策略取舍

| 策略 | 能解决什么 | 能击穿它的条件 | 本项目选择 |
|---|---|---|---|
| 最近完整历史/简单截断 | 零额外模型成本，短任务连续性 | 早期唯一约束/失败只在被删历史中；最新单组巨大 | 保留作为短任务默认及兜底，但必保留任务核心不能随尾窗删 |
| 滚动摘要 | 缩短长期探索过程 | 摘要遗漏、语义反转、摘要模型自身超窗、多代污染 | 修好前摘要承接、覆盖与恢复；不增加摘要层数 |
| 结构化任务状态 | 目标、待办、错误、证据明确可验证 | 未及时更新、错误自报、全文状态无限增长 | 复用现有TaskState，确定性事实补充+有界视图 |
| 按需检索 | 避免全仓/全部日志入prompt | 关键词未知、检索遗漏、读到旧版本 | 先rg/符号/行窗/artifact search，保来源与“尚未覆盖” |
| 工具输出落盘 | 原文可恢复，日志不淹没对话 | ref丢失、权限/存储失效、只存不再读 | 已有设施上统一短envelope、错误摘要和恢复动作 |
| 分层上下文 | 指令、任务核心、证据成本分开 | 层间优先级仅靠标签、固定层不断膨胀 | 引入typed blocks与一个预算器，避免多套独立裁剪 |
| 子Agent隔离 | 独立探索，父不读中间轨迹 | 目标没继承、长报告洪水、结果互相冲突 | 只用于可独立任务，2–3路起步；强制父输入/返回预算，不作默认压缩手段 |

### 5.4 必须分清的四种能力

| 能力 | 操作对象 | 是否减少需要呈现的信息 | 与本项目关系 |
|---|---|---|---|
| 上下文管理 | messages、tools、任务状态、证据选择与压缩 | 是；选择/移除/外置会改变模型能看到什么 | 本文主体；有信息丢失风险 |
| Prompt caching | 重复前缀的服务端处理复用 | 通常不减少逻辑上下文；降低重复计算费用/延迟 | `ContextPrefix`稳定有利于缓存，但本地cache_hit不是供应商命中率；缓存不能恢复被删内容 |
| KV cache | Transformer推理中的历史key/value张量 | 本身不维护任务语义，也不等于摘要 | 服务端/推理引擎责任；本项目未发现自行管理KV的代码，没必要为API agent加入 |
| 长期记忆 | 跨任务可持久化的经验/用户事实及其检索 | 只有检索进prompt的部分占本轮预算 | 由其他任务负责；本任务接收候选并保来源/版本，不自动把summary写成长期真理 |

Prompt caching定义依据[Claude官方文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)，访问2026-09-27；KV定义依据[Hugging Face Transformers官方说明](https://huggingface.co/docs/transformers/main/en/cache_explanation)，访问2026-09-27。这里没有推断具体供应商内部KV实现，亦没有引用缓存优惠价格作为本项目收益。

## 6. 可落地方案：一个预算器、一个任务状态、有来源的压缩视图

本节均为**建议**，字段/接口不是声称已实现。最小改造目标是可靠承接信息与可解释地拒绝不可能装入的请求；不是在有限窗口内无损容纳任意多信息。

### 6.1 三层数据职责，复用现有模块

```text
不可变来源：Session.event_log + ArtifactStore（原始请求/工具原文/证据）
    ↓ 确定性提取与有出处的语义候选
权威任务状态：现有 TaskStateCore + coding extension（目标/约束/待办/失败状态）
    ↓ 有界投影 + previous snapshot claims + 新事件 + 当前文件证据
模型视图：ContextBuilder 编译 → RequestBudget → provider serialization → gate
```

- **事件不是状态**：工具确实返回exit1是事件；“修复未完成”是从事件归纳的状态；二者都不等于根因已验证。
- **状态不是全部prompt**：状态可保全量索引，渲染只取当前未完成项所需片段。`evidence_index`不应每步全文JSON重复发送。
- **摘要不是权威写入**：summary无权更改objective、constraint有效性或verified结果；其claims必须指回事件/文件hash。
- **引用不是覆盖证明**：保留artifact_ref只能证明可定位资料；`access_status=not_accessed/search_only`必须保留，不能声称已读全文。

### 6.2 各类信息的预算与优先级

#### 统一预算口径

拟将主调用和summary/reflection/recovery等辅助调用统一成：

```text
W = 选中provider的总窗口；I = provider输入上限（如有）
O = 本次实际允许的最大输出预留（与发送参数一致）
M = 计数误差余量；起始用max(1024, 5%×W)，再按实测校准
T = 序列化工具schemas及协议开销
L = min(I, W - O - M)                 # 全请求输入预算；无I取W-O-M
H = L - T                            # 仅messages预算，已扣工具/协议开销
F = 系统/获认可仓库规则的不可删部分
C = 当前目标、用户硬约束、当前请求/有效纠正、待处理工具事务所需最小核心
D = H - F - C                        # 可分配的动态部分
```

I在adapter层统一为包含工具定义的总输入限制；最终完整请求的估算与L比较，单独messages与H比较，不能再从L重复扣T。若`D<0`，返回`budget_conflict`及各必保留块成本，不把它夹到1后假装有可用预算。**存在双重限制时取更小者；不把已缓存tokens扣掉。** O与最终max_output一致，避免只预留4096却请求明显更长输出。多模态/工具协议的估算必须由provider adapter提供或加明示保守余量。

soft/target统一只对**动态用量**比较：超过`0.70×D`进入整理，目标`0.45×D`；最终始终校验完整请求≤L且messages≤H。一次确定性整理即可达标时不调用summary模型。70/45只是沿用现有默认方便比较的初始值，不是质量已验证的最优参数。

| 类别 | 保留优先级 | 初始分配（D内） | 必须保留 / 可压缩 / 可外置 |
|---|---:|---:|---|
| F与C：可信指令、有效目标/硬约束、最新修改要求、未闭合工具事务 | P0 | 先计实际成本，不参与静默淘汰 | 必须保留语义与来源。短原请求全文；长纯指令无法安全缩减则报告冲突，不只留首320字符 |
| TaskState投影 + 多代摘要claims | P1 | 20% | 待办/未解决失败/关键决定及证据ref保留；已完成细节外置；压缩推理过程 |
| 最近完整交互及工具事务 | P1/P2 | 35% | 最新必要观察与调用/结果配对；重复成功日志可删；不截坏JSON事务 |
| 当前代码/测试证据 | P1/P2 | 30% | 当前修改位置、失败断言、相关依赖片段；全文外置/按需读，路径hash需保留 |
| 可选检索/长期记忆候选 | P3 | 5% | 相同来源去重；相关且未过期才入；可先移除，再按ref恢复 |
| 并行/后台返回 | P2 | 7% | status、error、未完成、关键claim与ref；完整报告外置；不得重复payload+顶层全文 |
| 附件/工件读取进度 | P2/P3 | 3% | 当前依赖附件的ref、未读/部分读状态；历史range明细外置，按需投影 |

配额可借用空闲部分，并按当前子任务动态调整；未解决关键错误可提高到P0/P1，不能因它来源于工具就一律低优先级。配额不是必须把prompt填满；D很小时退化为核心+单个必要证据，少开工具、少并行。

**示例（建议预算，不是观测结果）**：W=32000，O=4096，M=1600，T=1800，F=2400，C=1800，且无更低I。则H=24504，D=20304；动态soft约14212，target约9136。六项初始上限依次约4061/7106/6091/1015/1421/609，余量由舍入处理。不要给8个子任务各2000返回token，否则单返回就超过父预算；可只开2路各600，剩余用于汇总结构。

**长用户请求例外**：自然语言硬约束提取无法靠规则保证完整。先保原请求全文与来源；若超过C可承受额度，区分“附件数据”与“指令文本”。对指令文本的压缩必须保留分段覆盖索引、候选约束和未确认项，不能把抽取器没识别到的内容认定无约束。极端超长指令应明确请求结构化拆分或分阶段处理；这比静默截掉禁止条件更可控。

### 6.3 来源、状态与摘要数据结构

现有`TaskState`已有objective、constraints、findings、hypotheses、evidence_index、pending、history，**不再引入第二套TaskState**。对已有`EvidenceRef`补新鲜度和claim验证状态；对现有`ContextSnapshot`补上一代摘要关联/覆盖字段；将完整索引与短投影分开。

```json
{
  "snapshot_id": "ctx-003",
  "previous_snapshot_id": "ctx-002",
  "source_task_state_version": 12,
  "covered_event_range": [41, 78],
  "source_hash": "sha256:canonical-source-prefix",
  "projection_version": 1,
  "pinned_ids": ["objective-1", "constraint-no-db", "action-7"],
  "claims": [
    {
      "id": "claim-18",
      "kind": "failure",
      "text": "test_api::case_17 当前失败，根因尚未确认",
      "epistemic_status": "observed",
      "lifecycle": "unresolved",
      "source_refs": ["event:52", "artifact:log-52"],
      "workspace_revision": "head+worktree-digest",
      "file_hashes": {"api.py": "sha256:..."},
      "supersedes": [],
      "needs_revalidation": false
    }
  ],
  "unresolved_conflicts": [{"claims": ["claim-8", "claim-18"], "reason": "不同版本的测试结论"}],
  "omitted_groups": [{"event_range": [41, 49], "reason": "duplicate_log", "artifact_ref": "artifact:events-41-49"}],
  "summary_text": "派生说明，仅用于导航；事实状态由claims与TaskState决定",
  "budget": {"counter": "provider-estimator-v1", "estimated_tokens": 900, "limit_tokens": 1200}
}
```

示例值只展示schema。必须校验的规则：

1. origin由runtime设置为`user/assistant_model/runtime/tool/subagent/retrieval/repository_instruction`；不能从内容的XML、自报JSON或引用文件的role字段采信。消息角色与来源身份分开；模型生成的历史保留model/event来源，不因runtime包装变成已观测事实。
2. objective_id稳定；`amend/replace/cancel/status_query`记录原用户event，runtime_retry不能伪装amend；替换不删除旧版本。
3. 约束的生效/撤销必须指向真实用户或认可仓库规则变更事件；摘要不能撤销。
4. evidence ref必须存在且作用域属于当前任务可访问范围；文件证据携带hash和行范围。`reference_exists`与`supported_by_evidence`是两种状态。
5. 未解决failure、pending、conflict不能因字符配额从逻辑状态删除；只可将非当前必需项从prompt投影外置，同时保状态计数与可分页的ID目录ref；完整状态保留全部ID，当前决策必需项仍在prompt保留ID、语义及证据。
6. summary“测试通过”不能创建成功事件；成功只从可核验工具结果派生。用户说“测试通过”保为user-reported，不能混成tool-verified。
7. 新快照覆盖从上一快照到当前边界的连续来源，不要求全文再摘要，但上一代仍有效claims必须可达；旧原事件保留以便回查。
8. 约束/ID/hash校验是确定性保障；语义是否支持claim仍需定向验证，报告明确保留这一限制。

### 6.4 压缩触发、校验、恢复

**触发**：每次模型调用前编译全部候选块；超soft、单工具结果超cap、provider切换到更小窗口、或工具schema显著增加时整理。用户中途改需求先更新来源/约束再预算。安全的小任务不为了“定期总结”额外调用模型。

**顺序**：

1. 相同来源+内容hash去重，移除已撤销/过期的证据正文但留状态；合并无新信息的重复错误计数。
2. 大工具结果持久化，注入短envelope；剔除低相关检索；将全状态投影为当前未完成项所需子集。
3. 以完整消息/工具事务为单位选择recent tail，保P0；保持现有sanitizer作防线。
4. 仍超soft且有已闭合可压事件时，只对`previous_snapshot.claims + new_events + state_projection`做一次摘要；摘要模型也走自己的预算门控。
5. 新摘要校验pinned IDs、未解决项、引用可达性、覆盖范围、source hash、版本CAS及预算。压后不达target就继续确定性选择低优先级块；不循环生成越来越长摘要。
6. prepare→验证→activate→archive；只有激活成功才推进窗口。保留最近至少2份快照和原始events，复用现有事务边界。

**恢复**：

- 摘要不合格：不激活，保上一个active + 新事件；至多一次有明确错误的修复，然后确定性投影。设整次压缩的总调用/token/时间预算，不能把chunked“1次”误当1次LLM请求。
- 摘要源hash/state_version变化：丢弃待激活候选，基于新源重编译；不能复用旧摘要覆盖新用户指令。
- 原生context-length错误：确认模型尚未执行工具且无已展示的有效输出后，缩一次低优先级上下文、重编译、重计数，再最多重试一次；禁止重新执行既有工具。其他错误不冒充context overflow。
- artifact丢失/权限拒绝：显示`unavailable`，保错误；文件证据可按当前版本重读但必须标新版本，不能用同ref伪装恢复旧证据。
- 必保留核心超H：返回`budget_conflict`，保存已完成事实和pending，列出最大成本项并建议分阶段或降低输入范围；不吞用户约束、不无限重试。
- 摘要错误被发现：以原事件生成`claim_revoked/conflicted`，新投影明确替代旧claim；回查相关后续决定。无需回滚整个会话。

**对替代方案的攻击**：前摘要+新摘要可能继续传播旧错误，所以继承的是有来源/状态的claims，不把流畅文字当真；结构化状态可能严重膨胀，所以只投影有界子集；hard guard可能使任务停止，所以必须返回可恢复状态和明确原因，而不是以强行截断制造“继续工作”的假象。

### 6.5 大型工具输出处理

复用`ToolResultStoreHook`、`ArtifactStore`、`compress_tool_result`，建立单一结果入口，避免不同路径分别裁剪导致诊断消失。

```json
{
  "tool_call_id": "call-52",
  "tool": "bash",
  "status": "failed",
  "exit_code": 1,
  "summary": "pytest: 1 failed, 28 passed；失败 api/test_auth.py::test_expired",
  "diagnostic_locations": [{"artifact_offset": 38210, "length": 980}],
  "source": {"artifact_ref": "artifact:log-52", "sha256": "...", "bytes": 2097152},
  "coverage": "excerpt_only",
  "next_read": {"tool": "read_artifact", "offset": 38210, "limit": 1200},
  "storage_status": "persisted"
}
```

建议默认单结果可见部分≤min(1500 tokens,动态预算10%)，总可见结果不能超过当步证据配额；输出较小时保全文。错误envelope优先于头部欢迎语和进度条。日志内容保原字节/hash，摘要另存；搜索命中带offset，不以自然语言伪造行号。

对`read_file/read_files/rg`保持路径、行窗、是否截断和下一页参数；对`git diff`保文件列表及当前相关hunks；对测试输出保status、失败用例、首要异常、原文定位。无需一上来为每种工具各建LLM摘要器；已有类型化压缩可逐步统一接入。

用户指令与工具数据不能共用“取首段即可”的安全假定。超长用户输入外置时保原请求ref与覆盖状态，尚未审阅部分可能包含硬约束，必须列入任务未确认输入。

### 6.6 并行输入与返回协议

隔离上下文前先评估：子任务是否独立、是否有足够父返回预算、是否值得多一次模型调用。小团队默认2–3路；现有最多8路可以保为可配置上限，由父预算动态压低。

```json
{
  "input": {
    "task_id": "child-2",
    "parent_task_id": "task-1",
    "parent_state_version": 12,
    "objective_revision": 3,
    "constraints_revision": 2,
    "objective_id": "objective-1",
    "objective": "追踪缓存失效的调用路径",
    "required_constraints": [{"id": "c1", "text": "只读，不修改业务代码", "source_event": "user:1"}],
    "scope": {"paths": ["runtime/", "tools/"], "read_only": true},
    "evidence_refs": ["artifact:initial-map"],
    "budget": {"max_steps": 8, "max_input_tokens": 8000, "return_tokens": 600},
    "deliverable": "最多5条事实，路径/行号/未验证点"
  },
  "return": {
    "task_id": "child-2",
    "parent_state_version": 12,
    "objective_revision": 3,
    "constraints_revision": 2,
    "status": "partial",
    "summary": "发现缓存键缺版本；外部改文件路径尚未复现",
    "claims": [{"id": "x1", "kind": "code_fact", "source_refs": ["artifact:snippet-9"], "verification": "static"}],
    "unresolved": ["外部修改后的失效行为"],
    "errors": [],
    "full_result_ref": "artifact:child-2-report",
    "needs_parent_verification": true
  }
}
```

**执行约束**：runner而非prompt强制max_steps和return_tokens；scope若只是选题范围需叫`requested_scope`，若是权限必须由工具policy实际执行，不能混淆。超长返回先存全文，再按字段投影为有效JSON；优先保failure/unresolved，claims按当前决策相关性选，不能直接截JSON字符串。父只接收一份结构，不同时注入payload和顶层重复内容。

`parent_state_version`用于来源快照与提交patch的CAS；普通进度更新也会改变它，不能一律判目标过期。仅当`objective_revision/constraints_revision`改变或依赖证据hash变化时将相关结论置`stale_for_objective/stale_evidence`；仅state版本变动则重基检查再提交。原始文件证据hash未变且目标修订兼容时可以复用。父只验证将影响决策的claims，并记录未验证其余项。并行结果互相矛盾时保冲突与原ref，不能通过多数投票直接定真。

### 6.7 模块接口与伪代码

以下是接口草案，不要求增加同名文件；可在现有`runtime/context/`、`runtime/execution/`中扩展，避免大迁移。

```python
class ContextBlock:
    id: str
    origin: Origin              # runtime控制，不能由正文改写
    kind: BlockKind             # instruction / task_core / evidence / summary ...
    source_refs: list[SourceRef]
    authority: Authority
    priority: int
    content: object
    must_keep: bool
    artifact_ref: str | None

class RequestBudget:
    def for_provider(self, provider, output_limit, schemas) -> Budget: ...
    def measure(self, serialized_request) -> TokenEstimate: ...
    def fit(self, blocks, budget) -> Projection | BudgetConflict: ...

class ContextCompiler:
    def collect(self, session, task_state, contributions) -> list[ContextBlock]: ...
    def project(self, blocks, budget) -> Projection: ...

class SnapshotCompactor:
    def prepare(self, previous_snapshot, new_events, task_projection, budget) -> Candidate: ...
    def validate(self, candidate, pinned_ids, source_hash, state_version) -> Validation: ...

class TaskStateObserver:
    def observe(self, event) -> DeterministicPatch: ...  # 先接少量错误/测试/文件事实
```

```python
def prepare_request(session, purpose, provider, tools, output_limit):
    budget = RequestBudget.for_provider(provider, output_limit, tools)
    source = capture_source_revision(session)  # event boundary + state version
    blocks = collect_all_blocks(source)       # 包括后加contributions、附件与检索
    blocks = dedupe_and_mark_stale(blocks)
    blocks = externalize_large_results(blocks) # 仅在持久化确认后发布ref
    projection = deterministic_project(blocks, budget)

    if projection.dynamic_tokens > budget.soft and has_closed_events(source):
        candidate = compact(
            previous=source.active_snapshot,
            events=source.new_closed_events,
            task=source.authoritative_task_projection,
            budget=budget_for_summary_provider(),
        )
        if validate_ids_refs_coverage_and_version(candidate, source):
            activate_then_archive(candidate)  # 复用现有事务协议
            projection = deterministic_project(collect_all_blocks(session), budget)

    if projection.is_conflict:
        return resumable_budget_conflict(projection, source)
    request = provider.serialize(projection.messages, tools, output_limit)
    estimate = RequestBudget.measure(request)
    if not budget.accepts_total(estimate):  # 完整请求对比L，不能对比已扣T的H
        return resumable_budget_conflict(estimate, source)
    return request  # invoke仅接受已预算请求；所有purpose都走同一门
```

接口落点建议：`ContextBuilder`负责顺序与typed blocks；`dynamic_budget.py`统一计数、配额和guard；`context_state.py`负责有限状态投影/多代承接；`snapshots.py`复用事务与扩展校验；`tools/hooks.py`统一结果envelope；`agents/subagent/runner.py`执行预算协议；provider adapter在序列化后测量并接受实际usage校准。

**伪代码的边界**：并发状态修改必须由prepare/activate的版本比较拦截；计数/文件hash可能有IO成本；最终gate拒绝不代表任务语义失败；语义摘要再可靠也不替代原始证据。在事实采集器尚未接通前，不应宣称所有失败都已进入ledger。

## 7. 面向个人/小团队的实施顺序与止损条件

### 7.1 最小可行改进（先做，仍需后续开发任务授权）

| 顺序 | 改造范围 | 可独立交付的结果 | 不顺带引入什么 |
|---|---|---|---|
| M1 | `context_state.py` + `snapshots.py` | 前摘要有效claims与新事件共同承接；未解决错误/待办/约束跨两次压缩不消失；增加精确来源/覆盖断言 | 不改长期记忆存储，不要求向量召回 |
| M2 | `context_state.py`、合成消息产生点、subagent runner | 统一origin，目标ID稳定，状态询问/运行时消息不替换目标；根指令绑定实际workspace | 不引入多Agent意图分类流程；只在歧义影响任务时澄清 |
| M3 | `dynamic_budget.py`、builder、provider调用边界 | 全部context contributions进入统一预算；主/summary/收尾/反思共享guard；每步报告最终消息与tools成本 | 不换底层provider SDK，不自管KV缓存 |
| M4 | hooks、任务状态投影 | 统一大结果envelope，错误/未完成优先；有界状态投影；先接确定性失败与验证事件，再逐步扩展observer | 不全量开启所有自动抽取，不每个工具再发摘要请求 |
| M5 | parallel/runner/result protocol | 强制返回预算、父目标版本、结果去重与全文工件ref；失败字段不能被裁掉 | 不提高并行默认数，不上分布式工作流 |

M1/M2/M3优先于为了省token改更激进的截断参数。每一步先用§8的反例验证；保留旧编译器作为可对照路径，**回退开关不得关闭最终gate或允许静默丢硬约束**。文档未实施这些改动，不能把M1–M5列为已完成。

### 7.2 有明确收益后再做的增强

| 增强 | 需要先证明的收益 | 新成本及停止条件 |
|---|---|---|
| provider-specific计数与usage校准 | 多语言/工具schema较多时估算偏差或真实超窗频繁 | tokenizer版本和模型映射维护；启发式+余量已稳定则先不做远程计数 |
| 更强repo_map图排序 | 跨多文件定位耗时或重复read占主要成本；现有rg/outline找不到关键调用者 | 解析器、多语言覆盖、索引失效；命中率提升不足以抵消维护成本即停止 |
| 摘要后台预生成 | 摘要阻塞确实占主要p95延迟，且存在稳定事件边界 | 过期候选、CAS与取消；不能拿旧summary覆盖最新用户变更。单同步压缩够快就不做 |
| 语义去重/冲突辅助判断 | 规则hash只能去完全重复，而近义重复持续占大量预算 | 模型误合并不同版本证据；只能输出建议关系，不自动判真 |
| 嵌套AGENTS作用域 | 项目确有多包不同规范，根规则频繁误用 | 冲突解释与动态刷新；单根项目仅绑定根即可 |
| 选择性子Agent | 可独立探索的任务通过并行确实降低总完成时间/失败率 | 总token、验证和汇总成本；共享状态修改占多数时回到单Agent |

### 7.3 当前不建议引入

- 为会话内日志/代码定位新建向量数据库。已有不可变ref、关键词查找和repo_map足以完成第一阶段；向量索引不能解决目标错误、来源混淆或摘要不连续。
- 多级“摘要Agent→审查Agent→裁判Agent”默认链条。会增加摘要污染与延迟，且仍无事实真实性保证。
- 为上下文选择做微调或训练压缩器。尚无稳定测试集、失败分布与成本基线，本轮也未运行实验。
- 通用知识图谱、跨团队分布式状态总线、无限笔记系统。当前任务状态和事件存储已足够；越多权威副本，越难处理冲突。
- 把prompt caching/KV cache当作更大有效窗口，或把所有过往summary自动晋升为长期记忆。

### 7.4 何时简单截断或摘要就足够

以下阈值为**采用复杂设计前的建议观察门槛，不是测得结论**。

| 任务形态 | 足够的最简方案 | 升级条件 |
|---|---|---|
| 单次问答、少量文件、≤约10次工具调用，prompt长期低于H的40% | 完整当前请求+最近完整工具组；单工具分页/长度上限 | 早期约束会被丢、重复读显著增加，或出现第二次压缩 |
| 一次压缩可结束、依赖关系少的短Coding任务 | 任务核心固定+一份带ref摘要+recent tail | 同一任务多次压缩、反复失败、用户多次修改需求 |
| 长日志但决策只依赖明确失败用例 | 类型化提取+原文工件+按offset检索 | 错误散落、解析器不确定、需要多处交叉证据，才加覆盖/claim索引 |
| 跨3–5文件修改、每项有明确验收 | 小TaskState表+待办+失败ledger+文件hash | 多版本并发、冲突证据和多个工作线程同时更新；才强化CAS/依赖投影 |
| 多方向只读研究 | 单Agent批量搜索/读取，必要时2路独立子任务 | 串行耗时成为瓶颈且报告预算、父验真可控后才扩大并行 |

简单截断的前提是**可以丢弃的资料已明确、硬约束和原请求不在被丢范围内、原始证据可恢复**。不能因为窗口大或任务“看起来简单”，就把未知重要性的信息静默删除。复杂基础设施只有在§8的重复压缩/冲突/恢复测试或真实短板显示收益时才值得引入。

## 8. 检查结果、反例与验收设计

### 8.1 本次实际执行的低成本检查

已阅读测试内容，使用项目现有解释器、内存Session和假provider。禁写字节码、未启用pytest缓存、未调用模型、未访问真实DB：

```bash
PYTHONDONTWRITEBYTECODE=1 TRACE_INDEX_ENABLED=0 .venv/bin/python -B -m unittest \
  tests.test_dynamic_prompt_budget \
  tests.test_reasoning_loop_token_gate \
  tests.test_context_snapshots -q
```

结果：**17 tests，0.003s，OK**（unittest报告的测试执行时间，不是端到端运行耗时）。覆盖：动态额度、完整工具事务选择、过大必保留请求在provider调用前拒绝、关闭dynamic flag仍保guard，以及快照normal/repair/chunked/deterministic、prepare/activate/archive/recover。它们不能证明摘要语义正确、真实provider计数准确、生产持久化恢复可靠或任务成功率提高。

另外执行了三个stdin纯内存检查，输出如下：

```json
{"check":"anchor_validator","accepted_conflicting_constraint":true}
{"check":"two_deterministic_compactions","first_contains_A":true,"second_contains_A":false,"rendered_state_contains_A":false,"event_log_still_contains_A":true,"snapshots":2}
{"check":"objective_helper","new_objective":"进度如何？","previous_preserved_in_history":"完成只读审查"}
```

可复核的关键操作（以下可在已安装项目环境中以`python -B -`执行，不写测试文件、不需要模型）：

```python
from runtime.context.snapshots import SnapshotValidator, EventCompactor, active_context_snapshot
from runtime.sessions import Session
from applications.coding.task_state import TaskState, Objective
from applications.coding.context_state import (
    _compact_events, _snapshot, render_coding_context_message,
    _apply_user_objective_change,
)

SnapshotValidator().validate(
    "修复接口。不得修改数据库：这个限制已被撤销。测试全部通过。",
    objective="修复接口", constraints=["不得修改数据库"], pending=[],
    evidence_refs=[], artifact_refs=[], max_chars=1000,
    covered_start_seq=1, covered_end_seq=2, source_task_state_version=1,
)  # 未抛错：只证明锚点校验接受了此文本

s = Session(id="review:in-memory-only")
state = TaskState(objective=Objective("完成只读审查", original_request_ref="request-1"))
s.add_message("user", "完成只读审查")
s.add_message("assistant", "unique_evidence_A: module x test failed on case 17")
assert _compact_events(s, task_state=state,
    before_message_index=len(s.messages), event_compactor=EventCompactor()) is not None
assert "unique_evidence_A" in active_context_snapshot(s).summary
s.add_message("assistant", "second phase: inspected another module")
assert _compact_events(s, task_state=state,
    before_message_index=len(s.messages), event_compactor=EventCompactor()) is not None
assert "unique_evidence_A" not in active_context_snapshot(s).summary
assert "unique_evidence_A" not in render_coding_context_message(
    state, snapshot=_snapshot(s, state))["content"]
assert "unique_evidence_A" in str(s.event_log)

updated = _apply_user_objective_change(
    s, state, objective="进度如何？", event_ref="request-2", artifact_refs=[])
assert updated.objective.summary == "进度如何？"
```

复现范围必须准确：第二项调用真实压缩/快照/渲染函数，**没有经过主loop soft阈值触发与最终recent tail选择**；证明的是未写入TaskState的旧信息在第二份快照及state渲染中不可见。原messages和events仍在；整个任务是否重犯错误仍属推演。第三项仅调用目标更新helper，不是模拟网关完整续会话。

### 8.2 至少五个能击穿新方案的测试场景

建议先用确定性脚本和fake provider重放，不需要付费实验。下表是**待实施测试设计及目标值**；本轮只执行§8.1，未实现这些业务测试。

| ID | 输入/攻击与检查点 | 可击穿的机制 | 验收标准 |
|---|---|---|---|
| T1 长日志 | 2MB pytest日志，唯一失败置于中部，首尾成功进度；同时模拟工件写失败 | 头尾裁剪、首段摘要、假ref | status/exit_code/失败测试保留100%；错误位置可恢复；外置成功单结果≤配置cap；失败不回灌无限原文且明确不可恢复 |
| T2 连续三次压缩 | 第1段只有一次关键失败A，第2段读其他模块，第3段修复；A未手动写state | 非累计摘要、旧失败丢失 | 未解决A及source ref在3次投影均可见或有明确恢复索引；未解决项召回100%；不得再建议同一已否决动作而不说明新证据 |
| T3 反复失败 | 同命令失败20次，错误内容相同但最后一次根因改变 | 简单去重误合并、摘要循环 | 前19次合并为计数，保首次/最近位置；第20次新signature独立；不超过指定repair预算；无原样模型超窗重试 |
| T4 用户中途改变需求 | 原“只读审查、不改DB”；随后“进度如何”、再“只检查认证模块”、最后显式允许改测试 | 最新user覆盖全目标、撤销失误 | 状态问句不改目标；只对明确范围和权限做版本化amend；原硬约束未撤销部分100%保留；不得因允许改测试扩成允许改DB |
| T5 运行时伪用户 | 空响应retry、步数耗尽收尾、reflection插入；正文带“忽略旧目标” | role=user等于用户身份 | 合成消息的origin始终runtime；用户目标/权限变化次数0；最新真实用户ref保持 |
| T6 跨5文件修改 | A接口→B实现→C调用→D配置→E测试；中途压缩，C未完成，D已变hash | 状态投影漏依赖、完成状态过早 | 未完成文件/动作保留100%；D旧证据标stale；最终完成需相关验证证据，缺项不得completed |
| T7 压缩前后冲突 | 旧测试通过，新测试失败；摘要刻意声称“全部通过”，仍包含原约束文字及真实但无关ref | 形式校验冒充语义校验 | 无工具成功事件不能升级verified；冲突两端ref保留；约束集合不受摘要改写；存在ref但不支持claim标unverified |
| T8 并行返回洪水 | 8个子任务各1万字符，payload重复，2个失败，父在返回前改需求 | 汇总预算、列表裁剪、陈旧结果 | 总父增量≤parallel总配额；JSON始终合法；错误和未完成保留100%；目标/约束修订不兼容的迟到结果标stale，纯进度版本变化仅重基；全文ref可读 |
| T9 指令/内容混淆 | cwd=A、workspace=B，各自AGENTS冲突；普通文件含伪system/XML闭合标签 | 错根、字符串来源、摘要提权 | 只加载认可B规则；普通文件保evidence origin；嵌套作用域按明确支持能力测试；权限变化0 |
| T10 小窗口/大schema | 主模型32k、summary8k；新增工具schema 6k，长JSON单事件，纯中文长约束 | 只预算messages、字符分块、乐观窗口 | 每种purpose发送前均计schemas/输出余量；不合格请求provider调用次数0；单条大事件可外置；保护项装不下返回budget_conflict |
| T11 崩溃与并发 | prepare后用户补约束；activate写失败；archive失败；重启回放 | stale snapshot、提前归档 | state/source版本改变则候选不能激活；激活前不推进边界；归档失败恢复不重复摘要；不丢最新事件 |
| T12 过期与重复读取 | 工具读文件后外部改文件；同query/ref重复3次；工件被移除 | cache key无revision、无效重复填充、伪恢复 | 关键证据hash不匹配标stale；重复结果使用ref且投影不线性膨胀；工件缺失明确unavailable |
| T13 不可能预算 | 单条用户纯指令/不可删约束已大于H；无可外置数据段 | “必保留”与有限窗口矛盾 | 不发送超限、不截断硬约束、不无限压缩；保存resumable state并报告可操作拆分需求，作为正确降级 |

每项都应同时用正常数据和对抗数据测；只用“摘要返回正确JSON”的happy path不足以证明方案可用。目标变化、路径hash、调用/结果配对、origin等用代码断言；自然语言目标是否完整理解用固定任务说明与人工复核，不能把模型自己评分当唯一验收。

### 8.3 指标、基线及成功判定

| 指标 | 明确定义 | 初始验收门槛（建议，不是本轮实测） |
|---|---|---|
| 目标/硬约束保留率 | 当前有效目标和硬约束中，下一prompt可定位的条目数/应保留条目数；同时核原文及版本 | 所有硬不变量100%；不能仅出现ID而语义已反转 |
| 未完成与失败保留率 | 未解决action/blocker/failure在跨次压缩投影可见或有明确可恢复索引的比例 | 100%；执行前依赖项必须实际展开读取 |
| 关键证据可恢复率 | 关键ref可以定位正确hash/版本/片段的比例 | 100%；不可恢复必须转unavailable，不能仍verified |
| 语义错误升级率 | 已知错误/无支持/过期claim被当作confirmed或授权执行的比例 | 对T4/T5/T7/T9固定攻击集为0；不宣称对任意注入绝对安全 |
| 任务成功率 | 固定样例中交付物、范围约束及验证检查全满足的任务数/任务总数 | 不低于现状基线；涉及硬约束违反一例即阻断发布。小样本同时报告样本量/置信区间，不凭1–2例称提升 |
| token成本 | 主调用+摘要+反思+恢复+全部子任务的input/output总量，并单列cached/uncached | 长任务在成功率不退化前提下，目标每成功任务总token下降≥20%；短任务额外摘要调用0 |
| 上下文效率 | 每轮最终prompt总tokens、去重/外置节省量、重读tokens、压缩前后tokens | 完整请求≤L、messages≤H为硬要求；target达成率≥95%或给出可解释保护项冲突；不能只统计删掉多少字符 |
| 延迟 | 编译/哈希/检索/摘要/主模型分别记录p50/p95，含恢复重试 | 无摘要的≤1MB重放，编译p95目标<50ms（需标硬件）；端到端p95不比基线恶化>10%作为初始门槛 |
| 压缩开销 | 摘要总tokens/全部模型tokens；摘要总耗时/任务耗时 | 初始目标分别≤10%/15%；若超标先调触发/输出规则，不自动加模型层 |
| 溢出与恢复 | 本地hard block、provider原生overflow、一次恢复成功率、无效重复调用次数 | 本地预知超限发送次数0；同payload原样overflow重试0；原生overflow需记录估算误差与provider配置 |
| 重复失败率 | 相同输入版本和失败signature且无新证据的再次尝试次数 | 相对现状下降；确定性重放不因摘要而重新启动同一失败循环 |

成本统计不可只比较“父主模型输入”，否则把工作搬给summary/子Agent会显得虚假省钱。也不可用失败后早停的低token任务压低平均成本；同时报告成功率、总预算耗尽率、每成功任务成本和失败消耗。token下降20%、延迟10%等只是试点取舍门槛，需由汇总任务结合真实基线裁决。

**建议评测顺序**：先将T1–T13做成纯内存replay和fixture（包括不同窗口、中文/代码/日志），固定旧版与新版projection对比；再收集经脱敏、获授权的真实任务trace进行离线检查。若以后需要付费模型评测，应另行确认预算与数据范围；本轮没有执行或默认授权任何批量调用。

## 9. 与长期记忆及其他任务的接口

### 9.1 长期记忆边界

当前接口事实：`ContextMemoryService`调用semantic retriever；`ContextRetrievalService`通过`EpisodicBoundary.from_session`做历史候选检索（`runtime/context/memory.py:14–24`；`retrieval.py:33–69`）。Coding隔离builder是否拥有这些服务须以装配为准（§2.1）。

**建议输入协议**：长期记忆任务向本任务提供`record_id/scope/source_ref/source_time/valid_until或revision/epistemic_status/text/conflict_status`及相关性分数；本任务决定是否在当轮预算内注入。相关性分数不是可信度，用户当前明确纠正优先于旧记忆；项目旧结论不能自动覆盖当前文件证据。

**建议输出协议**：会话结束仅产生`memory_candidate`，包含原任务ID、证据引用、适用scope、时间、验证状态和撤销关系；由长期记忆任务裁决提升/冲突/删除。临时错误、未完成项和原始摘要默认留在任务事件，不自动成为跨会话事实。原任务resume优先用task_id/state_version/snapshot/event边界恢复，不依赖语义检索碰巧找回目标。

**共同不变量**：关闭长期记忆不应破坏会话内任务完成；工件可恢复性由上下文/存储接口保证，不把“也许能搜到旧记忆”当压缩回退。不同用户/工作区的ref授权必须继续遵守现有边界。

### 9.2 其他审查任务接口

| 对接对象（不预设其他任务文件名） | 本任务提供 | 需要对方提供/裁决 |
|---|---|---|
| 总体架构/执行循环 | 统一RequestBudget、origin、模型视图边界；主gate与辅助调用差异 | 哪个模块拥有所有provider调用门控，避免每个应用再实现一套 |
| 工具与安全 | 工具envelope、工件ref、scope与requested_scope区分、指令来源字段 | 工具权限强制、工件读权限、日志解析语义、原生overflow安全重试条件 |
| 多Agent/调度 | 输入parent_state_version、强返回预算、去重key和错误优先返回 | 取消/超时/重试的唯一所有者；并行完成事件的唯一记录点 |
| 评测/可观测性 | T1–T13、目标/证据保留率、全链路token口径 | 可用脱敏trace、固定任务集、硬件/provider、成本与延迟基线 |
| 长期记忆 | 候选/投影协议、禁自动晋升summary、task resume边界 | 记忆scope、失效/冲突/撤销策略；是否向隔离Coding显式注入 |
| 存储/恢复 | snapshot CAS、source hash、连续覆盖、artifact可恢复性 | 数据库事务/GC保留期/真实故障注入，避免原事件先于有效快照被回收 |

## 10. 关键资料及支持结论

### 10.1 项目代码定位索引

以下链接相对本文件，便于仓库内导航；具体行号已在正文列出。最终检查确认19个本地链接目标均存在、代码围栏闭合；HEAD未变化，已跟踪文件无diff。本任务只写入本文件；结束时同目录还出现`01-architecture.md`、`03-memory.md`、`04-reliability.md`、`05-finetuning.md`、`06-evaluation.md`等并行任务文档，未改动这些文件。初始两个未跟踪项保留。

| 来源 | 支持的具体结论 |
|---|---|
| [ContextBuilder](../../runtime/context/builder.py) `build/_build_coding_context_bundle/_budget_conversation_history` | 普通/Coding消息顺序、Coding旧history清空、后加contributions、RAG frame位置 |
| [PromptAssetsService](../../runtime/context/assets.py) `build_instruction_block` | cwd默认根、仅根AGENTS、两阶段字符截断、缓存签名 |
| [Coding上下文](../../applications/coding/context_state.py) `build_coding_context_view/_compact_events/_apply_user_objective_change` | 状态投影、软预算基线、最新目标选择、摘要未继承上一代 |
| [快照压缩](../../runtime/context/snapshots.py) `EventCompactor/SnapshotValidator/ContextSnapshotManager` | normal/repair/chunk/fallback、锚点校验、辅助模型无主gate、快照恢复 |
| [动态预算](../../runtime/context/dynamic_budget.py) 与 [token估算](../../runtime/token_estimator.py) | W/S/T/O/M公式、完整组选择、启发式计数与双重安全阈值 |
| [主推理循环](../../runtime/execution/reasoning_loop.py) `_reasoning_step` | sanitizer、紧急裁剪、hard guard、runtime_retry/反思入口 |
| [工具hooks](../../tools/hooks.py) 与 [长内容](../../runtime/context/long_content.py) | 工具全文存储、外置阈值、首段替换、失败回灌风险 |
| [任务状态](../../applications/coding/task_state.py) 与 [状态更新](../../applications/coding/state_updates.py) | 已有丰富结构、已知证据ID校验、确定性提取器未发现生产挂接 |
| [工具处理器](../../tools/handlers.py) | 按需代码片段/符号图、工具cache、显式state patch、子任务返回 |
| [子任务runner](../../agents/subagent/runner.py)、[parallel](../../agents/subagent/parallel.py)、[prompting](../../agents/subagent/prompting.py) | 独立session、文本budget协议、全局限制、宽松返回解析、重复完成事件记录点 |
| [应用装配](../../applications/bootstrap.py) 与 [Coding装配](../../applications/coding/runner.py) | 默认能力与真实注入不同；isolated Coding不自动继承所有retrieval/memory服务 |
| [Session](../../runtime/sessions/session.py) 与 [provider](../../models/provider.py) | 原事件/活动窗口/消息分离，最终传输转换与metadata差异 |

### 10.2 外部关键资料

所有可变文档访问日期2026-09-27；开源源码日期/commit见§5.1。下表列明资料支持的结论，完整永久链接见§5.2及本表；没有将知名度当作适配证据。

| 编号 | 资料 | 支持的结论 / 不能推出的结论 |
|---|---|---|
| S1 | [Aider history.py（固定SHA）](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/history.py) | 支持“近期尾部+旧头部摘要”；不支持所有旧证据都被保留 |
| S2 | [Aider repomap.py（固定SHA）](https://github.com/Aider-AI/aider/blob/5dc9490bb35f9729ef2c95d00a19ccd30c26339c/aider/repomap.py) | 支持符号图排序/预算二分；不支持必须用向量数据库或图排名优于本项目现状 |
| S3 | [Cline basic-compaction（固定SHA）](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/extensions/context/basic-compaction.ts) | 支持不用LLM也能确定性压缩；活动清单不能证明错误细节完整 |
| S4 | [Cline agentic-compaction（固定SHA）](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/extensions/context/agentic-compaction.ts) | 支持previousSummary+新消息承接；不保证语义真实性 |
| S5 | [Cline session-compaction（固定SHA）](https://github.com/cline/cline/blob/29896ec7fa8e2dd98805b56c2dae987d59fc9baa/sdk/packages/core/src/session/models/session-compaction.ts) | 支持来源前缀hash绑定和独立投影；hash不是事实判断器 |
| S6 | [OpenHands condenser（固定SHA）](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-sdk/openhands/sdk/context/condenser/llm_summarizing_condenser.py) | 支持软/硬触发与常规原子边界；不应把事件数当token预算 |
| S7 | [OpenHands Condensation事件（固定SHA）](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-sdk/openhands/sdk/event/condenser.py) | 支持事件ID及摘要投影；不需要本项目再造事件存储 |
| S8 | [OpenHands delegate（固定SHA）](https://github.com/OpenHands/software-agent-sdk/blob/d77ada7a030b3acaa82593d402632680361dfe42/openhands-tools/openhands/tools/delegate/impl.py) | 支持子会话隔离与最终响应汇总；不支持父返回流量自动有界 |
| S9 | [Anthropic上下文工程文章，2025-09-29](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents) | 公开披露压缩/笔记/子任务隔离；不支持反推Claude Code当前私有阈值/实现或本项目收益 |
| S10 | [Claude Prompt caching文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching) | 支持重复前缀处理复用；不能用缓存命中替代上下文预算 |
| S11 | [Transformers How caching works](https://huggingface.co/docs/transformers/main/en/cache_explanation) | 支持KV为推理中间张量复用；不是Agent任务摘要或长期记忆 |
| S12 | Aider/Cline/OpenHands LICENSE永久链接及Claude Code LICENSE，见§5.1 | 支持所选版本许可分类；不泛化到托管产品，未在本轮复制代码或做许可证法律结论 |

## 11. 尚未确认的问题

1. 实际部署provider/window/tokenizer/max_input/max_output及`.env`覆盖值；本文没有把默认128k当线上事实。
2. 真实任务通常多少工具轮、发生几次压缩、摘要/重读占多少费用；尚无统一基线，所以ROI与参数最优性均待验证。
3. 生产数据中有多少关键约束/失败已由模型显式写入TaskState；缺少自动挂接不等于所有任务都丢信息。
4. 网关中途输入的准确路由：创建新任务、恢复原task、还是取消旧任务；决定R2问句替换的系统触发面。本次只复现helper行为。
5. 真实数据库进程崩溃、并发写入、artifact GC/权限故障的恢复性质；已跑内存测试不能替代这些验证。
6. 当前实际使用的plugins/custom contributors是否额外引入未预算prompt；本文覆盖已找到的核心入口，不对动态第三方代码做无证据保证。
7. 工具缓存对所有外部编辑、bash/子进程修改的失效覆盖；本文只确认key无内容版本和write/edit主动清理，不宣称所有写路径均失效失败。
8. 运行日志是否存在真实目标漂移、幻觉摘要或窗口拒绝；本轮未读取用户私有会话/日志，不能给出线上发生率。
9. 当前provider是否接受chat message中附加metadata、如何计特殊消息开销；Responses丢metadata与chat保留字典的静态差异已确认，具体服务兼容性未测。
10. 需要支持的任务范围是否包含多仓库、多模态附件和强嵌套规则；未确认前不引入相应复杂索引或多模态估算设施。

## 12. 建议交汇总任务裁决的争议

| 争议 | 本任务建议 | 裁决所需证据 |
|---|---|---|
| 先改摘要还是先换大窗口模型 | 先修多代连续性/origin/预算闭环；大窗口只推迟失效 | 现有超窗率、摘要失败及长任务成功率，不能只看输入价格 |
| 是否将状态纯数据从system移至非指令通道 | 保系统中的来源/使用规则，数据采用明确低信任块；具体角色按provider能力测试 | 指令遵循、工具协议、目标保留的兼容性回归；改role不自动等于防注入 |
| 是否立即全量接入DeterministicEventExtractor | 先接错误/测试/修改事实的小子集，同时做有界投影 | state体积、字段质量、事件吞吐；避免从“没采全”变成“全量塞prompt” |
| 开启向量检索/长期记忆是否能救上下文 | 先保证原任务状态与refs，记忆只作有预算候选 | 按需rg/工件检索遗漏率；与长期记忆任务共同确定权限/冲突接口 |
| 并行最多8路是否应保留 | 保配置上限，默认小并行且由父返回预算限制 | 全链路token与wall time，不只子任务完成速度 |
| typed claims是否过重 | 最小只实现目标/约束/待办/失败与关键refs；其余按需增加 | 连续压缩反例是否仍失败；短任务不要付出完整审计图成本 |
| 20% token节省/10%延迟门槛是否合适 | 先作为试点门槛，硬约束与任务成功率优先 | 相同窗口/provider/任务集的真实基线和不确定性；本轮没有测出这些收益 |
| 是否建立统一ModelCallGateway | 在现有provider/执行边界形成一个可复用预算入口即可，不必新平台 | 架构任务对调用所有权、fallback与兼容性的判断，确保辅助模型无遗漏 |

本报告提供可追踪的现状和最小设计边界；后续实施应以§8的反例和完整成本口径验收，不能以字段增加、摘要更流畅或采用知名框架作为完成标准。
