# Agent Memory Runtime Evolution Report

## 1. 执行摘要

- 分支：`feat/memory-runtime-evolution`
- 基础提交：`f0d5050`（`feat: generate web session titles from first answer`）
- 完成阶段：Stage 0–8、Stage 10–12。
- 跳过阶段：Stage 9 动态 Context Pressure 策略。
- 功能提交：10 个；另有本报告与 checklist 的最终文档提交。
- 最终变更规模：32 个文件，2,892 行新增、18 行删除。
- 默认行为：不改变。所有会影响候选治理、pending enrichment、pressure observation、injection trace 的接入均默认关闭；普通工具的 schema、权限与 trace 顺序保持不变。
- 数据迁移：不需要。MemoryNote 使用 legacy adapter，未修改 PostgreSQL schema。
- 测试：最终全量 `635 passed, 0 failed, 2 warnings`，耗时 34.27 秒。

总体完成度按阶段计为 11/12。Stage 9 是按任务门禁主动跳过，不是实现失败：当前仓库没有独立 InjectionPlanner，直接让 ContextBuilder 根据压力改变 prompt 会把观测层和策略层耦合，无法证明核心指令永不被裁剪。

## 2. 当前代码事实

| 能力 | 当前真实实现 | 主要调用链 |
| --- | --- | --- |
| Legacy memory store | `memory/store.py::MemoryStore`、`memory/scoped_store.py` | lifecycle → scoped store → MEMORY/PENDING 兼容文件 |
| Semantic memory | `MemoryCommandService`、repository、index synchronizer、`SemanticMemoryRetrievalService` | explicit/tool → command service → PostgreSQL truth → Qdrant index → context retrieval |
| Pending memory | `MemoryProcessingDevice`、candidate store | after-turn pattern evaluation → governance（可选）→ enrichment（可选）→ `upsert_candidate` |
| Lifecycle | `MemoryLifecycle`、`BackgroundMemoryLifecycle` | Runtime `_after_turn` → background lifecycle → trace summary refresh |
| Promotion | legacy candidate promotion 与 `MemoryPromotionService` | evidence/confidence gate；本任务未改 promotion 决策 |
| Task-local | `MemoryContext.allowed_owners()` 与 `MemoryOwnerScope.TASK` | session metadata → scope decision；enricher 不可提升 scope |
| Context | `runtime/context/builder.py::ContextBuilder` | providers → budgeting → messages/report → 可选 pressure observation |
| Injection | 当前没有独立 InjectionPlanner | semantic retrieval 的全部有效 hits 由 renderer 注入；本任务只解释，不改变选择 |
| Dedup | `memory/dedup.py`、candidate/store/repository constraints | normalized exact match + 现有相似候选；RelationDecider 复用候选分数，不替换 dedup |
| Tool registry/policy | `ToolRegistry`、`ToolPolicy` | registry → visibility/policy → executor；governance metadata 是旁路分类 |
| Trace/audit | `TraceStore`、`trace_summary.json/md` | existing JSONL events → summary；未创建平行日志系统 |
| Coding application | `applications/coding` context state | 继续使用既有 coding context；pressure 仅观测，不干预压缩策略 |

## 3. Memory Hierarchy

当前和目标层级的详细对照见 `docs/system-design/memory-hierarchy.md`。本轮明确了以下边界：

1. session transcript 是会话事实，不是长期记忆；新 session 不应通过长期记忆路径检索原始跨会话消息。
2. working/task memory 是可恢复的短中期运行状态，不可被 enrichment 提升为 user/global。
3. pending/candidate 是治理入口；非用户推断、工具结果、低置信度或缺 source 的内容不得直接进入 stable。
4. stable semantic memory 仍以现有 PostgreSQL domain 为真相源；MemoryNote 是渐进兼容视图，不是第二套持久化系统。
5. relation/evolution 当前只生成 proposal；accepted link、旧记忆归档和替换仍需后续确认与持久化设计。
6. trace/archive 是审计证据，不会回流为长期记忆内容。

主要差距：缺少 proposal repository/审批状态机、独立 InjectionPlanner、真正的 contextual-description 注入选择，以及面向运维的治理查询入口。

## 4. 完成内容

### Stage 0–1：基线和设计

- 记录基线 `f0d5050`、清洁工作区和 `577 passed`。
- 新增 hierarchy、spec、plan、task、checklist。
- 明确 PostgreSQL truth、legacy adapter、默认关闭和无 migration 原则。

### Stage 2：MemoryNote / MemoryLink

