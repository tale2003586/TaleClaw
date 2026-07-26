# TaleClaw 长期记忆系统渐进式重构 Plan

## 设计依据

本计划匹配当前仓库，而非机械套用提示词中的目录：

- `memory/` 当前是平铺模块，`MemoryStore` 管理 Markdown/JSON。
- `MemoryLifecycle` 每轮追加 `HISTORY.md`、更新 `RECENT_CONTEXT.*`、写 `PENDING.json`，并把 Session turn 与五份整文件写入同一个 Qdrant collection。
- `ContextMemoryService` 使用 Markdown `recall()`，会混合 SELF、MEMORY、NOW、PENDING 和 HISTORY。
- `ContextRetrievalService` 只按单一 scope 检索；普通 Session 使用用户级 scope。
- `TaskMemoryPromoter` 把 Coding Conclusion 写入全局 `PENDING.md`，与普通候选的 `PENDING.json` 不统一。
- `memorize` 与 `recall_memory` 直接调用 `MemoryStore`。
- PostgreSQL 由各 Store 的幂等 `_init_schema()` 管理，没有 Alembic。
- `MemoryArchiveStore` 使用 PostgreSQL，但只保存 recent window 淘汰的历史 turn。
- 当前没有 `applications/minecraft/`。

因此先在现有平铺结构增加清晰端口和适配器，避免一次性搬迁全部模块。

## 架构概览

### 领域层

新增独立于存储和模型的长期语义记忆对象、枚举和状态转换。领域层验证作用域、类型、状态、版本和有效时间，不依赖 PostgreSQL、Qdrant、Markdown 或 Coding。

### 应用服务层

- `MemoryCommandService`：显式记忆、proposal、确认、拒绝、更新、替代、撤销和遗忘。
- `MemoryConflictService`：确定性去重、证据合并、冲突和版本替代。
- `MemoryPromotionService`：普通候选与 Coding Conclusion 的统一晋升策略。
- `SemanticMemoryRetrievalService`：Qdrant 候选召回、PostgreSQL 回源验证、排序和预算。
- `EpisodicHistoryRetrievalService`：Session/Task 历史事件检索，不读取长期记忆表。

### 基础设施层

- PostgreSQL Repository 保存 MemoryItem、Evidence、SourceRef 和 Index Outbox。
- Qdrant Semantic Index 以单条 MemoryItem 版本为粒度。
- 现有 history collection 过渡期服务 Session Event，查询增加可信边界过滤。
- Markdown Exporter 从 PostgreSQL 生成只读视图；旧 `MemoryStore` 仅作为 legacy adapter。

### 一致性模型

长期状态先在 PostgreSQL 事务中提交，并在同一事务写入 outbox。事务结束后由后台 synchronizer 更新 Qdrant。检索命中始终回源 PostgreSQL，陈旧向量不能直接进入 Context。

### Context 边界

```xml
<semantic_memory>当前有效的长期事实</semantic_memory>
<episodic_history>当前 Session 或当前 Task 的相关历史事件</episodic_history>
<working_memory>当前任务状态</working_memory>
```

普通聊天的 Episodic History 强制当前 Session；跨 Session 只共享 Semantic Memory。

## 核心领域结构

### 枚举

```python
class MemoryOwnerScope(StrEnum):
    USER = "user"
    PROJECT = "project"
    APPLICATION = "application"
    WORKSPACE = "workspace"
    TASK = "task"

class MemoryKind(StrEnum):
    PREFERENCE = "preference"
    FACT = "fact"
    DECISION = "decision"
    PROCEDURE = "procedure"
    CONSTRAINT = "constraint"
    RELATIONSHIP = "relationship"

class MemoryStatus(StrEnum):
    CANDIDATE = "candidate"
    ACTIVE = "active"
    SUPERSEDED = "superseded"
    REVOKED = "revoked"
    EXPIRED = "expired"
    REJECTED = "rejected"

class MemorySourceType(StrEnum):
    EXPLICIT_USER = "explicit_user"
    INFERRED = "inferred"
    CODING_CONCLUSION = "coding_conclusion"
    LEGACY_IMPORT = "legacy_import"
```

