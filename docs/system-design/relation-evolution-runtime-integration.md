# Relation / Evolution Proposal 运行时接入设计

## 1. 文档目的

本文说明 TaleClaw 的 Relation / Evolution Proposal 为什么暂未接入真实运行时，以及如何在不自动覆盖长期记忆、不污染跨 Session 数据、不绕过用户确认的前提下完成后续接入。

当前代码已经具备：

- `MemoryNote`、`MemoryLink` 和 `MemoryEvolutionProposal` 领域模型；
- duplicate、related、updates、contradicts、supersedes、enriches 关系分类；
- 保守的 `RelationDecider`；
- 非法 adapter 输出验证与 fallback；
- proposal 默认 pending、`auto_apply=false`；
- 不修改和不归档现有 stable memory 的测试。

当前尚不具备：proposal 持久化、审批命令、事务化 apply、candidate 到 MemoryNote 的正式 adapter，以及真实 pending write 接入。

## 2. 当前为什么没有直接接入

### 2.1 Candidate 写入结果不是结构化对象

当前 `MemoryStore.upsert_candidate()` 返回展示文本，例如：

```text
Saved candidate to PENDING.json: <candidate-id>
Updated candidate in PENDING.json: <candidate-id>
```

如果运行时通过解析字符串取得 ID，提示文本一旦变化，proposal 外键关系就可能失效。接入前应返回结构化结果：

```text
CandidateUpsertResult
├── candidate_id
├── created
├── updated
├── status
├── dedup_decision
└── audit_metadata
```

兼容层可以继续渲染旧字符串，但内部不应解析展示文本。

### 2.2 Related candidate 与 RelationDecider 的类型不同

现有 related candidate 使用 legacy `MemoryCandidate`，新 RelationDecider 使用 `MemoryNote` 和 `RelatedMemoryCandidate`。目前没有正式 adapter 定义：

- owner scope 和 scope ID；
- origin 和 source；
- timestamp、confidence、kind 和 status 映射；
- 哪些 legacy metadata 可以进入 audit；
- 缺失字段的安全默认值。

临时拼装对象可能把 task-local candidate 错误映射为 user/global memory。

### 2.3 Proposal 没有持久化真相源

当前 proposal 是不可变 value object，但没有对应 repository 或 PostgreSQL 表。若只生成 trace：

```text
生成 proposal → 写入 trace → run 结束 → proposal 无法再审批
```

这只能用于观测，不能形成真实生命周期。用户即使看见 proposal，也无法可靠地 accept、reject 或查询最新状态。

### 2.4 没有审批和应用命令

contradicts、supersedes、replace 和 archive 都属于高风险状态变化，不能由 RelationDecider 自动执行。当前缺少：

```text
propose_relation(...)
accept_evolution_proposal(...)
reject_evolution_proposal(...)
expire_evolution_proposal(...)
apply_accepted_evolution(...)
```

没有统一 command service 时，handler、后台任务或 LLM 可能绕过 policy，直接修改 memory status。

### 2.5 缺少事务边界

一次安全 apply 可能同时修改：

- proposal status；
- accepted MemoryLink；
- candidate 和 target memory status/version；
- evidence；
- outbox/index operation；
- audit event。

这些变化必须处于 PostgreSQL 事务中。否则先归档旧 memory、后写 proposal 失败时，会留下无法解释的不一致状态。

## 3. 设计原则

1. PostgreSQL 是 proposal、link 和 memory 状态的唯一真相源。
2. Qdrant 只做检索索引，不保存审批状态。
3. RelationDecider 只能 propose，不能 apply。
4. contradiction 和 supersede 默认要求用户确认。
5. 未确认前不得修改、覆盖或归档 stable memory。
6. task-local candidate 不得提升为 user/global scope。
7. LLM 输出必须经过 enum、ID、scope、confidence 和 action 白名单验证。
8. trace 只记录 ID、分数、原因、状态和 digest，不记录完整敏感内容。
9. flag 关闭时不得增加 proposal 写入或额外模型调用。
10. 所有 apply 操作必须幂等并产生 audit。

## 4. 目标运行链路

```text
Capture candidate
  ↓
Governance classification
  ├── discard / task-local / pending
  ↓
Pending candidate upsert
  ↓ structured CandidateUpsertResult
Retrieve related active memories in same allowed scope
  ↓
Adapt candidate and hits to MemoryNote
  ↓
RelationDecider.propose()
  ↓
Persist pending proposal + candidate link + audit
  ↓
User/runtime review
  ├── reject → proposal rejected; memories unchanged
  └── accept → transactional apply command
                 ├── accept link
                 ├── update memory lifecycle state
                 ├── write audit/outbox
                 └── synchronize search index
```

Related retrieval 必须限定在 allowed owners 内，不能把其他用户、workspace 或无关 task 的 memory 作为 target。

## 5. 建议数据模型

### 5.1 memory_evolution_proposals

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `proposal_id` | text/uuid PK | 稳定 proposal ID |
| `candidate_memory_id` | text | 新 candidate ID |
| `relation_type` | text | validated enum |
| `proposed_action` | text | validated enum |
| `reason` | text | 截断后的解释 |
| `confidence` | numeric | 0–1 |
| `status` | text | pending/accepted/rejected/expired/applied/failed |
| `policy_decision` | text | policy 结果 |
| `source` | text | runtime/user/adapter |
| `created_by` | text | rule/llm/runtime/user |
| `created_at` | timestamptz | 创建时间 |
| `decided_at` | timestamptz nullable | 审批时间 |
| `decided_by` | text nullable | 用户或 runtime identity |
| `applied_at` | timestamptz nullable | 应用时间 |
| `version` | integer | optimistic locking |
| `audit_metadata` | jsonb | 有长度限制的安全 metadata |

