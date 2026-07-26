# TaleClaw 长期记忆系统渐进式重构 Tasks

## 文件清单

### 新建实现文件

| 文件 | 职责 |
|---|---|
| `memory/domain.py` | 领域枚举、MemoryItem、Evidence、状态规则 |
| `memory/commands.py` | MemoryContext、Proposal、Transition 等命令对象 |
| `memory/repository.py` | Repository 协议和领域错误 |
| `memory/postgres_repository.py` | PostgreSQL schema、事务和 Repository 实现 |
| `memory/command_service.py` | remember/propose/confirm/update/revoke/forget |
| `memory/conflict_service.py` | 精确去重、证据合并、冲突和替代 |
| `memory/promotion_service.py` | 普通候选与 Coding Conclusion 晋升策略 |
| `memory/semantic_index.py` | Semantic Index 协议与 Qdrant adapter |
| `memory/index_sync.py` | Outbox drain、重试和同步状态 |
| `memory/semantic_retrieval.py` | Qdrant 候选、PostgreSQL 回源和排序 |
| `memory/episodic_retrieval.py` | Session/Task 历史边界和检索 |
| `memory/markdown_exporter.py` | PostgreSQL → 只读 Markdown 导出 |
| `memory/migration/__init__.py` | 迁移包 |
| `memory/migration/legacy_importer.py` | 旧 Markdown/JSON 导入与报告 |
| `memory/migration/rebuild_index.py` | Semantic index 重建 |
| `scripts/migrate_long_term_memory.py` | 导入 CLI |
| `scripts/rebuild_memory_index.py` | 索引重建 CLI |

### 修改实现文件

| 文件 | 改动 |
|---|---|
| `runtime/bootstrap.py` | 新服务、feature flag、Provider 和 synchronizer 装配 |
| `runtime/context/providers.py` | Semantic/Episodic Provider |
| `runtime/context/retrieval.py` | 收敛为受边界约束的历史检索适配 |
| `runtime/context/builder.py` | 三类上下文分区和 report |
| `runtime/context/budget.py` | Semantic/Episodic 独立预算 |
| `runtime/context/build_state.py` | 新区块状态 |
| `memory/lifecycle.py` | proposal 输出、停止整文件索引和重复历史写入 |
| `memory/processor.py` | 普通候选适配统一 promotion |
| `memory/store.py` | Legacy adapter、只读兼容和 deprecation |
| `memory/vector_index.py` | Episodic boundary/filter contract |
| `memory/qdrant_index.py` | 多字段过滤与单事件粒度 |
| `memory/vector_runtime.py` | Semantic/Episodic index 构建与 collection 配置 |
| `tools/handlers.py` | 统一 MemoryCommandService 调用 |
| `tools/schema.py` | 新工具语义和旧 section 兼容说明 |
| `applications/coding/conclusions.py` | 结论转统一 proposal 所需证据字段 |
| `applications/coding/promotion.py` | 从 Markdown promotion 改为统一服务 |
| `applications/coding/runner.py` | 可信 workspace/project/task context |
| `runtime/trace/summary.py` | Memory 事件和指标汇总 |

### 新建测试文件

| 文件 | 覆盖 |
|---|---|
| `tests/fakes/in_memory_memory_repository.py` | Fake Repository |
| `tests/fakes/in_memory_memory_index.py` | Fake Semantic/Episodic Index |
| `tests/test_memory_domain.py` | 枚举、状态和领域校验 |
| `tests/test_memory_postgres_repository.py` | PostgreSQL CRUD、版本与 outbox |
| `tests/test_memory_command_service.py` | 显式记忆、更新、撤销、遗忘 |
| `tests/test_memory_conflicts.py` | 重复、冲突和 scope |
| `tests/test_memory_index_sync.py` | Outbox 和最终一致性 |
| `tests/test_semantic_memory_retrieval.py` | 回源过滤、排序和降级 |
| `tests/test_episodic_history_scope.py` | Session/Task 边界 |
| `tests/test_memory_context_sections.py` | Semantic/Episodic/Working 分区 |
| `tests/test_memory_promotion_service.py` | 候选晋升策略 |
| `tests/test_coding_memory_proposals.py` | Coding Conclusion 作用域与证据 |
| `tests/test_legacy_memory_importer.py` | dry-run、幂等和映射 |
| `tests/test_memory_index_rebuild.py` | 重建和一致性恢复 |
| `tests/test_memory_markdown_export.py` | 只读导出 |