### MemoryItem

```python
@dataclass(frozen=True)
class MemoryItem:
    id: str
    owner_scope: MemoryOwnerScope
    owner_id: str
    kind: MemoryKind
    content: str
    normalized_content: str
    status: MemoryStatus
    confidence: float
    salience: float
    valid_from: datetime
    valid_until: datetime | None
    last_confirmed_at: datetime | None
    supersedes_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime
```

### MemoryEvidence

```python
@dataclass(frozen=True)
class MemoryEvidence:
    id: str
    memory_id: str
    source_type: MemorySourceType
    source_ref: str
    session_id: str | None
    task_id: str | None
    workspace_id: str | None
    project_id: str | None
    excerpt: str
    metadata: dict[str, Any]
    created_at: datetime
```

Evidence excerpt 限长；完整事实留在 Session Store 或 Task Artifact。

### MemoryContext

```python
@dataclass(frozen=True)
class MemoryContext:
    user_id: str
    session_id: str
    application: str | None = None
    workspace_id: str | None = None
    project_id: str | None = None
    task_id: str | None = None
```

它只能由服务端 Session、Workspace Resolver 和 Application 构建。

### MemoryWriteProposal

```python
@dataclass(frozen=True)
class MemoryWriteProposal:
    content: str
    kind: MemoryKind
    owner_scope: MemoryOwnerScope
    owner_id: str
    source_type: MemorySourceType
    evidence: tuple[MemoryEvidence, ...]
    confidence: float
    salience: float
    explicit_user_request: bool = False
```

## 核心端口

### MemoryRepository

```python
class MemoryRepository(Protocol):
    def create(self, item: MemoryItem, evidence: Sequence[MemoryEvidence]) -> MemoryItem: ...
    def get(self, memory_id: str) -> MemoryItem | None: ...
    def get_many(self, memory_ids: Sequence[str]) -> list[MemoryItem]: ...
    def list_active(self, scopes: Sequence[OwnerKey], now: datetime) -> list[MemoryItem]: ...
    def find_exact(self, owner: OwnerKey, kind: MemoryKind, normalized: str) -> MemoryItem | None: ...
    def transition(self, command: MemoryTransition, expected_version: int) -> MemoryItem: ...
    def add_evidence(self, memory_id: str, evidence: Sequence[MemoryEvidence]) -> MemoryItem: ...
```

### SemanticMemoryIndex

```python
class SemanticMemoryIndex(Protocol):
    def upsert(self, item: MemoryItem) -> None: ...
    def delete(self, memory_id: str, version: int | None = None) -> None: ...
    def search(self, query: str, scopes: Sequence[OwnerKey], top_k: int) -> list[IndexedMemoryHit]: ...
```

### EpisodicHistoryIndex

```python
class EpisodicHistoryIndex(Protocol):
    def upsert_event(self, event: EpisodicEvent) -> None: ...
    def search(self, query: str, boundary: EpisodicBoundary, top_k: int) -> list[EpisodicHit]: ...
```

普通 `EpisodicBoundary` 必须包含 `session_id`；Coding 可以包含 task/workspace/project，禁止只有 user scope。

## PostgreSQL 设计

沿用 `runtime.db` 和幂等初始化模式，新增 `PostgresMemoryRepository` 与模块 schema version。

### memory_items

| 字段 | 约束 | 用途 |
|---|---|---|
| id | TEXT PK | 稳定 ID |
| owner_scope/owner_id | CHECK + NOT NULL | 可信作用域 |
| kind | CHECK | 受控类型 |
| content/normalized_content | NOT NULL | 内容与去重键 |
| status | CHECK | 生命周期 |
| confidence/salience | 0..1 | 排序与治理 |
| valid_from/valid_until | TIMESTAMPTZ | 有效期 |
| last_confirmed_at | TIMESTAMPTZ | 最近确认 |
| supersedes_id | FK nullable | 版本链 |
| version | INTEGER | 乐观锁 |
| created_at/updated_at | TIMESTAMPTZ | 审计 |