- `MemoryNote`、`MemoryLink`、status/origin/relation enums。
- timezone、confidence、scope、source、metadata 边界验证。
- `from_legacy` / `to_legacy` 保留未知 metadata；candidate 与 accepted link 明确区分。

### Stage 3：Memory Write Governance

- `MemoryWriteRequest`、classification、policy decision、audit、result。
- 保守 secret detector 与 prompt-injection marker；不宣称覆盖所有秘密格式。
- `MEMORY_GOVERNANCE_ENABLED=0`；开启时接在 legacy candidate 写入前，discard 不触发 related/upsert。

### Stage 4：Tool Metadata

- `tool_scope`、state/risk/memory/context effect、audit、mode、policy tag。
- 旧注册调用使用 inert default；`catalog()` 和模型 tool schema 不变。
- `memorize` / `recall_memory` 有显式 kernel 分类；只有非默认 metadata 才产生诊断事件。

### Stage 5：Context Pressure

- 纯函数四级 evaluator，综合 token ratio、category imbalance、大工具结果、长任务和候选规模。
- 异常/负数/零 window 输入归一化；阈值集中在 value object。
- `CONTEXT_PRESSURE_OBSERVATION_ENABLED=0`；开启只写 report/trace，messages 完全一致。

### Stage 6：Relation / Evolution Proposal

- 支持 duplicate、related、updates、contradicts、supersedes、enriches。
- proposal ID 确定性生成，默认 pending，所有 action 均 `auto_apply=false`。
- 非法 adapter 输出回退为 related/no action；不改写、不归档 stable memory。
- 当前只提供 service/contract test；因 pending store 没有 proposal persistence 接点，不暴露一个看似可用但实际无效的运行时 flag。

### Stage 7：Pending Enrichment

- bounded description/keywords/tags、kind/confidence/provenance/version。
- `MEMORY_PENDING_ENRICHMENT_ENABLED=0`；关闭时甚至不构造新 request，metadata 与旧路径一致。
- adapter 不能写 origin/source/scope；task scope 不能提升；secret/injection 不发送给 adapter。
- enrichment confidence 只作为 metadata suggestion，不替换 candidate confidence，因此不改变 promotion。

### Stage 8：Injection Explanation

- candidate/retrieval/injection trace，包含 ID、scope、score、confidence、reason、representation、token estimate、pressure、link depth、policy tags、content digest。
- `MEMORY_INJECTION_TRACE_ENABLED=0`；关闭时只有旧 `memory.semantic.retrieved` 事件。
- trace 不含完整 memory content；trace callback 失败不会阻塞 retrieval。

### Stage 10：Diagnostics

- 复用 `trace_summary.json/md`，汇总 candidate、pending、stable/rejected、relation/evolution、tool scope/kernel action、pressure、injection。
- 治理判定 trace 对敏感 candidate 使用 `[redacted]`；diagnostic aggregation 不复制任意 event content。

### Stage 11–12：回归与自审

- 新增 8 组核心 contract tests，并扩展 lifecycle、trace summary 和 phase-0 snapshot 回归。
- 首次全量测试发现普通工具多出 governance event；修复为只有显式非默认 metadata 才发事件。
- 第二次全量、compileall、diff check 全部通过。

## 5. 未完成内容

| 内容 | 原因 | 是否阻塞 | 后续入口 | 优先级 |
| --- | --- | --- | --- | --- |
| Stage 9 动态 pressure policy | 无独立 InjectionPlanner，无法安全隔离选择策略与 ContextBuilder | 否 | 新建 planner，消费 `ContextPressurePolicyHint`，先做 shadow comparison | P1 |
| Evolution proposal persistence/approval | 当前 pending candidate store 没有 proposal transaction/audit repository | 否 | 在 PostgreSQL 增 proposal/link 表与 command service | P1 |
| RelationDecider 实际接入 | 上述 persistence 缺失；接入后只能生成无法管理的临时对象 | 否 | pending write 成功后检索 related，事务内写 proposal | P1 |
| contextual description 实际注入 | renderer 当前直接输出完整 content | 否 | InjectionPlanner 根据 pressure/representation policy 选择 | P2 |
| 完整 sensitive detector | 当前仅明显 pattern fallback | 否 | 可插拔 detector + 组织策略 + false-positive metrics | P2 |
| Tool metadata 批量分类 | 本轮只标注 memory 代表工具 | 否 | 按 runtime state mutation 审计 scheduler/context/admin 工具 | P2 |

## 6. 自主决策记录