### 文档交付物

| 文件 | 职责 |
|---|---|
| `docs/refactors/memory-system-audit.md` | 当前真实链路审计 |
| `docs/architecture/MEMORY_ARCHITECTURE.md` | 目标架构 |
| `docs/migrations/long-term-memory-migration.md` | 迁移与回滚 |
| `memory-refactor-summary.md` | 最终实施与验收报告 |

## Phase 0：审计与保护性测试

### T01：生成当前系统审计报告

**文件：** `docs/refactors/memory-system-audit.md`
**依赖：** 无

**步骤：**
1. 列出当前 memory、context、tools、coding、PostgreSQL、Qdrant 和 Trace 模块。
2. 绘制普通写入、显式记忆、候选晋升、Coding promotion、检索和遗忘数据流。
3. 记录每种数据的事实来源、重复副本、调用方、废弃候选和迁移风险。
4. 明确当前没有 Minecraft Application。

**验证：** `rg -n '^## ' docs/refactors/memory-system-audit.md` 覆盖 Spec 要求的 12 个审计主题。

### T02：锁定旧 Markdown 记忆行为

**文件：** `tests/test_memory_recall.py`、`tests/test_memory_lifecycle_archive.py`
**依赖：** T01

**步骤：**
1. 增加现有 recall 混合 MEMORY/PENDING/HISTORY 的行为测试。
2. 增加每轮写 HISTORY、RECENT_CONTEXT 和整文件索引的基线断言。
3. 将测试标注为 legacy baseline，后续迁移时有意识更新。

**验证：** `python -m pytest -q tests/test_memory_recall.py tests/test_memory_lifecycle_archive.py` 通过。

### T03：增加跨 Session 泄漏复现测试

**文件：** `tests/test_episodic_history_scope.py`
**依赖：** T01

**步骤：**
1. 创建同用户 Session A、B 和不同用户 Session C。
2. 写入 A 的历史 turn，并用 B/C 的相似请求查询。
3. 记录当前 B 会命中的失败基线，定义目标为只有 A 可命中。

**验证：** 测试在旧实现上准确暴露 B 的越界命中，且不会误报跨用户结果。

### T04：记录旧数据与索引规模

**文件：** `docs/refactors/memory-system-audit.md`
**依赖：** T01

**步骤：**
1. 统计每个用户 legacy 文件条目数和字符数，不输出敏感全文。
2. 统计 Qdrant source type、scope 和整文件 point 数。
3. 记录 PostgreSQL `memory_archive`、sessions/messages 数量。

**验证：** 审计报告包含可重复的只读统计命令和统计日期。

## Phase 1：领域模型、Repository 与 Outbox

### T05：实现领域枚举和 OwnerKey

**文件：** `memory/domain.py`、`tests/test_memory_domain.py`
**依赖：** T01

**步骤：**
1. 定义 OwnerScope、Kind、Status、SourceType。
2. 定义不可变 OwnerKey 并校验非空 owner ID。
3. 测试所有合法值和非法自由字符串。

**验证：** `python -m pytest -q tests/test_memory_domain.py -k 'enum or owner'` 通过。

### T06：实现 MemoryItem 与状态转换规则

**文件：** `memory/domain.py`、`tests/test_memory_domain.py`
**依赖：** T05

**步骤：**
1. 定义 MemoryItem、MemoryEvidence 和时间字段。
2. 校验 confidence/salience、有效期、版本和 supersedes 关系。
3. 定义允许的状态转换矩阵。
4. 覆盖 candidate→active、active→superseded/revoked/expired 和非法转换。

**验证：** `python -m pytest -q tests/test_memory_domain.py` 通过。

### T07：实现可信上下文与 Proposal 命令对象

