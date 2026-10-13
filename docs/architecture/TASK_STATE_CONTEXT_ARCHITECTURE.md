# TaskState And ContextSnapshot Architecture

Task progress and context compaction are separate facts with separate owners.

## Ownership

| Concern | Owner | Persistence |
|---|---|---|
| Objective, constraints, progress, pending actions, blockers, completion basis | `TaskStateCore` | current `task_state` session envelope |
| Coding-specific rendering details | coding TaskState extension | same current envelope |
| Immutable conversation and tool facts | Session event log | session repository |
| Compacted summary and covered event boundary | `ContextSnapshot` | snapshot repository |
| Per-run counters, cancellation, recovery and stop decision | `RunExecutionState` | run/trace lifetime |
| Large content body | Artifact store | content-addressed artifact plus references |

`applications/coding/context_state.py` builds a coding prompt view from these
owners. Rendering does not persist a second objective, plan, blocker list, or
progress ledger. Evidence checks during view construction may update TaskState;
read-only rendering must not be confused with the entire build lifecycle. A
snapshot may summarize TaskState facts but cannot itself mutate them.

## Run Lifecycle

TaskState is optional. Coding supplies `TaskStateRunObserver` through
`RuntimeExtensions.run_observers`. `ReasoningLoop` emits a generic
`StopDecision`; the observer maps completed, cancelled, failed, or blocked
decisions into TaskState and returns the resulting version. Chat runs without
that observer and do not create TaskState.

Tool handlers update TaskState through validated optimistic patches. Terminal
states cannot be reopened, and completion requires evidence/basis and no pending
actions or blockers. A supplied stale base version is rejected; the version is
optional and this reducer check is not a cross-process database compare-and-swap.

## Snapshot Lifecycle

```text
REQUESTED / GENERATING
        -> PREPARED
        -> ACTIVE
        -> archive boundary advanced

        -> FAILED_RETRYABLE / FAILED_FINAL
```

Only an active snapshot is eligible to advance the event archive boundary.
Snapshot identity includes the covered event range and source TaskState version,
so retrying the same compaction is idempotent. Failure preserves the prior
active snapshot and original events. Deterministic fallback retains objective,
constraints, pending work, blockers, evidence/artifact references, and retained
observation previews when model summarization is invalid or unavailable.

## Legacy Input

Old `working_memory`, `coding_context_state`, and flat TaskState payloads are
handled only by `runtime/task_state/legacy.py`. The adapter converts them to the
current envelope, checkpoints the migration, and removes old keys atomically.
They are never rendered or updated as current runtime state. See
`docs/migrations/TASK_STATE_CONTEXT_MIGRATION.md` for the removal condition.

## Runtime Configuration

TaskState and ContextSnapshot are architecture contracts, not parallel feature
flags. Current tunable controls are limited to artifact offload, dynamic prompt
budget ratios, context section budgets, and provider/model limits. Removed
WorkingMemory, CodingContextState, TaskState-context, and semantic-compaction
flags have no compatibility aliases.


## 当前上下文与缓存合同（2026-10-03）

> 验收范围：以下新增合同以本轮独立冻结版本为依据。共享工作区同期另有压缩、证据失效与恢复工具修改，尚未包含在本轮 1014 项通过结论中；具体差异见[验收报告](../workplan/2026-10-03-context-memory-cleanup.md)。

模型输入只存在于 `ContextBundle.messages`，不在 Session 中再保存一套
`prompt_messages`。通用模式与 Coding 使用同一条组装、报告、压力观察路径；
两者仍有不同的历史选择策略。`BuildState` 只保留报告需要的值，raw/rendered
文本从现有 `BudgetedText` 读取。

正常布局：

```text
稳定 system / 项目指令
→ 本轮已选择的历史
→ 当前交互及完整工具调用/结果
→ 最新 TaskState（如该模式提供）
→ 附件状态、产物访问状态、显式贡献块
→ 带正文来源边界的检索证据与运行事件
```

会改变的任务版本、焦点、检索命中不再插在近期轨迹前面。状态与证据仍按当前
构建更新；末尾旧动态块被替换，因此只保证可复用未改变的真实轨迹前缀，不能
声称整个上次请求都是下次请求的前缀。历史裁剪、旧工具结果替换、压缩、项目
指令更新、模型路由变化和工具权限变化仍会使相应前缀失效。

`ContextPrefix` 与 `PromptAssetsService` 是本地组装/文件读取复用；报告中的
`prefix_cache_scope=local_prompt_assets` 明确区分服务商 KV/prompt cache。
没有新增跨会话的状态或证据缓存，也不为提高缓存读数保留已撤权工具。

