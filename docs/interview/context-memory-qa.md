# 上下文与记忆管理逐题答辩稿

本文针对“上下文与记忆管理追问”逐题作答，答案基于当前仓库实现。重点代码入口：

- `runtime/context.py`：把 system prompt、instructions、history、memory、working memory、retrieved history、security RAG、task runtime events 组装成模型上下文。
- `runtime/context_budget.py`：section 级字符预算与裁剪策略。
- `runtime/context_history.py`：历史消息和 active turn 的消息级压缩。
- `runtime/token_estimator.py`：provider-aware token 估算、safe context limit、emergency trim。
- `runtime/working_memory.py`：coding task 的可续做工作记忆。
- `memory/store.py`：长期记忆、候选记忆、history、recent context 的本地存储。
- `memory/lifecycle.py`：每轮结束后的记忆生命周期。
- `memory/processor.py`、`memory/candidates.py`：候选记忆选择、证据累计、晋升。

## 0. 总括回答

我的上下文管理不是简单把所有信息拼进 prompt，而是把不同来源拆成逻辑 section，每个 section 有预算、裁剪策略和 trace report。模型最终看到的还是标准 chat messages，但 Runtime 内部知道每个 section 原始长度、渲染长度、是否截断、为什么截断。

记忆系统也分层：长期稳定记忆存在 `MEMORY.md`，当前任务状态存在 session metadata 的 working memory，历史 turn 会进入 `HISTORY.md`、recent context 和可选向量索引，候选记忆先放在 `PENDING.json`，经过相似历史证据和置信度累计后才晋升为稳定长期记忆。

面试可以用这句话开场：

> 我把上下文当成一个可观测的资源调度问题处理，而不是 prompt 拼接问题。system、history、active turn、memory、working memory、RAG 都是 section，有不同预算和优先级；每次裁剪都会进入 trace，所以我能复盘模型这一步到底看到了什么。

## 1. 你说把系统提示、历史消息、当前请求、长期记忆、工作记忆、检索结果拆成 section 管理，section 的优先级怎么定？

优先级不是靠一个全局排序数组，而是由三个机制共同决定：

1. 传输位置：system prompt 最前，conversation history 其次，context frame 再次，active turn 最后。active turn 离模型最近，保真度最高。
2. context frame 内部顺序：`security_knowledge -> working_memory -> task_runtime_events -> retrieved_history -> memory`。这表示安全 RAG 证据和当前任务状态优先于泛化长期记忆。
3. section budget：每个 section 有独立预算和裁剪策略，例如 active turn 用 `latest_tool_call`，conversation history 用 `summary_middle`，memory/retrieved history/security knowledge 用 `head_tail`。

最终模型 messages 顺序大致是：

```text
system prompt
budgeted conversation history
context frame: security_knowledge / working_memory / task events / retrieved_history / memory
active turn
```

代码依据：

- `runtime/context.py:358-395`：构造 context frame 并拼接 active turn。
- `runtime/context.py:669-710`：context frame 内 section 顺序和 priority 注释。
- `runtime/context_budget.py:58-157`：各 section 的默认预算和策略。

## 2. 当上下文超预算时，最先裁剪什么？最后保护什么？

当前是两层治理：

第一层是 section budget。每个 section 先按自己的策略裁剪：

- instruction、skill catalog：保留头部。
- memory、retrieved history、security knowledge：`head_tail`，保留开头和结尾。
- task runtime events：`tail`，保留最新事件。
- conversation history：`summary_middle`，保留开头若干 turn 和最近若干 turn，中间压成 summary。
- active turn：先压缩旧工具结果，再保护最近工具链。

第二层是模型调用前 token gate。如果估算 token 仍超过 provider safe limit，`ReasoningLoop` 会执行 `emergency_trim()`，保留 system message、前两个 conversation groups 和最近 groups，最后才对大文本做截断。

最后保护的是：