**文件：** `memory/commands.py`、`tests/test_memory_domain.py`
**依赖：** T05

**步骤：**
1. 定义 MemoryContext、MemoryWriteProposal 和 Transition。
2. 禁止缺少 user/session 的普通请求上下文。
3. 为 workspace/project/task scope 校验对应可信 ID。

**验证：** `python -m pytest -q tests/test_memory_domain.py -k 'context or proposal'` 通过。

### T08：定义 Repository 协议与领域错误

**文件：** `memory/repository.py`
**依赖：** T06、T07

**步骤：**
1. 定义 create/get/get_many/list_active/find_exact/transition/add_evidence。
2. 定义 NotFound、VersionConflict、ScopeDenied 和 InvalidTransition。
3. 保证协议不暴露 SQL row 或 psycopg 类型。

**验证：** `python -m py_compile memory/repository.py` 通过，且 `rg -n 'psycopg|cursor|Row' memory/repository.py` 无结果。

### T09：实现 Fake Repository

**文件：** `tests/fakes/in_memory_memory_repository.py`
**依赖：** T08

**步骤：**
1. 实现协议的确定性内存版本。
2. 支持 evidence、active query 和 expected version。
3. 为后续服务测试提供事务结果与 outbox 观察点。

**验证：** `python -m py_compile tests/fakes/in_memory_memory_repository.py` 通过。

### T10：创建 PostgreSQL schema

**文件：** `memory/postgres_repository.py`、`tests/test_postgres_store_schemas.py`
**依赖：** T08

**步骤：**
1. 创建 memory_items、memory_evidence、memory_index_outbox、memory_schema_versions。
2. 增加 CHECK、FK、唯一约束和查询索引。
3. 使用现有 `runtime.db` 与幂等初始化风格。

**验证：** `python -m pytest -q tests/test_postgres_store_schemas.py` 通过，重复初始化不报错。

### T11：实现 PostgreSQL CRUD 与查询映射

**文件：** `memory/postgres_repository.py`、`tests/test_memory_postgres_repository.py`
**依赖：** T10

**步骤：**
1. 实现 create/get/get_many/list_active/find_exact。
2. 将 SQL 行转换为领域对象。
3. 测试 scope、status、valid time 和 current version 查询。

**验证：** `python -m pytest -q tests/test_memory_postgres_repository.py -k 'crud or query'` 通过。

### T12：实现状态转换、乐观锁和 Evidence

**文件：** `memory/postgres_repository.py`、`tests/test_memory_postgres_repository.py`
**依赖：** T11

**步骤：**
1. 实现 expected version 转换和 evidence 追加。
2. 在冲突时抛出 VersionConflict。
3. 测试 supersedes chain、并发更新和 evidence 保留。

**验证：** `python -m pytest -q tests/test_memory_postgres_repository.py -k 'version or evidence or supersede'` 通过。

### T13：实现事务内 Outbox 写入

**文件：** `memory/postgres_repository.py`、`tests/test_memory_postgres_repository.py`
**依赖：** T12

**步骤：**
1. 状态变化时写入唯一 outbox 事件。
2. 保证 memory 与 outbox 同事务提交或回滚。
3. 实现 claim/complete/retry 所需 Repository 方法。

**验证：** `python -m pytest -q tests/test_memory_postgres_repository.py -k 'outbox or transaction'` 通过。

## Phase 2：显式记忆垂直切片

### T14：实现精确重复与 Scope 冲突规则

**文件：** `memory/conflict_service.py`、`tests/test_memory_conflicts.py`
**依赖：** T09、T12

**步骤：**
1. 复用确定性规范化函数生成去重键。
2. 同 owner/kind/content 合并 evidence。
3. 不同 scope 不判冲突。

**验证：** `python -m pytest -q tests/test_memory_conflicts.py -k 'exact or scope'` 通过。

### T15：实现语义重复与替代决策

**文件：** `memory/conflict_service.py`、`tests/test_memory_conflicts.py`
**依赖：** T14

