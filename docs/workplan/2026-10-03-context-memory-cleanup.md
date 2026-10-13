# 上下文缓存与记忆框架精简验收报告

本轮已实现上下文组装合并、动态内容后置、真实指令保护、检索生命周期修复、缓存用量观测及死代码清理。相对本轮开始时的工作区，生产源码及配置 23 个文件增加 285 行、删除 548 行，净减少 263 行；测试 14 个文件净增加 776 行。3 个子 Agent 分别从删除风险、记忆一致性和实际模型请求反驳设计，主 Agent 合并并复核修复。

**验收范围是本轮独立冻结的精简版本：1014 项测试及 2 项子测试通过。当前共享工作区另有后续压缩、证据版本和恢复工具修改；这些修改保留，但不包含在这次全量通过结论中。** 没有提交、推送、部署、迁移真实数据库或调用真实模型。

## 结论与架构取舍

现有设计值得保留的是 TaskState、不可变事件、ContextSnapshot、artifact 和长期记忆各自承担不同事实的方向。问题在于这些所有者的投影、预算、恢复和生存期没有完全对齐；继续增加一套 MemoryManager 或 ContextManager 会扩大协调成本。

| 维度 | 通用 Agent | Coding Agent | 本轮决定 |
|---|---|---|---|
| 当前任务 | 当前真实请求与必要历史通常足够 | 还需要任务约束、修改与验证状态 | 保留同一组装器，历史选择政策不同；TaskState 不强加给所有聊天 |
| 历史价值 | 主题连续性、用户偏好、近期观察 | 精确路径、符号、错误、补丁、测试结果及来源版本 | 用户原指令不能被字符裁剪或运行包装替代；工具结果仍可压缩 |
| 动态状态 | 检索与运行事件变化较多 | TaskState 版本及证据变化更频繁 | 动态内容后置，保护前面的共同轨迹 |
| 记忆 | 跨任务用户事实可检索复用 | 只有经过验证且适合跨任务复用的结论才晋升 | 保留现有 command service 与数据库权威，不把暂时假设写成长期事实 |
| 压缩 | 连贯摘要在部分任务中可能足够 | 摘要不能代替可回读的精确工程证据 | 本轮修预算与布局；多代压缩、恢复与证据有效期须独立验收 |

以上任务差异是根据本项目调用链作出的设计判断，不是所有 Agent 的统一最优政策。对于 Coding，建议后续比较“旧观察遮蔽 + 有界任务状态 + 精确证据回读”与递归摘要，使用同一模型、预算和任务验收；不能仅凭 prompt 更短判定策略更优。

架构合同仍维护在原位置：[上下文与 TaskState](../architecture/TASK_STATE_CONTEXT_ARCHITECTURE.md)、[长期记忆](../architecture/MEMORY_ARCHITECTURE.md)、[系统架构](../architecture/SYSTEM_ARCHITECTURE.md)。本报告记录变更及证据，不建立第二份运行时设计权威。既有 [上下文调研](../agent-design-review/02-context.md) 和 [记忆调研](../agent-design-review/03-memory.md) 是历史审查，不能把其中旧问题清单全部当作当前实现。

## KV cache 设计与实际边界