- system prompt 和关键 instructions。
- 当前用户请求。
- active turn 中最近一次 assistant tool_call 及对应 tool result，避免 provider 协议错误。
- coding 场景下的 working memory / coding context state。
- read_file、repo_map、code_outline、git_diff 等对继续 coding 推理关键的工具结果。

代码依据：

- `runtime/context_budget.py:66-155`：默认 section 预算。
- `runtime/context_history.py:131-241`：active turn 预算与旧工具结果压缩。
- `runtime/token_estimator.py:133-177`：emergency trim。
- `runtime/reasoning_loop.py:501-529`：模型调用前 token gate。

## 3. 历史消息是直接截断、摘要压缩，还是按重要性选择？

历史消息不是简单从头截断。

`conversation_history` 默认策略是 `summary_middle`：

- 保留开头 `CONTEXT_HISTORY_KEEP_HEAD_TURNS`，默认 3 个 turn。
- 保留结尾 `CONTEXT_HISTORY_KEEP_TAIL_TURNS`，默认 6 个 turn。
- 中间 turn 变成一条 synthetic summary message，记录压缩 turn 数、用户问题、assistant 摘要、工具调用名、tool result 预览。
- 如果压缩后仍超预算，再从尾部按 group 做 trim，避免破坏 assistant tool_calls 和 tool messages 的配对。

这不是 embedding importance selection，而是“结构保真优先”的消息级压缩。因为聊天历史里最怕裁掉 tool_call 对应的 tool result，导致 provider 报格式错误。

代码依据：

- `runtime/context_budget.py:120-131`：conversation history 默认 `summary_middle`。
- `runtime/context_history.py:43-128`：conversation history budget 主流程。
- `runtime/context_history.py:258-290`：head/middle/tail 分组策略。
- `runtime/context_history.py:320-360`：summary message 生成。

## 4. Working Memory 和 Long-term Memory 的区别是什么？分别存什么？

区别是生命周期和用途。

Working Memory 是任务内、可续做的运行状态，存在 session metadata 里，key 是 `working_memory`。主要用于 coding task：

- `objective`
- `completed_units`
- `pending_units`
- `archived_findings`
- `step_checkpoints`
- `observed_calls`
- `last_checkpoint_step`
- `status`: running / suspended / completed

Long-term Memory 是跨 turn、跨会话可复用的稳定事实，存在用户 memory root 下的 Markdown/JSON 文件：

- `MEMORY.md`：稳定记忆。
- `SELF.md`、`NOW.md`：长期自我/当前状态类信息。
- `PENDING.json` / `PENDING.md`：候选记忆。
- `HISTORY.md`：历史 turn 摘要。
- `RECENT_CONTEXT.md/json`：最近若干 turn。

简单说：Working Memory 解决“这个 coding 任务做到哪里了”；Long-term Memory 解决“用户长期偏好、项目约定、可复用事实是什么”。

代码依据：

- `runtime/working_memory.py:40-52`：WorkingMemory 字段。
- `runtime/working_memory.py:88-100`：从 session metadata 读写 working memory。
- `memory/store.py:14-26`：长期记忆文件布局。
- `memory/store.py:46-57`：prompt memory 默认包含 SELF/MEMORY/NOW。

## 5. “候选抽取、相似记忆匹配、证据次数 / 置信度累计实现长期记忆晋升”，具体流程是什么？

实际流程是：

1. 每轮结束后 `MemoryLifecycle.after_turn(session)` 取最后 user text 和 assistant text。
2. 如果用户显式说“记住/请记住/remember that”，直接写入 `MEMORY.md`，不走候选。
3. 否则进入候选流程：`MemoryProcessingDevice.process_user_description()` 用当前用户描述去历史向量索引里查相似 session turn。
4. 如果相似历史命中数低于阈值，什么都不写，避免偶发一句话污染记忆。
5. 如果命中数足够，把这条用户描述 upsert 到 `PENDING.json`，并记录相似历史证据、confidence、source_ref。
6. 如果已有相关候选被触发，会增加 evidence_count 和 confidence。
7. 每轮检查候选是否达到晋升条件：`confidence >= 0.85` 或 `evidence_count >= 3`。
8. 达到条件后，用 `CandidateMemoryExtractor` 抽取稳定记忆。如果配置了 `ModelTaskRunner`，会让 summary purpose 模型提炼；否则回退候选原文。
9. 稳定记忆写入 `MEMORY.md`，候选标记为 `promoted`。