**步骤：**
1. 对有限同 scope/kind 候选执行语义相似判断。
2. 相似内容合并证据，明确冲突返回 supersede 决策。
3. 不确定冲突保持 candidate，不自动覆盖。

**验证：** `python -m pytest -q tests/test_memory_conflicts.py` 通过。

### T16：实现 remember 与 propose

**文件：** `memory/command_service.py`、`tests/test_memory_command_service.py`
**依赖：** T09、T15

**步骤：**
1. 校验 MemoryContext 与 proposal owner。
2. 显式 remember 创建 active，推断 propose 创建 candidate。
3. 应用重复/冲突规则并写 Trace payload。

**验证：** `python -m pytest -q tests/test_memory_command_service.py -k 'remember or propose'` 通过。

### T17：实现 confirm、reject、update 与 supersede

**文件：** `memory/command_service.py`、`tests/test_memory_command_service.py`
**依赖：** T16

**步骤：**
1. 实现 candidate 确认/拒绝。
2. update 创建新版本并 supersede 旧 active。
3. 校验 owner、状态与 expected version。

**验证：** `python -m pytest -q tests/test_memory_command_service.py -k 'confirm or reject or update'` 通过。

### T18：实现 revoke 与 forget

**文件：** `memory/command_service.py`、`tests/test_memory_command_service.py`
**依赖：** T17

**步骤：**
1. revoke 保存原因并调度索引删除。
2. forget 在可信 scope 内查找匹配 active 记忆。
3. 禁止跨 owner 撤销并保留审计。

**验证：** `python -m pytest -q tests/test_memory_command_service.py -k 'revoke or forget'` 通过。

### T19：定义 Semantic Index 与 Fake Adapter

**文件：** `memory/semantic_index.py`、`tests/fakes/in_memory_memory_index.py`
**依赖：** T06

**步骤：**
1. 定义 IndexedMemoryHit 和 upsert/delete/search。
2. Fake 支持 owner filters、版本和失败注入。
3. 不返回 Evidence 或把 payload 当事实。

**验证：** `python -m py_compile memory/semantic_index.py tests/fakes/in_memory_memory_index.py` 通过。

### T20：实现 Qdrant Semantic Adapter

**文件：** `memory/semantic_index.py`、`memory/vector_runtime.py`
**依赖：** T19

**步骤：**
1. 创建独立 semantic collection 配置。
2. point 粒度为 memory ID + version。
3. payload 只保存批准的检索元数据。

**验证：** 使用 Fake Qdrant 单测确认 upsert/delete/search payload 不含完整 evidence 或 Markdown 文件。

### T21：实现 Outbox Synchronizer

**文件：** `memory/index_sync.py`、`tests/test_memory_index_sync.py`
**依赖：** T13、T19

**步骤：**
1. claim pending 事件并按版本 upsert/delete。
2. 成功 complete，失败记录 error 和 next retry。
3. 保证 Qdrant 调用不在 PostgreSQL 事务内。

**验证：** `python -m pytest -q tests/test_memory_index_sync.py` 通过。

### T22：实现 Semantic Retrieval 回源过滤

**文件：** `memory/semantic_retrieval.py`、`tests/test_semantic_memory_retrieval.py`
**依赖：** T11、T19

**步骤：**
1. 从 MemoryContext 生成允许 OwnerKey。
2. Qdrant 命中后批量回源 PostgreSQL。
3. 过滤非 active、旧版本、过期和越界记录。
4. 实现相关性×置信度×新鲜度×显著性排序与去重。

**验证：** `python -m pytest -q tests/test_semantic_memory_retrieval.py` 通过。

### T23：实现安全降级

**文件：** `memory/semantic_retrieval.py`、`tests/test_semantic_memory_retrieval.py`
**依赖：** T22

**步骤：**
1. Qdrant 不可用时走受限 PostgreSQL 候选。
2. 不放宽 scope/status/version 条件。
3. 输出 degradation 与 drop reason。

**验证：** `python -m pytest -q tests/test_semantic_memory_retrieval.py -k 'fallback or stale'` 通过。

### T24：接入 Semantic Context Provider 与预算