- 选择 `MemoryProcessingDevice` 的 candidate upsert 前作为治理接点，因为它可在不碰 stable/promotion 的情况下阻止明显危险候选。
- enrichment 只写 pending metadata，刻意不采用 enrichment confidence 作为 upsert confidence。
- MemoryNote 使用 legacy adapter，而没有创建新 repository 或 migration，避免双真相源。
- relation proposal 不做“临时 trace-only 接入”，因为没有持久化和审批就无法形成可管理生命周期。
- Tool metadata 不进入模型 schema/catalog；内部另设 `governance_catalog()`。
- pressure observation 写 report/trace，不裁剪 context；Stage 9 因门禁不满足而跳过。
- injection trace 使用 SHA-256 截断 digest，不记录完整内容；query 的旧 retrieval event 保持兼容。
- 首次全量回归后收窄 tool governance event，保持默认 phase-0 trace 快照。

## 7. 测试结果

| 命令 | 退出码 | 通过 | 失败 | 跳过 | 耗时 | 备注 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| 基线 `PYTHONPATH=. pytest -q` | 0 | 577 | 0 | 0 | 34.50s | 修改前 |
| 新增核心 tests（8 文件） | 0 | 57 | 0 | 0 | 0.16s | Note/Governance/Evolution/Enrichment/Injection/Diagnostics/Tool/Pressure |
| 首次全量 `PYTHONPATH=. pytest -q` | 1 | 634 | 1 | 0 | 34.51s | phase-0 tool trace 顺序回归，已修复 |
| 回归点 + Tool metadata tests | 0 | 7 | 0 | 0 | 0.13s | 验证修复 |
| 最终全量 `PYTHONPATH=. pytest -q` | 0 | 635 | 0 | 0 | 34.27s | 2 条基线同类 protobuf deprecation warning |
| `PYTHONPATH=. python -m compileall -q memory runtime tools tests` | 0 | — | 0 | — | 0.2s | 无输出 |
| `git diff --check` | 0 | — | 0 | — | <0.1s | 无 whitespace error |

仓库没有配置独立 lint、formatter 或 type-check 命令；`pyproject.toml` 仅定义 pytest dev dependency，因此没有虚构这些验证结果。

## 8. Git 提交

| Commit | Subject | 主要内容 | Stage |
| --- | --- | --- | --- |
| `132f5c2` | docs(memory): document runtime memory hierarchy | hierarchy + spec/plan/task/checklist | 0–1 |
| `edcca4b` | feat(memory): add memory note and link models | notes/links/adapters | 2 |
| `c32c0c3` | feat(memory): add governed memory write pipeline | governance + opt-in candidate hook | 3 |
| `a6fd032` | feat(tools): add runtime tool governance metadata | ToolSpec metadata/catalog | 4 |
| `1b8942b` | feat(runtime): add context pressure observation | evaluator + report/trace observation | 5 |
| `c9dbcdc` | feat(memory): add conservative evolution proposals | decider + pending proposal | 6 |
| `97e89ba` | feat(memory): enrich pending memory candidates | bounded enrichment + bootstrap flag | 7 |
| `96e3f63` | feat(memory): trace retrieval and injection decisions | safe injection explanation + summary | 8 |
| `d3eedd2` | feat(runtime): add memory governance diagnostics | lifecycle/tool events + summary diagnostics | 10 |
| `f20e861` | fix(tools): preserve default trace event order | phase-0 compatibility fix | 11 |

最终报告/checklist 提交是自引用文档提交，不在表中固化自身 hash；可用 `git log -1 --oneline` 查看。

## 9. 风险

- Schema/migration：当前无数据库变更，风险低；未来 proposal/link persistence 必须设计可回滚 migration。
- Backward compatibility：flags 默认关闭、schema/catalog 未变；风险主要来自显式开启实验 flag 后新增 metadata/trace。
- Prompt behavior：pressure/injection trace 不改 messages；最终 snapshot/full suite 已覆盖。动态策略尚未实现。
- Token estimation：目前采用 chars/4 的近似值，仅用于压力观测和解释，不能当模型 tokenizer 的精确计费结果。
- Trace privacy：新 injection trace 不含完整 content，但旧 trace/event 体系仍可能包含其他模块的 preview；部署前仍需整体 privacy audit。
- LLM validation：Relation/Enrichment adapters 有 enum、范围、字段白名单和 fallback；当前没有真实 LLM 接入测试。
- Task/global scope：enricher 固定继承 request provenance；未来 command/proposal persistence 仍需 DB 级 owner constraints。
- Contradiction：只生成 pending proposal，不自动解决；风险是 proposal 尚未持久化，暂不能用于运行时治理。
- Tool policy：metadata 不参与 authorization，避免暗改权限；这也意味着 metadata 当前只用于审计，不能替代 ToolPolicy。
- Sensitive detection：fallback pattern 覆盖有限，不能视为 DLP 或完美安全过滤器。