索引：

- `(owner_scope, owner_id, status)`：有效查询。
- `(owner_scope, owner_id, kind, normalized_content)`：精确去重。
- `supersedes_id`：版本链。
- `valid_until`：过期扫描。

### memory_evidence

保存 memory ID、source type/ref、session/task/workspace/project 引用、限长 excerpt、JSON metadata 和时间。保留 evidence，避免状态记录失去来源。

### memory_index_outbox

保存事件 ID、memory ID、目标版本、操作、状态、attempt count、next attempt、last error 和时间。唯一约束防止同一版本重复调度相同操作。

### memory_schema_versions

记录模块 schema。第一阶段 downgrade 只停用装配，不自动删除已有数据表。

## 应用服务

### MemoryCommandService

```python
class MemoryCommandService:
    def remember(self, proposal: MemoryWriteProposal, context: MemoryContext) -> MemoryItem: ...
    def propose(self, proposal: MemoryWriteProposal, context: MemoryContext) -> MemoryItem: ...
    def confirm(self, memory_id: str, context: MemoryContext) -> MemoryItem: ...
    def reject(self, memory_id: str, reason: str, context: MemoryContext) -> MemoryItem: ...
    def update(self, memory_id: str, content: str, context: MemoryContext) -> MemoryItem: ...
    def revoke(self, memory_id: str, reason: str, context: MemoryContext) -> MemoryItem: ...
    def forget(self, query: str, context: MemoryContext) -> list[MemoryItem]: ...
```

先校验 caller context 与 owner，再执行 conflict policy。状态变化和 outbox 同事务提交。

### MemoryConflictService

1. 规范化并查找相同 owner + kind 的精确重复。
2. 精确重复只合并证据与确认时间。
3. 对同作用域、同 kind 的有限候选检查语义重复。
4. 明确冲突创建新版本并 supersede 旧 active。
5. 不同 owner scope 不互相替代。

确定性规则优先；无法可靠判断时保留 candidate 或要求确认。

### MemoryPromotionService

普通候选与 Coding Conclusion 共用入口。Policy 检查：

- 是否来自独立 Session/Task 证据。
- 是否存在否定或修正。
- 作用域是否可信且具体。
- 是否稳定、可复用、非执行噪音。
- confidence、evidence count、salience 是否达标。
- 是否需要用户确认。

旧 `MemoryProcessingDevice` 与 `TaskMemoryPromoter` 过渡期只生成 proposal，不直接写 Markdown。

### SemanticMemoryRetrievalService

```text
query + trusted MemoryContext
→ 允许的 OwnerKey
→ Qdrant 返回 memory_id/version/score
→ PostgreSQL 批量回源
→ active/current/valid/scope 过滤
→ relevance × confidence × freshness × salience
→ 去重与预算
→ SemanticMemoryResult
```

Qdrant 不可用时只使用 PostgreSQL 受限降级，不信任旧索引 payload。

## Qdrant 与一致性

### Collection

- `taleclaw_semantic_memory_v1`：单条 active MemoryItem 版本。
- 现有 `taleclaw_history`：过渡期保留 Session Event，后续重建为 episodic collection。

Semantic payload 只保存：

```text
memory_id, memory_version, owner_scope, owner_id, kind, status,
valid_until, content_digest, indexed_at
```

### Outbox

```text
PostgreSQL transaction
  ├─ memory state change
  └─ outbox pending
        ↓ commit
BackgroundIndexSynchronizer
        ↓
Qdrant upsert/delete
        ↓
outbox completed 或 retry_scheduled
```

替代、撤销和过期删除旧 active point。重建使用新 collection/version alias，验证后切换。

## Episodic History