**文件：** `runtime/context/providers.py`、`builder.py`、`build_state.py`、`budget.py`、`tests/test_memory_context_sections.py`
**依赖：** T22

**步骤：**
1. 新增 semantic memory provider 和独立 budget。
2. 输出 `<semantic_memory>` 并记录 hit/drop/report。
3. 保持 Working Memory 顺序与现有行为。

**验证：** `python -m pytest -q tests/test_memory_context_sections.py -k semantic` 通过。

### T25：迁移 memorize 工具到 CommandService

**文件：** `tools/handlers.py`、`tools/schema.py`、`tests/test_memory_scope.py`
**依赖：** T16、T24

**步骤：**
1. 从 Session 构造可信 MemoryContext。
2. `memorize` 生成 explicit proposal，不再直接追加 Markdown。
3. 旧 section 参数提供明确兼容/拒绝信息。

**验证：** `python -m pytest -q tests/test_memory_scope.py tests/test_memory_command_service.py` 通过。

### T26：Bootstrap 新服务与 Feature Flags

**文件：** `runtime/bootstrap.py`、`.env.example`
**依赖：** T21、T24、T25

**步骤：**
1. 装配 Repository、CommandService、Index、Synchronizer、Retriever。
2. 增加新写入、新读取和 semantic context flags。
3. 默认保持可控迁移顺序并记录启用状态。

**验证：** `python -m pytest -q tests/test_runtime_phase0_entrypoint_baseline.py tests/test_context_instructions.py` 通过。

### T27：完成显式记忆垂直切片集成测试

**文件：** `tests/test_memory_command_service.py`、`tests/test_memory_context_sections.py`、`tests/test_memory_index_sync.py`
**依赖：** T18、T21、T26

**步骤：**
1. 覆盖 remember→PostgreSQL→outbox→Qdrant→Context。
2. 覆盖 update/supersede 与 forget/revoke。
3. 覆盖 Qdrant 失败但写入成功。

**验证：** 三个测试文件全部通过，满足 AC1、AC2、AC3、AC10、AC14。

## Phase 3：普通候选与 Coding Conclusion 统一

### T28：实现统一 Promotion Policy

**文件：** `memory/promotion_service.py`、`tests/test_memory_promotion_service.py`
**依赖：** T16

**步骤：**
1. 校验独立 Session/Task 证据、作用域和噪音。
2. 单次推断保持 candidate。
3. 支持 confirm-required 与 auto-promote 结果。

**验证：** `python -m pytest -q tests/test_memory_promotion_service.py` 通过。

### T29：普通 MemoryLifecycle 输出 Proposal

**文件：** `memory/lifecycle.py`、`memory/processor.py`、`tests/test_memory_lifecycle_archive.py`
**依赖：** T28

**步骤：**
1. 将显式提取和 inferred candidate 转成 proposal。
2. 使用统一 Command/Promotion Service。
3. 在兼容 flag 下保留旧 lifecycle baseline。

**验证：** `python -m pytest -q tests/test_memory_lifecycle_archive.py tests/test_memory_promotion_service.py` 通过。

### T30：扩展 Coding Conclusion 证据字段

**文件：** `applications/coding/conclusions.py`、`tests/test_coding_memory_proposals.py`
**依赖：** T07

**步骤：**
1. 增加 evidence file/location、category 和代码版本字段。
2. 保持 extractor 对旧 JSON 输出兼容。
3. 过滤缺少证据的高风险结论。

**验证：** `python -m pytest -q tests/test_coding_memory_proposals.py -k extraction` 通过。

### T31：把 TaskMemoryPromoter 改为 Proposal Adapter

**文件：** `applications/coding/promotion.py`、`tests/test_coding_memory_proposals.py`
**依赖：** T28、T30

**步骤：**
1. 不再向全局 `PENDING.md` 写入。
2. 将结论映射为 workspace/project candidate proposal。
3. 保留 rejected/skipped/promoted 的兼容报告结构。

**验证：** `python -m pytest -q tests/test_coding_memory_proposals.py -k promotion` 通过。

### T32：接入可信 Workspace/Project/Task 上下文