代码依据：

- `memory/lifecycle.py:70-128`：after_turn 中显式记忆、候选处理和晋升。
- `memory/processor.py:44-113`：基于历史相似 hits 选择候选。
- `memory/lifecycle.py:352-378`：晋升条件与写入 MEMORY.md。
- `memory/processor.py:137-178`：候选稳定记忆提取器。

## 6. 记忆候选是由 LLM 抽取，还是规则抽取？如何避免把错误信息写入长期记忆？

当前要分两步说：

- 候选“是否入池”主要不是 LLM 抽取，而是基于当前用户描述 + 历史向量相似命中。只有当类似描述在历史中多次出现，才写入 `PENDING.json`。
- 候选“晋升为稳定记忆时的措辞”可以由 LLM 提炼。如果没有配置 `ModelTaskRunner`，就回退使用候选原文。

避免错误写入长期记忆主要靠几道门：

1. 显式记忆才直接写长期记忆，例如“记住我喜欢真实人物细节”。
2. 非显式记忆必须有相似历史证据，默认至少 2 个相似历史命中。
3. 候选还要累计 evidence_count 或 confidence 才晋升，默认 3 次证据或 0.85 置信度。
4. 写入 `MEMORY.md` 前会做 duplicate/semantic duplicate 检查。
5. LLM 提炼器的 system prompt 要求只提取稳定、可复用的偏好/项目约定/长期事实；没有稳定记忆则输出空字符串。

但要诚实说：当前没有完整的 contradiction detector 或自动事实失效机制，错误信息完全自动拦截还不是强保证。后续可加“纠错/否定表达识别 + memory invalidation metadata + 人工审核队列”。

代码依据：

- `memory/lifecycle.py:333-347`：显式记忆 marker。
- `memory/processor.py:73-83`：相似命中足够才 upsert candidate。
- `memory/lifecycle.py:358-360`：晋升阈值。
- `memory/store.py:59-66`、`memory/dedup.py:29-63`：重复和语义重复检查。

## 7. “证据次数”是什么意思？同一事实出现几次才晋升？有没有阈值？

`evidence_count` 是候选记忆被独立 turn 支持或再次触发的次数。

候选创建时 `evidence_count=1`。之后有两种方式增加：

- 完全相同或规范化后相同的候选再次 upsert，`evidence_count += 1`。
- 新文本和已有候选 token overlap / 关键词相关，`trigger_related()` 会给相关候选 `evidence_count += 1`。

默认晋升条件在 `MemoryLifecycle`：

- `promotion_evidence_count = 3`
- `promotion_confidence = 0.85`

也就是说，默认同一类事实累计 3 次证据，或者置信度先达到 0.85，就会尝试晋升。

代码依据：

- `memory/candidates.py:18-30`：`MemoryCandidate.evidence_count`。
- `memory/candidates.py:75-115`：upsert 创建或更新 evidence_count/confidence。
- `memory/candidates.py:117-148`：相关候选触发。
- `memory/lifecycle.py:44-68`：默认晋升阈值。
- `memory/lifecycle.py:352-378`：晋升判断。

## 8. “相似记忆匹配”用的是 embedding 相似度吗？相似但不完全一致的记忆怎么合并？

候选选择阶段用的是历史向量索引，即 embedding/vector search。`MemoryProcessingDevice._similar_history()` 调 `history_vector_index.search(query=..., scope=..., top_k=8, min_score=0.55)`，默认至少需要 2 个相似命中。