Session Store 继续作为原始消息真源。Qdrant 以 turn/event 为粒度，payload 包含 user、session、application、workspace、project、task 和 source type。

过滤规则：

- 普通聊天必须 `user_id + session_id` 同时匹配。
- Coding 必须匹配 task，或由明确 workspace/project policy 授权。
- Context Builder 禁止仅按 user 检索跨 Session turn。
- 旧用户级 points 在重建前保留，但新查询条件使其无法越界注入。

`HISTORY.md`、`RECENT_CONTEXT.*` 和 `memory_archive` 不作为 Semantic Memory 来源。

## Context Builder 集成

- 新增 `SemanticMemoryContextProvider`，输出 `<semantic_memory>`。
- 新增 `EpisodicHistoryContextProvider`，输出 `<episodic_history>`。
- 旧 `MemoryContextProvider` 在兼容阶段读取 legacy Markdown，切换后退役。
- Working Memory 与 Coding Context Provider 保持不变。

Context report 分别记录命中数、丢弃原因、scope、状态过滤、raw/rendered chars 和 token 占比。

## 写入入口迁移

### memorize

```text
memorize
→ trusted MemoryContext
→ explicit MemoryWriteProposal
→ MemoryCommandService.remember
→ PostgreSQL + outbox
→ Trace
```

普通工具不再接受任意文件分区。旧 `self/now/pending` 参数映射到受控行为或拒绝，并输出 deprecation 信息。

### 自动候选

`MemoryLifecycle` 继续 after-turn 调度，但不再把历史文件作为长期真源。`MemoryProcessingDevice` 生成 inferred proposal，统一服务创建 candidate。候选不会作为其他 Session 的 Episodic History 注入。

### Coding Conclusion

`TaskConclusionExtractor` 继续提取结构化结论；`TaskMemoryPromoter` 改成 proposal adapter：

- Workspace Resolver 提供可信 workspace/repository。
- 记录 commit 或可用代码版本。
- evidence 记录 task、文件、位置和 category。
- 默认使用 workspace/project scope，不写用户全局。
- 未验证或低置信度结论保持 candidate。

## Markdown 与旧文件

| 旧文件 | 目标处置 |
|---|---|
| `MEMORY.md` | 迁移后由 active memories 生成只读导出 |
| `SELF.md` | Agent 自设转移到 `.agent`；仅人工复核，不自动导入用户事实 |
| `NOW.md` | 普通会话停用；Coding task-local 暂留兼容，后续由 Working Memory 取代 |
| `PENDING.json` | 导入 candidate；迁移期 legacy adapter 只读 |
| `PENDING.md` | 不作真源；漂移内容只进入人工复核报告 |
| `HISTORY.md` | 不导入 Semantic Memory；只读归档后停止维护 |
| `RECENT_CONTEXT.*` | 不导入 Semantic Memory；普通会话停止重复写入 |

Exporter 原子生成 Markdown，并标注 generated/read-only。业务代码不读取导出来决定状态。

## 迁移与重建

### LegacyMemoryImporter

支持 `--dry-run`、`--user-id`、`--source-root`、`--include-candidates`、`--report-path` 和 checkpoint resume。

映射规则：

- `MEMORY.md` bullet → user-scoped candidate 或经确认的 active。
- `PENDING.json` → candidate，保留 confidence、evidence count、source refs。
- `PENDING.md` → 与 JSON 对照；漂移项人工复核，不自动 active。
- `SELF.md/NOW.md` → 默认 skip + review。
- `HISTORY.md/RECENT_CONTEXT.*` → episodic legacy report，不写 active memory。
- Coding pending/conclusion → workspace/task scoped candidate。

幂等键由 source type/ref、owner、kind 和 normalized content 组成。报告输出 imported、skipped、duplicate、conflict、failed。

### RebuildSemanticMemoryIndex

从 PostgreSQL 读取 active、未过期、当前版本，批量写入新 collection，校验数量、版本和 digest。支持 dry-run、限定 owner 和失败重试。

## Trace 与指标