**文件：** `applications/coding/runner.py`、`promotion.py`、`tests/test_coding_memory_proposals.py`
**依赖：** T31

**步骤：**
1. 从 Workspace Resolver 获取 root/display/repository。
2. 记录 task ID 和可用 commit。
3. 禁止模型提供的 owner ID 覆盖可信值。

**验证：** `python -m pytest -q tests/test_coding_memory_proposals.py tests/test_memory_scope.py` 通过。

### T33：候选与 Coding 端到端测试

**文件：** `tests/test_memory_promotion_service.py`、`tests/test_coding_memory_proposals.py`
**依赖：** T29、T32

**步骤：**
1. 验证普通单次推断不 active。
2. 验证独立证据达到 policy 后可晋升。
3. 验证 Coding 项目 A 不泄漏到 B。

**验证：** 两个测试文件通过，满足 AC4、AC5、AC6。

## Phase 4：Semantic 与 Episodic 分离

### T34：定义 Episodic Event 与 Boundary

**文件：** `memory/episodic_retrieval.py`、`memory/vector_index.py`、`tests/test_episodic_history_scope.py`
**依赖：** T07

**步骤：**
1. 定义 session/task/workspace/project 边界。
2. 普通边界强制 user + session。
3. 定义 EpisodicHit 与历史语义提示。

**验证：** `python -m pytest -q tests/test_episodic_history_scope.py -k boundary` 通过。

### T35：实现 Qdrant 多字段历史过滤

**文件：** `memory/qdrant_index.py`、`memory/vector_runtime.py`、`tests/test_episodic_history_scope.py`
**依赖：** T34

**步骤：**
1. 支持 user/session/source_type 等 must filters。
2. Coding 支持可信 task/workspace/project filters。
3. 旧单 scope API 保留 deprecated adapter。

**验证：** `python -m pytest -q tests/test_episodic_history_scope.py -k qdrant` 通过。

### T36：实现 Episodic Retrieval Service

**文件：** `memory/episodic_retrieval.py`、`runtime/context/retrieval.py`、`tests/test_episodic_history_scope.py`
**依赖：** T35

**步骤：**
1. 当前 Session 查询只产生当前 session filters。
2. 回传 source refs、历史事件标签和 score。
3. 服务失败安全降级为空，不退回用户级宽查询。

**验证：** `python -m pytest -q tests/test_episodic_history_scope.py` 通过。

### T37：Context Builder 输出 Episodic 分区

**文件：** `runtime/context/providers.py`、`builder.py`、`build_state.py`、`budget.py`、`tests/test_memory_context_sections.py`
**依赖：** T36

**步骤：**
1. 将 retrieved_history 重命名/兼容为 episodic_history。
2. 输出 `<episodic_history>` 和独立 report/budget。
3. 保证 semantic、episodic、working 顺序明确。

**验证：** `python -m pytest -q tests/test_memory_context_sections.py tests/test_context_instructions.py` 通过。

### T38：停止整份 Markdown 向量写入

**文件：** `memory/lifecycle.py`、`tests/test_memory_lifecycle_archive.py`
**依赖：** T22、T36

**步骤：**
1. 移除/flag 关闭 `_index_memory_files` 生产调用。
2. Session turn 只写单事件索引。
3. Trace 不再出现新的 memory.file_vector upsert。

**验证：** `python -m pytest -q tests/test_memory_lifecycle_archive.py tests/test_run_trace.py -k memory` 通过。

### T39：跨 Session 和 Context 综合测试

**文件：** `tests/test_episodic_history_scope.py`、`tests/test_memory_context_sections.py`
**依赖：** T37、T38

**步骤：**
1. 新 Session B 不命中 A 的 turn。
2. B 可以命中同用户 active semantic memory。
3. 当前 Session A 可命中自己的 episodic event。

**验证：** 两个测试文件通过，满足 AC7、AC8、AC9、AC13。

## Phase 5：迁移、导出和索引重建

### T40：实现 Legacy 文件解析与分类

**文件：** `memory/migration/legacy_importer.py`、`tests/test_legacy_memory_importer.py`
**依赖：** T16