候选合并分两层：

1. 精确/规范化合并：`CandidateMemoryStore._find_exact()` 用 `normalize_memory_text()` 做规范化比较，完全一致就更新同一个 candidate。
2. 相关触发：`trigger_related()` 用 token overlap、包含关系和默认 memory keywords 判断相似但不完全一致的候选，命中后给已有 candidate 增加 evidence_count 和 confidence。

长期记忆写入 `MEMORY.md` 时还有 `is_duplicate_memory()`，会做规范化和语义 token overlap，避免同义重复写入。

代码依据：

- `memory/processor.py:118-127`：向量相似搜索。
- `memory/processor.py:129-134`：根据相似 hits 计算 confidence。
- `memory/candidates.py:160-169`：候选规范化 exact merge。
- `memory/candidates.py:117-148`：相似候选触发。
- `memory/dedup.py:29-63`：长期记忆去重和语义重复判断。

## 9. 如果用户纠正了之前的记忆，比如“我不是做 Java 的”，旧记忆怎么失效？

当前实现没有完整自动失效机制，这点面试要讲清楚。

现状能做的是：

- 用户可以显式记忆新事实，写入 `MEMORY.md`。
- `MEMORY.md` 是 Markdown 文件，人类可审计、可编辑，可以手动删除或修正旧条目。
- 后续如果新事实被多次证明，会作为新候选晋升。
- 记忆 recall 是按当前 query 召回相关片段，不是每次整包注入所有历史，因此旧记忆不一定总进入 prompt。

但如果 `MEMORY.md` 里已有“用户做 Java”，用户说“我不是做 Java 的”，当前代码不会自动把旧条目标记 revoked。更完整的方案应该加：

- contradiction detector：识别“不是/不再/改为/纠正一下”等否定纠错表达。
- memory item id 和 status：`active/revoked/superseded`。
- source_ref 和 supersedes 指针。
- recall 时过滤 revoked，并在 trace 里记录冲突解决。

这也是我会作为后续重构点承认的边界。

代码依据：

- `memory/store.py:59-66`：当前 append 是追加式 Markdown。
- `memory/store.py:177-184`：按 query recall，不是默认全量注入。
- `memory/candidates.py:150-158`：候选只有 promoted 状态，没有 revoked/superseded。

## 10. RAG 检索结果和长期记忆同时注入时，冲突怎么办？谁优先？

当前没有自动“真理仲裁器”，但上下文优先级上 RAG 尤其是安全 RAG 更靠前。

`ContextBuilder._build_context_frame()` 的顺序是：

1. `security_knowledge`
2. `working_memory`
3. `task_runtime_events`
4. `retrieved_history`
5. `memory`

对于代码安全问题，本地安全 RAG 是当前问题的证据，优先级高于长期偏好记忆。长期记忆更多用于用户偏好、项目约定、历史上下文，不应该覆盖 RAG 命中的安全事实。

如果冲突，面试回答可以说：

> 我会要求模型优先使用当前检索证据和当前用户请求；长期记忆作为背景。如果两者冲突，应该在回答里显式说明冲突并倾向当前证据。当前代码已经通过 context frame 顺序表达了优先级，但还没有做结构化冲突检测，这是可以继续增强的地方。

代码依据：

- `runtime/context.py:669-710`：context frame 内 RAG/working memory/retrieved history/memory 的顺序。
- `runtime/context.py:975+`：security RAG 自动上下文构建。
- `runtime/context.py:907-923`：长期 memory block 构建。

## 11. 怎么避免 prompt 里 section 太多导致模型注意力分散？

主要做了六件事：