沿用点分命名：

```text
memory.candidate.created
memory.item.promoted
memory.item.confirmed
memory.item.rejected
memory.item.updated
memory.item.superseded
memory.item.revoked
memory.item.expired
memory.index.scheduled
memory.index.completed
memory.index.failed
memory.semantic.retrieved
memory.episodic.retrieved
memory.context.dropped
```

`runtime/trace/summary.py` 聚合 Spec 指标。内容只记录 digest、限长 preview 或 source ref。

## 文件组织

```text
memory/
├── domain.py
├── commands.py
├── repository.py
├── postgres_repository.py
├── command_service.py
├── conflict_service.py
├── promotion_service.py
├── semantic_retrieval.py
├── episodic_retrieval.py
├── semantic_index.py
├── index_sync.py
├── markdown_exporter.py
└── migration/
    ├── __init__.py
    ├── legacy_importer.py
    └── rebuild_index.py

scripts/
├── migrate_long_term_memory.py
└── rebuild_memory_index.py

docs/refactors/memory-system-audit.md
docs/architecture/MEMORY_ARCHITECTURE.md
docs/migrations/long-term-memory-migration.md
```

主要修改：

- `runtime/bootstrap.py`：装配 Repository、服务、Provider 和 synchronizer。
- `runtime/context/providers.py`、`retrieval.py`、`builder.py`、`budget.py`：语义/历史分区。
- `memory/lifecycle.py`、`processor.py`、`store.py`：proposal/legacy adapter，停止整文件索引。
- `memory/qdrant_index.py`、`vector_index.py`、`vector_runtime.py`：拆分 semantic/episodic contract。
- `tools/handlers.py`、`tools/schema.py`：统一命令和兼容提示。
- `applications/coding/conclusions.py`、`promotion.py`、`runner.py`：Coding proposal 与可信作用域。
- `runtime/trace/summary.py`：新增事件与指标。

## 分阶段实施

### Phase 0：审计与保护性测试

1. 生成真实数据流审计报告。
2. 补显式记忆、候选晋升、Coding promotion、Context 和 Qdrant 基线测试。
3. 增加“新 Session 不读取其他 Session turn”的失败复现。
4. 记录旧文件和索引规模，不修改数据。

退出条件：审计完整；基线可重复；跨 Session 问题被测试捕获。

### Phase 1：领域、Repository 与 Outbox

1. 领域枚举、模型、状态规则和测试。
2. PostgreSQL schema、Repository 和乐观锁。
3. Evidence、SourceRef 和 Outbox。
4. Fake Repository/Index。
5. 旧运行链路保持不变。

退出条件：领域与 Repository 测试通过；数据库升级幂等。

### Phase 2：显式记忆垂直切片

1. remember、update、revoke、forget。
2. `memorize` 改为 PostgreSQL 主写。
3. Semantic Qdrant index 与 outbox drain。
4. Semantic retriever 回源验证。
5. Context semantic provider。
6. Markdown 单向兼容导出。

退出条件：AC1、AC2、AC3、AC10、AC14 通过。

### Phase 3：候选与 Coding 统一

1. 普通 inferred memory 转 proposal/candidate。
2. 迁移证据累计和 promotion policy。
3. Coding Conclusion 使用 workspace/project proposal。
4. 停止全局 `PENDING.md` 主写。

退出条件：AC4、AC5、AC6 通过。

### Phase 4：Semantic/Episodic 分离

1. Episodic index 增加可信边界过滤。
2. 普通 Session 强制当前 session ID。
3. Context 标签和 report 分离。
4. 停止整份 Markdown Qdrant 索引。

退出条件：AC7、AC8、AC9、AC13 通过；跨作用域泄漏为零。

### Phase 5：迁移、导出与重建

1. Importer dry-run 与报告。
2. 人工确认后导入。
3. 重建 semantic/episodic collection。
4. 启用 Markdown exporter。
5. 验证 PostgreSQL、Qdrant、导出数量和版本。