**步骤：**
1. 解析 MEMORY bullets 和 PENDING.json candidates。
2. SELF/NOW 默认 review，HISTORY/RECENT 默认 skip semantic。
3. 对 PENDING.md/JSON 漂移输出 review item。

**验证：** `python -m pytest -q tests/test_legacy_memory_importer.py -k parse` 通过。

### T41：实现 Importer dry-run、幂等与 checkpoint

**文件：** `memory/migration/legacy_importer.py`、`tests/test_legacy_memory_importer.py`
**依赖：** T40

**步骤：**
1. 定义 import batch 和幂等键。
2. dry-run 零写入并输出逐条原因。
3. 支持失败 checkpoint 与重复执行。

**验证：** `python -m pytest -q tests/test_legacy_memory_importer.py` 通过。

### T42：实现迁移 CLI

**文件：** `scripts/migrate_long_term_memory.py`
**依赖：** T41

**步骤：**
1. 支持 dry-run、user、source root、candidate、report、checkpoint 参数。
2. 默认 dry-run 或要求显式 apply。
3. 输出 imported/skipped/duplicate/conflict/failed。

**验证：** `python scripts/migrate_long_term_memory.py --help` 成功；fixture dry-run 不修改文件和数据库。

### T43：实现只读 Markdown Exporter

**文件：** `memory/markdown_exporter.py`、`tests/test_memory_markdown_export.py`
**依赖：** T11

**步骤：**
1. 按 scope/kind 导出 active 与最近 superseded。
2. 文件顶部标注 generated/read-only。
3. 使用临时文件和原子替换。

**验证：** `python -m pytest -q tests/test_memory_markdown_export.py` 通过。

### T44：实现索引重建服务与 CLI

**文件：** `memory/migration/rebuild_index.py`、`scripts/rebuild_memory_index.py`、`tests/test_memory_index_rebuild.py`
**依赖：** T20、T22

**步骤：**
1. 从 PostgreSQL active snapshot 生成新 collection。
2. 校验数量、版本和 digest 后切 alias。
3. 支持 dry-run、owner filter 和失败恢复。

**验证：** `python -m pytest -q tests/test_memory_index_rebuild.py` 与 `python scripts/rebuild_memory_index.py --help` 通过。

### T45：完成迁移文档

**文件：** `docs/migrations/long-term-memory-migration.md`
**依赖：** T42、T44

**步骤：**
1. 记录 schema upgrade、feature flag 切换和 downgrade。
2. 记录 legacy mapping、dry-run、apply、验证、回滚。
3. 明确不自动删除源文件、旧表或 collection。

**验证：** 文档包含 upgrade、dry-run、apply、verify、rollback、cleanup 六个可执行章节。

## Phase 6：旧链路退役、可观测性与文档

### T46：停止普通会话重复历史文件写入

**文件：** `memory/lifecycle.py`、`memory/store.py`、`tests/test_memory_lifecycle_archive.py`
**依赖：** T39、T41

**步骤：**
1. 停止普通会话追加 HISTORY 和更新 RECENT_CONTEXT。
2. Coding task-local 兼容路径单独保留并标注。
3. PostgreSQL Session/episodic 路径继续可用。

**验证：** 更新后的 lifecycle 测试确认普通会话不再写文件，Coding 兼容测试仍通过。

### T47：把 MemoryStore 降级为 Legacy Adapter

**文件：** `memory/store.py`、`tools/handlers.py`、`tools/schema.py`、`tests/test_memory_scope.py`
**依赖：** T25、T43、T46

**步骤：**
1. 生产长期写入全部经过 CommandService。
2. 旧 append/recall 路径标记 deprecated 或限制为 task-local。
3. recall_memory 改为 Semantic Retrieval，不混入 Pending/History。

**验证：** `python -m pytest -q tests/test_memory_scope.py tests/test_memory_recall.py` 通过；生产 handler 不直接调用 `MemoryStore.append`。

### T48：补齐 Memory Trace 事件