### 状态、证据与真实指令

- Coding 入口分别记录 `origin=user` 的原请求与 `origin=runtime` 的运行说明、
  workspace/handoff。包装块不能替代真实请求来源。
- 当前 objective/constraints 在 protected-task 中投影一次；其余状态投影移除
  这两个重复字段。投影操作使用递归副本，不改写持久 envelope。
- 检索证据继续使用原传输角色。正文明确标记其为宿主提供的数据，不授权新动作；
  不能只依赖会被 Chat/Responses 转换丢弃的 metadata。
- 紧急裁剪按真实用户请求分组。相关 runtime wrapper 属于该请求，防止只留下
  “继续”却丢掉所指任务。选中消息按原始出现位置输出，同文消息不互相替换。
- 没有剩余预算时仍保留必需组，让最终 gate 明确拒绝；不能返回一个可容纳但
  丢失用户指令的 system-only 请求。
- 活动轮的字符预算若丢了真实用户消息，回退到保留原指令的工具压缩视图。
  字符阈值不能凌驾于用户指令保护。

### 预算与压缩

Coding 选择近期窗口前，预留 system、schema、输出、外部运行状态和检索证据。
软阈值只比较所管理的 state+tail，避免重复计入已扣除的固定内容。上游分项
仍可使用估算器；最终模型调用依据完整输入及选定 provider 再检查。

当前通用 history/activity 仍使用字符政策，Coding 使用事件快照政策。全局
`CONTEXT_BUDGET_CHARS` 是报告阈值，硬调用边界由 token gate 执行。70%/45%
是默认触发/目标参数，没有真实任务对照证明其最优。

### 自动知识检索的有效期

`RunExecutionState.security_context` 只保存本次 run、session、request、service
对应的自动安全检索观察（三元组含正文、路由决定和命中）。每一步持续注入，
避免首步之后丢证据，也避免每步再次调用付费路由分类器。新 run reset 清空；
请求、会话或服务变化重新获取；显式关闭不允许缓存绕过。空结果及返回的错误
观察也属于本轮快照，服务真正抛异常时不保存。

语义记忆和 episodic history 不使用此快照，每次构建仍查询当前服务。需要运行中
新知识时使用显式安全检索，它绕过结果缓存。自动观察的语义不是永远最新事实。

### KV 指标与验收

Provider 解析 Chat `prompt_tokens_details.cached_tokens` 与 Responses
`input_tokens_details.cached_tokens`，投影为 `cached_input_tokens`。缺失、非法、
超过已知 input 或 total 的值均为 unknown（None）；明确的零保留。

Trace 只保留一套累计读数、`unknown_cache_usage_calls` 和
`cache_usage_scope=trace_main_calls`。这是已报告的主循环小计，失败/fallback
缺项按未知记录，不能与覆盖所有模型用途的 root budget 混用。缓存 token 仍计入
执行 token 预算；这里不推算费用，也没有假定所有兼容服务商返回相同字段。

验收分三层：消息/工具协议与指令正确性；实际 SDK 输入的公共前缀结构；真实
服务商的缓存读写、总 token、延迟、费用和任务成功率。离线公共 JSON 字节不是
模型 token，也不是缓存命中率。新布局已有 Chat/Responses 的独立离线回归；
真实缓存费用收益尚未运行模型实验。

设计依据：[OpenAI prompt caching](https://developers.openai.com/api/docs/guides/prompt-caching)、
[Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)、
[Manus context engineering](https://manus.im/blog/Context-Engineering-for-AI-Agents-Lessons-from-Building-Manus)，
均核对于 2026-10-03。不同模型/服务商的阈值和 TTL 不作为本框架统一常量。

### 明确保留的限制与后续实验

本轮独立验收版本没有改写持久快照协议，其限制包括受保护观察累积、模型 notes
跨代继承不足、跨快照 handoff 有损及证据恢复接口不统一。共享工作区的后续修改
已开始处理其中部分问题，但不属于本轮验收范围。不能据缓存布局修复宣称长期推理无损。下一步应在相同预算/模型下对比当前策略、观察遮蔽、结构化工作
记忆和混合策略，并测约束保留、精确信息恢复、陈旧证据使用和任务验收结果。

快照摘要可以成为按 generation 稳定的前缀，但必须先从实时状态中分离，并证明
新用户修订立即生效、旧摘要不夺取权威；当前实现仍把整个状态/摘要块置于尾部。
不为增加缓存层而引入第二份可变任务状态。