退出条件：AC11、AC12 通过；重复执行无重复项。

### Phase 6：旧链路退役

1. 停止普通会话写 HISTORY 与 RECENT_CONTEXT。
2. 标记 MemoryStore 长期写入和旧 section 参数 deprecated。
3. 清理无调用方的整文件索引。
4. 旧数据只读归档；永久删除另行审批。
5. 完成架构、迁移和总结文档。

退出条件：旧写入无生产调用；兼容测试和回滚说明验证通过。

## Spec 覆盖矩阵

| Spec | Plan 归属 |
|---|---|
| F1–F6 | 核心领域结构、Repository、PostgreSQL 设计 |
| F7–F15 | MemoryCommandService、ConflictService、PromotionService、写入入口迁移 |
| F16–F22 | Semantic Retrieval、Episodic History、Context Builder 集成 |
| F23–F26 | Qdrant collection、Outbox、索引重建 |
| F27–F31 | Markdown 与旧文件、Legacy Importer、分阶段迁移 |
| F32–F34 | Trace 与指标、Context report、测试策略 |

依赖方向保持为 `Domain ← Application Services ← Infrastructure Adapters/Runtime Composition`，没有领域层反向依赖基础设施的设计缺口。

## 测试策略

### 领域

- candidate → active、active → superseded/revoked/expired。
- 不同 scope 不冲突，相同事实合并证据。
- 非法转换、越权作用域和无效时间被拒绝。

### PostgreSQL

- CRUD、active query、evidence、version chain。
- expected version 并发冲突。
- memory + outbox 原子提交。
- 初始化和升级重复执行。

### Qdrant 与 Outbox

- create/update/supersede/revoke/expire/rebuild。
- 失败重试与陈旧 point 回源过滤。
- Fake Adapter 进入常规 CI；真实服务进入可选集成配置。

### Context

- semantic/episodic/working 三分区。
- 新 Session 不注入其他 Session turn。
- 非 active 与越界记忆不注入。
- project/workspace 隔离、去重、预算和 drop reason。

### Coding

- Conclusion 生成统一 candidate。
- workspace/repository/task/evidence 正确。
- Task-local 不直接写用户 active。
- 低置信度不自动 active。

### 迁移与兼容

- MEMORY/PENDING 映射，HISTORY/RECENT 跳过。
- dry-run 零写入、幂等、checkpoint 恢复。
- 聊天、Session 恢复、Coding、Working Memory、Security RAG 回归。

## 回滚策略

- Phase 1 只新增表和模块；回滚为停止装配，不删表。
- Phase 2–4 用 feature flag 控制新写入、新检索和 Context provider；回滚切 legacy reader，但不反向覆盖 PostgreSQL。
- Outbox 可暂停，恢复后继续同步。
- Importer 保留源文件与报告，可按 batch/import ID 撤销导入状态。
- Qdrant 重建使用新 collection/version alias，验证后切换，失败保留旧 alias。
- 永久删除 legacy 文件、表或 collection 必须单独审批。

## 技术决策

| 决策点 | 选择 | 理由 |
|---|---|---|
| 权威存储 | PostgreSQL | 支持事务、状态、版本、关系和审计 |
| 向量存储 | Qdrant 派生索引 | 可重建，不承担事实一致性 |
| 索引一致性 | PostgreSQL outbox | 数据库事务不等待远程服务 |
| 模块布局 | 先沿用 `memory/` 平铺 | 降低导入和移动回归 |
| 删除语义 | 逻辑 revoke | 保留审计和版本关系 |
| 冲突判断 | 确定性优先、语义辅助 | 避免模型随意改写事实 |
| 普通历史边界 | 当前 Session | 跨 Session 只共享长期记忆 |
| Coding 作用域 | workspace/project 优先 | 项目事实不污染用户全局 |
| Markdown | 只读生成视图 | 保留可审计性，取消双真源 |
| 数据库升级 | 幂等 init + 显式迁移文档 | 符合当前仓库实践 |