**文件：** `memory/command_service.py`、`promotion_service.py`、`index_sync.py`、`semantic_retrieval.py`、`episodic_retrieval.py`、`tests/test_run_trace.py`
**依赖：** T33、T39、T44

**步骤：**
1. 写入 spec 定义的 item、candidate、index、retrieval、drop 事件。
2. 限制 preview 并记录 ID/scope/kind/version/reason。
3. 覆盖成功、拒绝、失败和降级。

**验证：** `python -m pytest -q tests/test_run_trace.py -k memory` 通过。

### T49：实现 Memory 指标汇总

**文件：** `runtime/trace/summary.py`、相关 evaluation 模块、`tests/test_run_trace.py`
**依赖：** T48

**步骤：**
1. 汇总写入、候选、晋升、拒绝、替代、撤销、重复、冲突和索引失败。
2. 汇总命中、unused、stale、cross-scope leak 和 context token ratio。
3. 对分母为零和缺事件安全处理。

**验证：** `python -m pytest -q tests/test_run_trace.py -k 'memory and metric'` 通过。

### T50：完成目标架构文档

**文件：** `docs/architecture/MEMORY_ARCHITECTURE.md`
**依赖：** T27、T33、T39、T44

**步骤：**
1. 记录领域模型、生命周期、写入、检索和存储职责。
2. 记录 Qdrant outbox、Coding promotion、遗忘和迁移。
3. 标注 feature flags、回滚边界和未实现 Application 适配。

**验证：** 文档逐项覆盖 Plan 的架构章节，接口名称与实现一致。

### T51：运行分层回归测试

**文件：** 无
**依赖：** T47、T49、T50

**步骤：**
1. 运行 memory/context/coding/session/trace 定向测试。
2. 运行 PostgreSQL 与可选 Qdrant 集成测试。
3. 运行完整测试、py_compile 和仓库已有静态检查。

**验证：**

```bash
python -m pytest -q tests/test_memory_domain.py tests/test_memory_postgres_repository.py tests/test_memory_command_service.py tests/test_memory_conflicts.py tests/test_memory_index_sync.py tests/test_semantic_memory_retrieval.py tests/test_episodic_history_scope.py tests/test_memory_context_sections.py tests/test_memory_promotion_service.py tests/test_coding_memory_proposals.py tests/test_legacy_memory_importer.py tests/test_memory_index_rebuild.py tests/test_memory_markdown_export.py
python -m pytest -q
python -m compileall -q memory runtime applications tools scripts
git diff --check
```

### T52：生成最终总结报告

**文件：** `memory-refactor-summary.md`
**依赖：** T51

**步骤：**
1. 汇总真实问题、修改模块、新架构和兼容方式。
2. 记录迁移、废弃接口、测试证据和未完成工作。
3. 记录剩余风险和后续建议，不隐藏失败项。

**验证：** 报告包含提示词要求的 10 个章节，并引用实际测试命令和结果。

## 执行顺序

```text
Phase 0: T01 → T02/T03/T04
                 ↓
Phase 1: T05 → T06/T07 → T08 → T09/T10 → T11 → T12 → T13
                 ↓
Phase 2: T14 → T15 → T16 → T17 → T18
          T19 → T20 ───────────────┐
          T13 + T19 → T21          │
          T11 + T19 → T22 → T23    │
          T22 → T24 → T25          │
          T21 + T24 + T25 → T26 → T27
                 ↓
Phase 3: T28 → T29
          T30 → T31 → T32
          T29 + T32 → T33
                 ↓
Phase 4: T34 → T35 → T36 → T37
          T22 + T36 → T38
          T37 + T38 → T39
                 ↓
Phase 5: T40 → T41 → T42
          T11 → T43
          T20 + T22 → T44
          T42 + T44 → T45
                 ↓
Phase 6: T39 + T41 → T46
          T25 + T43 + T46 → T47
          T33 + T39 + T44 → T48 → T49
          T27 + T33 + T39 + T44 → T50
          T47 + T49 + T50 → T51 → T52
```

首个必须完成的可运行里程碑是 T01–T27。后续阶段不能以“基础设施已搭好”为完成标准，必须分别满足对应 AC 与退出条件。