### 5.2 memory_evolution_targets

一个 proposal 可能关联多个 target，应使用关系表而不是 JSON 外键：

| 字段 | 说明 |
| --- | --- |
| `proposal_id` | proposal FK |
| `target_memory_id` | target memory FK |
| `target_version` | proposal 生成时的 memory version |
| `retrieval_score` | 检索分数 |
| `scope_match` | 生成时的 scope 验证结果 |

`target_version` 用于避免用户确认前目标已被其他操作修改。版本不匹配时，apply 必须失败并重新生成 proposal。

### 5.3 memory_links

| 字段 | 说明 |
| --- | --- |
| `link_id` | 主键 |
| `source_memory_id` | source FK |
| `target_memory_id` | target FK |
| `relation_type` | relation enum |
| `confidence` | 0–1 |
| `reason` | 截断说明 |
| `status` | candidate/accepted/rejected |
| `proposal_id` | 产生该 link 的 proposal |
| `created_by` | rule/llm/runtime/user |
| `created_at` | timestamptz |
| `audit_metadata` | bounded jsonb |

candidate link 和 accepted link 必须区分。检索或 link expansion 默认只能使用 accepted link。

## 6. Command Service

建议新增 `MemoryEvolutionCommandService`，所有状态变化只能经过该 service。

### 6.1 propose

- 验证 candidate 和 target 存在；
- 验证 owner/scope；
- 验证 target active 且版本匹配；
- 写 pending proposal、candidate link、audit/outbox；
- 不改变任何 stable memory。

### 6.2 accept / reject

`accept` 只完成审批，由同一事务内的 apply command 或可靠 outbox worker 执行变化。

`reject` 应将 proposal 和 candidate link 改为 rejected，保持 candidate/target content 与 status 不变，并记录审批 identity。

### 6.3 apply

| Action | Apply 行为 |
| --- | --- |
| `create_link` | 接受 link，不改 memory content |
| `merge_metadata` | 只合并白名单 metadata，增加 version |
| `mark_duplicate` | 标记 candidate duplicate，target 不变 |
| `request_confirmation` | 未确认前禁止 apply |
| `archive_old_after_confirmation` | 确认且版本匹配后归档 target |
| `replace_after_confirmation` | 创建新版本并 supersede target，不原地重写 content |
| `no_action` | 仅记录 audit |

## 7. Runtime 接入点

推荐接在 pending candidate 成功写入之后：

```python
upsert_result = candidate_service.upsert(...)
if flags.memory_relation_proposals_enabled and upsert_result.is_pending:
    relation_runtime.propose_for_candidate(upsert_result.candidate_id, context)
```

这样 candidate 已有稳定 ID，governance/enrichment 已完成；relation 失败也不会回滚主 candidate 流程。

必须避免：

- 从 `Saved candidate...` 文本解析 ID；
- 在 RelationDecider 中直接 update repository；
- 在 LLM adapter 中执行 accept/apply；
- 在 trace callback 中修改 memory；
- scope 不匹配时降级为 global retrieval。

## 8. Feature Flag 与灰度

完成持久化和 command service 后再增加：

```dotenv
MEMORY_RELATION_PROPOSALS_ENABLED=0
MEMORY_EVOLUTION_APPLY_ENABLED=0
MEMORY_RELATION_LLM_ADAPTER_ENABLED=0
```

建议分三阶段：

1. Shadow：只计算 metrics，不持久化，不调用 LLM。
2. Proposal：持久化 pending proposal，允许查询/拒绝，不允许 apply。
3. Confirmed apply：只允许显式用户确认后的有限 action。

缺少审批 identity、optimistic locking 或 transaction tests 时，不得开启 apply。

## 9. Trace 与诊断

建议事件：

```text
memory.relation.candidate
memory.evolution.proposed
memory.evolution.accepted
memory.evolution.rejected
memory.evolution.apply.started
memory.evolution.applied
memory.evolution.apply.failed
```

安全 payload 只包含 proposal/candidate/target IDs、relation/action/status、score、scope、target version、reason code、content digest、actor 和 run/session/task ID，不默认记录完整内容。

## 10. 测试计划

### Domain 和 repository

- proposal/link enum、序列化和 confidence/timezone 边界；
- repository CRUD、状态过滤和 optimistic locking；
- migration upgrade/rollback。

### Relation decision

- duplicate、related、updates、contradicts、supersedes；
- invalid LLM output fallback；
- low confidence 不自动应用；
- scope mismatch 不生成 proposal。

### Approval 和 apply

- 未确认不能 archive/replace；
- reject 后 memory 不变；
- accept/apply 幂等；
- target version 改变时拒绝 apply；
- 事务中途失败完整回滚；
- task-local 不提升 global；
- 不同用户的 memory 不能建立 link。

### Compatibility

- flags 关闭时 candidate/promotion/trace snapshot 不变；
- proposal 失败不阻塞 candidate write；
- 不增加默认 LLM 调用；
- 不改变 semantic retrieval 排名。

## 11. 完成定义

只有同时满足以下条件，才能认为 Relation / Evolution 已接入运行时：

- proposal/link 有 PostgreSQL repository；
- candidate upsert 返回结构化 ID；
- legacy candidate 到 MemoryNote 的 scope-safe adapter 已完成；
- pending 写入后能持久化 proposal；
- 用户能查询、accept、reject；
- apply 有事务、版本检查和幂等保证；
- contradiction/supersede 未确认时 stable memory 不变；
- task/user/project/workspace scope 有隔离测试；
- trace 不包含完整敏感内容；
- flags 关闭时全量回归通过；
- migration 有 rollback 路径。

在这些条件完成之前，保持当前“纯 RelationDecider + pending proposal contract、无运行时自动应用”的状态，是更安全且更诚实的选择。