OpenAI 与 Anthropic 的缓存文档都以可复用前缀为关键约束；工具定义、指令或早期输入变化会影响可复用范围。Manus 的工程总结也强调确定性序列化、避免前部时间戳变动等实践。基于这些资料，本项目应减少真实需要变化的状态之前的无谓变化，而不是冻结应更新的证据或权限。[OpenAI 文档](https://developers.openai.com/api/docs/guides/prompt-caching)、[Anthropic 文档](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)、[Manus 工程总结](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)，核对日期 2026-10-03。

本轮共享布局为：

```text
稳定 system 与项目指令
→ 已选择历史
→ 当前真实请求及完整工具轨迹
→ 当前 TaskState
→ 附件、产物访问状态、显式贡献
→ 检索证据及运行观察
```

原来的实时状态、检索命中不再出现在近期轨迹前。每次构建仍取当前状态；动态尾部会替换，因此不能声称请求严格 append-only。真实轨迹发生裁剪、旧工具结果替换、快照换代、项目指令更新或工具撤权时，相关前缀仍可能失效。

本地 PromptAssetsService 的文件/文本复用与服务商 KV cache 是两件事。报告已加入 `prefix_cache_scope=local_prompt_assets`，布局标识为 `conversation_then_live_context`。不添加全局可变上下文缓存，也不为维持缓存而保留撤权工具。稳定快照摘要将来可独立作为 generation 级前缀，但要先从实时 TaskState 分离，验证新用户修订优先级；本轮没有引入第二份状态。

新增 `cached_input_tokens` 解析 Chat 与 Responses usage。缺失、非法、负数、布尔值或超过已知输入/总用量的值记为未知；明确的零保留。Trace 记录主推理调用小计与未知调用数，范围明确为 `trace_main_calls`。缓存 token 仍占执行预算；辅助模型、cache write、真实费用和端到端延迟没有因此自动获得完整计量。

独立测试捕获实际 SDK 的输入转换，离线结果如下：

| 模式与接口 | 规范化输入 JSON 公共前缀字节 修改前 | 修改后 | 完整公共输入项 修改前到修改后 |
|---|---:|---:|---:|
| 通用 Chat | 710 | 1074 | 1 → 4 |
| 通用 Responses | 710 | 880 | 1 → 4 |
| Coding Chat | 984 | 1315 | 1 → 4 |
| Coding Responses | 984 | 1121 | 1 → 4 |

这些是离线结构指标，不是 tokenizer 输出、服务商实际序列化、KV 命中率或费用收益。真实收益需要同时记录缓存读写、输入输出 token、延迟、费用和任务成功率，并区分冷缓存与暖缓存；本轮未做付费实验。证据：[修改前](evidence/2026-10-03-context-memory-cleanup/cache-prefix-before.txt)、[修改后](evidence/2026-10-03-context-memory-cleanup/cache-prefix-after.txt)。

## 已落实的精简与正确性修复

### 统一模型输入与上下文组装

Session 不再维护无人消费的 `prompt_messages` 副本；唯一待发送视图是 ContextBundle。删除独立 Coding builder 的重复组装与报告代码，保留 Coding 的历史政策、上下文指标和检索 token 统计。BuildState 删除 10 个可从已有 BudgetedText 推导或无人消费的字段。

TaskState 的 objective 与 constraints 在 protected-task 中投影一次，其余状态 JSON 不再重复这两项。投影对递归副本操作，不改变持久 envelope；这只描述渲染操作，不能扩张成“所有上下文构建都不写状态”。

检索证据的正文增加来源与指令边界，明确其为数据而非新增授权。Chat/Responses 会丢弃部分内部 metadata，仅给 metadata 打标签不足以保留模型可见边界。

### 真实指令与工具事务不能被优化丢失

Coding 入口分开保存真实用户请求与 runtime workspace/handoff 包装，避免包装被误认成最新用户。当前轮字符裁剪丢失真实用户消息时，回退至保留用户原文的工具压缩视图，再由最终 token gate 判断是否可发送。

紧急 reducer 按真实用户请求分组，同轮 runtime wrapper 随该请求保留。即使剩余预算为零，也不能输出丢失请求的 system-only 输入；不可容纳时交由最终 gate 明确拒绝。消息按原始出现位置恢复，避免两个相同字典被映射到同一旧位置。工具协议清理产生的合成 user-role 提示标明 runtime 来源。

独立审查还补出了“当前输入只有继续，任务依据在同轮 wrapper”反例，因此 wrapper 不能随意丢弃。此处保护的是当前请求完整性，不意味着把所有历史用户原文永久提升为不可裁剪 system 指令。

### 预算避免遗漏与重复扣除

Coding 选择近期窗口前预留外部状态、检索证据及其他固定消息；最终调用仍检查完整请求。软压缩只比较其负责的 state+tail 与扣除固定预留后的可用量，避免把固定部分再次算入触发值。

默认压缩比例不是经过真实 Coding 任务证明的最优参数；通用字符政策与 Coding token/事件政策仍有差异，本轮没有以改常数冒充压缩算法改进。

### 自动知识观察与显式检索分开管理

原来自动安全知识首步注入后被 `security_knowledge_used` 抑制，后续推理失去已选证据。现在 RunExecutionState 保存 run/session/request/service 范围内的自动观察，每一步持续注入；新 run 或请求等边界变化刷新。关闭检索不会被缓存绕过，异常不被缓存为正常结果。返回的空结果或错误观察在本轮保持一致。

这也避免每步重复调用付费路由分类器。语义用户记忆与 episodic history 不进入该快照，仍查询当前服务；显式安全检索绕过结果缓存，可取得新观察。

Bootstrap 将已有知识 index 注入插件，避免自动提示和工具各造客户端/embedding。共享实例会改变结果缓存语义，独立反例证明缺少 `use_cache=False` 会拿到旧结果，因此显式工具保留 fresh 语义。没有增加跨租户全局单例。

### 索引不可用不能伪装成写入成功

NullSemanticMemoryIndex 原来返回空搜索或无声接受写入，造成“无记忆”和“索引不可用”混淆，还可能让 outbox 被错误标记完成。现在显式报告不可用，复用现有 DB lexical fallback、degraded、同步 retry 和 rebuild failed 路径。

关闭全部记忆时仍不构造组件；仅关闭向量索引时，已启用的 DB 记忆仍可用。已有误标 completed 的旧记录不会自动重排，本轮也未运行真实 rebuild。没有为此引入新的恢复管理层。

### 删除没有生产消费者的旧结构

| 删除项 | 取代它或仍保留的实际路径 |
|---|---|
| Session prompt_messages 字段及 set/clear 方法 | ContextBundle |
| token_estimator.emergency_trim 及 4 个私有辅助函数 | 当前 dynamic_budget reducer；迁移相关断言至真实入口 |
| retrieved_history 预算别名与示例环境变量 | episodic_history 当前预算 |
| memory_vector_scope_for_session | 真实使用的历史 scope 仍保留 |
| 无人读取的 history_scope_resolver 参数与接线 | EpisodicBoundary.from_session |
| Markdown 全文件 is_duplicate_memory 旧入口 | 现有语义去重；旧格式解析留在迁移路径 |
| builder 旧包装函数及 instruction cache 别名 | PromptAssetsService 实际缓存 |

删除判断依据生产调用链、测试消费者与替代路径，不依据文件名含 legacy 就一律删除。仍有消费者的旧文件 importer、只读 Web adapter、TaskState 输入迁移、结果存储后端和历史索引保留。退役前需要迁移清单及引用存活合同，详见记忆架构文档。

## 对抗审查如何改变实现

| 审查方向 | 反驳或反例 | 已采取的修正 |
|---|---|---|
| Runtime 删除与状态连续性 | 旧函数仍有测试；只留当前“继续”可能丢任务依据 | 测试迁移到真实 reducer，按真实用户分组并保留关联 wrapper |
| Memory 与检索语义 | 共享 index 会沿用旧缓存；Null 写入会假成功 | 显式检索 bypass cache；Null 触发已有失败/重试/降级路径 |
| 实际模型请求与缓存 | 本地缓存计数不等于 KV；Responses 丢 metadata；重复消息映射错位 | cache scope 分开、证据正文边界、按 occurrence 选择、真实 SDK 输入测试 |
| 主线程交叉复核 | 合并分支时可能丢 Coding 统计；usage 的零被 or 覆盖 | 恢复既有指标、使用 None 判断、校验 cached token 上界 |

一次成组删除最初被自动审批拒绝，理由是删除范围较广且测试仍引用相关入口。随后先形成独立候选补丁、调用点证据、替代测试和回退基线，再重新提交精确补丁，获批准后应用；没有绕过拒绝。

关键反例证据：

- 布局测试修改前 10 failed / 2 passed，修改后 12 passed：[前](evidence/2026-10-03-context-memory-cleanup/cache-layout-before.txt)、[后](evidence/2026-10-03-context-memory-cleanup/cache-layout-after.txt)。
- 索引不可用测试修改前 12 failed / 14 passed，修改后 26 passed：[前](evidence/2026-10-03-context-memory-cleanup/null-index-before.txt)、[后](evidence/2026-10-03-context-memory-cleanup/null-index-after.txt)。
- 自动安全观察生命周期：[前](evidence/2026-10-03-context-memory-cleanup/security-lifetime-before.txt)、[后](evidence/2026-10-03-context-memory-cleanup/security-lifetime-after.txt)。
- 共享 RAG 的陈旧结果：[反例](evidence/2026-10-03-context-memory-cleanup/rag-freshness-counterexample.txt)、[修复](evidence/2026-10-03-context-memory-cleanup/rag-after.txt)。
- 原请求 handoff：[前](evidence/2026-10-03-context-memory-cleanup/handoff-before.jsonl)、[后](evidence/2026-10-03-context-memory-cleanup/handoff-after.jsonl)。

阶段定向测试包含重叠，不相加为全量测试数；测试数也不等于独立缺陷数量。

## 最终验证与源码身份

- HEAD 为 `5bc28e8d883a6ab4daf7e8d734ca3a1bcfbe24a2`，但工作区包含既有未提交修改，HEAD 不能唯一代表验收源。
- 独立冻结源码聚合 SHA-256 为 `f2c070982f1fc3432a72453524317bceab36e4397e128ac2d99ed74b3a6b44fc`。[源码清单](evidence/2026-10-03-context-memory-cleanup/source-manifest.json)记录 811 个文件，本轮独立变化文件 37 个。
- 最终 `.venv` + 全新 PostgreSQL 16：**1014 passed / 0 failed / 0 skipped，另有 2 subtests passed**，pytest 用时 22.43 秒；17 项关键路径定向也通过。[完整日志](evidence/2026-10-03-context-memory-cleanup/final-tests.txt)、[JUnit](evidence/2026-10-03-context-memory-cleanup/final-tests.xml)、[验证回执](evidence/2026-10-03-context-memory-cleanup/final-validation.json)。
- 使用剔除真实 .env 的源码副本、私有 Unix socket 数据库和 Python 网络/数据库 guard；没有访问既有数据库或下载模型。此隔离不是 OS 沙箱。[环境](evidence/2026-10-03-context-memory-cleanup/final-environment.json)、[guard 检查](evidence/2026-10-03-context-memory-cleanup/final-guard.txt)。
- 临时 PostgreSQL 已正常停止，socket、pidfile 均不存在。[清理回执](evidence/2026-10-03-context-memory-cleanup/final-cleanup.json)。
- 811 个冻结文件哈希和 37 个变更文件身份复核通过，变更 Python 文件 AST 解析通过。完整性记录单独列出当前工作区差异，未把当前工作区标记为已通过。[完整性记录](evidence/2026-10-03-context-memory-cleanup/final-integrity.json)。
- 相对本轮开始基线的 [精确补丁](evidence/2026-10-03-context-memory-cleanup/owned-changes.patch)、[文件级统计](evidence/2026-10-03-context-memory-cleanup/owned-changes.json) 与 [汇总](evidence/2026-10-03-context-memory-cleanup/change-statistics.json)均归档；统计不包含文档与并行修改。

冻结源保存在 `/tmp/taleclaw-cache-final-source-723kaop2/repo`；构造方式为最后一次本轮 1009 项全量通过的源码清单，加上本轮最终 6 个文件的小修复，排除后续并行实现；随后再次完整运行得到 1014。原始验证运行保存在 `/tmp/taleclaw-db-final-usb1jm_5`。临时路径不是长期源码仓库；持久记录用于审计，不能在基线消失后仅凭哈希还原文件。

## 共享工作区的后续改动与待验收点

最终复制工作区时出现新的 builder import 与实现尚未同时就绪的导入失败。检查确认，工作区在同时加入多代压缩、历史原请求保留、文件证据版本和 event/snapshot 恢复等修改。所有这些修改均保留；不能仅凭时间戳推断作者。

2026-10-03 12:56:59 UTC 的完整性记录显示，工作区源码与本轮独立冻结源有 23 个文件差异，其中 5 个也是本轮修改过的文件。此后独立复核仍发现文件继续变化；23 不是当前状态的固定计数。因此 1014 通过不是当前合并工作区的通过证明，也不能覆盖另一批新测试。另一路实现一旦冻结，需要重新跑全套并复核以下合同：

1. **恢复权限**：子 Agent 新增 retrieve_tool_result 后，旧 artifact/file/PostgreSQL 后端是否同样检查会话和调用者边界。新 event/snapshot reader 的限制不能替代旧后端授权。
2. **重复读取**：ToolLoopGuard 对恢复工具的豁免，应由完整的当前可见性检查接管；需覆盖压缩前重复读、压缩后允许重读及持久化恢复。
3. **历史指令权威和预算**：保留历史原文不能把已被修订的旧要求重新升级成当前 system 指令；长期不可裁剪原文也可能挤占全部预算。
4. **证据失效范围**：已观察文件 hash 能检查部分变化，不能据此保证 bash 或外部进程修改后所有旧验证结果都失效。
5. **完整状态路径**：只读 rendering 与会复核、更新 TaskState 的 view 构建要明确区分；多代压缩和恢复工具必须在同一个冻结版本验证。

这些是代码审阅发现的验收风险，其中权限与循环缺口未在本轮通过跨后端对抗实验确认为可利用漏洞；不把静态风险写成已经发生的生产事件。当前整体仍不应宣称“框架已彻底完成清理”。本轮完成的是有明确源码身份与测试证据的缓存、输入连续性和记忆边界精简。