1. 逻辑 section 多，但传输 message 少。最终 provider 看到的是 system、history、context frame、active turn，而不是十几个零散 messages。
2. memory 不是全量注入。当前请求存在时走 `store.recall(current_request)`，只返回相关 memory hit。
3. security RAG 自动注入每个用户 turn 只注入一次，后续需要再用 `security_rag_search` 手动查。
4. skill 只注入 catalog，不默认注入所有 skill 正文；需要时通过 `load_skill` 加载。
5. 各 section 有预算，超长会截断或摘要。
6. context report 会记录每个 section 的原始长度和渲染长度，后续可以根据 trace 调整预算。

代码依据：

- `runtime/context.py:358-395`：context frame 聚合为一个 user message。
- `runtime/context.py:907-923`：memory 按 current request recall。
- `runtime/pipeline.py:197-222`：security RAG 每 turn 只注入一次。
- `runtime/context.py:735-752`：skill catalog 只暴露描述，不塞正文。
- `runtime/context_sections.py:49-69`：context report 记录 sections 和 reductions。

## 12. 有没有做 token 预算统计？是按字符估算，还是调用 tokenizer 精确计算？

两层都有。

section budget 主要按字符数做，因为它在 `ContextBuilder` 阶段要快速、稳定、provider 无关地控制各 section 大小。默认总字符预算是 24000，各 section 有自己的 `budget_chars`。

模型调用前会做 token gate。`estimate_tokens()` 优先使用 provider 暴露的 `count_tokens()`；如果 provider 开启了 BPE tokenizer，则用 `tiktoken`；否则使用保守 fallback：ASCII/code 大约 4 chars 一个 token，CJK 接近 1.1 token/字，其它字符按 1 token 估算。

然后根据 provider 的 context window、max input tokens、output reserve 算 safe context limit。如果超限，就 `emergency_trim()`，并写 `context_emergency_trim` trace。

代码依据：

- `runtime/context_budget.py:8-9`：默认字符总预算。
- `runtime/context_budget.py:58-157`：各 section 字符预算。
- `runtime/token_estimator.py:17-40`：token 估算优先级。
- `runtime/token_estimator.py:89-130`：safe context limit 和 output token clamp。
- `runtime/reasoning_loop.py:501-549`：模型调用前 token gate 和 trace。

## 13. 如何验证“上下文管理确实提升了 Agent 成功率或降低了 token 消耗”？

我会从单测、run metrics、benchmark ablation 三层验证。

第一层是单测验证机制正确：

- `test_context_budget.py` 覆盖默认预算、旧工具结果压缩、read_file 结构压缩、traceback-aware 压缩、tool result store 可找回。
- `test_context_instructions.py` 覆盖 active turn 保留 tool call pairs、history summary middle、retrieved history 插入、section budget 保留 current request。
- `test_token_estimator.py` 覆盖 token 估算、emergency trim、tool_call pairing 保留。

第二层是 run metrics：

- `metrics.json` 里有 total tokens、context_builds、compressed context build count、truncated tool output count。
- `context_metrics.json` 里有每次 build 的 reductions、压缩前后 token、coding context state generation。
- `trace_summary` 可以看压缩后是否导致重复工具调用或失败。

第三层是 benchmark ablation：

1. 用同一批 coding/security RAG 任务跑 baseline。
2. 关闭或放宽 section budget / active turn compression / coding context state。
3. 对比 pass rate、平均 total tokens、平均 tool calls、reasoning steps、失败分类。
4. 如果开启上下文管理后 pass rate 不降或上升，同时 token、重复工具调用、context overflow 下降，就说明有效。

当前仓库已经有 coding benchmark、security RAG eval、trace metrics、context metrics 的基础。严格结论应该基于 ablation 报告，而不是单次主观体验。

代码依据：

- `tests/test_context_budget.py`：上下文预算机制测试。
- `tests/test_context_instructions.py`：消息结构和 section 插入测试。
- `tests/test_token_estimator.py`：token gate 测试。
- `runtime/trace/trace_store.py`：metrics/context metrics 聚合。
- `scripts/run_evals.py`、`scripts/run_security_rag_matrix.py`、`scripts/run_security_rag_ablations.py`：评测入口。