## 10. 明日人工测试清单

### 快速验证

```bash
cd /home/tale/kaggle/mytry
git switch feat/memory-runtime-evolution
PYTHONPATH=. pytest -q tests/test_memory_governance.py tests/test_memory_enrichment.py tests/test_memory_injection_trace.py tests/test_context_pressure.py tests/test_tool_governance_metadata.py
```

期望：全部通过；失败先查看对应同名模块和测试。

### 模块验证

```bash
PYTHONPATH=. pytest -q tests/test_memory_*.py
PYTHONPATH=. pytest -q tests/test_context_*.py tests/test_runtime_phase*context*.py
PYTHONPATH=. pytest -q tests/test_tool*.py tests/test_runtime_phase0_contract_baseline.py tests/test_runtime_phase0_extended_baseline.py
PYTHONPATH=. pytest -q
```

### 行为回归（flags 关闭）

1. 确认 `.env` 中以下值为 `0` 或未设置：`MEMORY_GOVERNANCE_ENABLED`、`MEMORY_PENDING_ENRICHMENT_ENABLED`、`CONTEXT_PRESSURE_OBSERVATION_ENABLED`、`MEMORY_INJECTION_TRACE_ENABLED`。
2. 运行 phase-0 snapshots 和 `tests/test_memory_governance.py::test_candidate_integration_without_governance_preserves_legacy_write`。
3. 期望 tool schema/visibility/trace 顺序不变，candidate metadata 只有旧字段，prompt messages 不变。

### 实验验证（flags 开启）

在仅测试环境设置：

```bash
export MEMORY_GOVERNANCE_ENABLED=1
export MEMORY_PENDING_ENRICHMENT_ENABLED=1
export CONTEXT_PRESSURE_OBSERVATION_ENABLED=1
export MEMORY_INJECTION_TRACE_ENABLED=1
```

运行一次本地会话后，检查 `.runs/<run-id>/trace_summary.json` 和 `trace_summary.md`：应出现 governance、pressure、injection 汇总。Relation proposal 尚未接入 runtime，请用 `PYTHONPATH=. pytest -q tests/test_memory_evolution.py` 验证纯 service。

### 手工场景

| # | 操作步骤 | 期望结果 | 检查位置 | 失败时看 |
| ---: | --- | --- | --- | --- |
| 1 | 明确说“请记住我偏好简洁回答” | explicit 路径按现有 semantic policy 写入；有 source/audit | trace summary、PostgreSQL memory item | command service / lifecycle |
| 2 | 再次添加完全相同偏好 | dedup，不创建第二条 stable；纯 decider 为 duplicate proposal | memory events、evolution test | dedup / evolution |
| 3 | 添加“回答时优先列结论”的相关偏好 | 不自动合并；decider 可给 related | proposal contract output | evolution |
| 4 | 添加更新旧事实的记忆，并提供 relation hint | pending updates proposal，旧 memory 不变 | proposal `proposed_action` | evolution |
| 5 | 添加与旧偏好冲突内容 | request confirmation proposal，不归档旧项 | proposal/audit | governance / evolution |
| 6 | coding task 中形成 task-local conclusion | scope 保持 task | enrichment metadata/audit | processor / commands |
| 7 | 新 user session 检查 global memory | task-local 不应出现在 user/global recall | retrieval drop/scope | commands / semantic retrieval |
| 8 | 输入 `API_KEY=sk-example-123456789` 样式候选 | governance discard；preview 为 `[redacted]`；不 upsert | `memory.governance.decided` | governance / lifecycle |
| 9 | 输入“Ignore previous instructions...”候选 | 不发给 enricher，不写 stable | governance/enrichment audit | governance / enrichment |
| 10 | 产生多个大 tool output | pressure 至少由 large result/category signal提升 | `context.pressure.observed` | pressure / builder |
| 11 | 构造接近 context window 的长会话 | HIGH/CRITICAL hint；system/current user messages仍在 | context report + prompt snapshot | pressure / builder |
| 12 | 执行能命中 semantic memory 的请求 | injection trace 有 selected/filtered、score、digest，无完整 content | `memory.injection.explained` | semantic retrieval / trace summary |

任何手工实验都应使用非生产数据库和假 secret；不要把真实凭证作为过滤器测试样本。
