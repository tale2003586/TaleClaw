# Memory Architecture

TaleClaw has one optional durable-memory system. Conversation history belongs
to Session, task progress belongs to TaskState, and compaction state belongs to
ContextSnapshot. None of those are long-term memory.

## Runtime Flow

```text
explicit user request / verified coding conclusion
    -> MemoryCommandService
    -> MemoryRepository (authoritative PostgreSQL data)
    -> outbox
    -> MemoryIndexSynchronizer
    -> semantic index (derived and rebuildable)

current request
    -> SemanticMemoryRetrievalService
    -> repository authorization and status checks
    -> ContextMemoryService
    -> selected <semantic_memory> context
```

The feature is optional. With all `SEMANTIC_MEMORY_*` flags off, bootstrap does
not construct a repository, index, command service, or retriever. Chat and
Coding continue without a memory context block.

## Ownership

| Concern | Owner |
|---|---|
| Durable item schema and evidence | `memory/domain.py` |
| Authoritative persistence | `memory/repository.py`, `memory/postgres_repository.py` |
| Controlled writes and transitions | `memory/command_service.py` |
| Retrieval and context selection | `memory/semantic_retrieval.py` |
| Derived lookup index | `memory/semantic_index.py`, `memory/qdrant_index.py` |
| Index reconciliation | `memory/index_sync.py` |
| Legacy import | `memory/migration/` |

There is no per-turn candidate/governance/enrichment/archive lifecycle and no
task-local Markdown memory. Verified Coding conclusions may be written directly
through `MemoryCommandService`; unverified conclusions remain task artifacts.

## Legacy Data

`memory/migration/legacy_importer.py` reads old `MEMORY.md`, `PENDING.*`,
`SELF.md`, `NOW.md`, `HISTORY.md`, and `RECENT_CONTEXT.*` files. It does not make
those files a current source of truth. `legacy_files.py` is a read-only adapter
for the Web legacy-memory view and can be removed after all user directories are
imported and that view is retired.

No normal Runtime path writes legacy Markdown or JSON memory files.


## 运行时数据边界与退役条件（2026-10-03）

| 数据 | 权威来源 | 当前用途与边界 |
|---|---|---|
| 当前目标、约束、进展 | TaskState | 同一任务的语义状态；不自动变为长期记忆 |
| 原始交互及工具事实 | Session events | 审计、恢复与上下文选择 |
| 完整工具输出/大文件 | Artifact / 配置的 result store | 精确证据；不是用户偏好库 |
| 跨任务已验证事实 | PostgreSQL MemoryRepository | 通过 command service 写入 |
| 语义向量 | 可重建 MemoryIndex | 只返回候选；正文、owner、状态、版本仍由 DB 校验 |
| episodic history | 独立历史索引 | task/session/user 边界下召回历史片段，不冒充正式事实 |
| 安全知识库 | 应用级知识索引 | 自动提示与显式检索工具共享客户端，独立于用户记忆 |
| 旧 Markdown 文件 | migration/read-only Web adapter | 迁移输入及旧视图，不参与正常 Runtime 新写入 |

删除了没有生产消费者的 Markdown 全文件去重入口、旧 memory scope helper，以及
传入但从未读取的 `history_scope_resolver`。真实的历史 scope 和 importer 仍保留；
旧 `CONTEXT_RETRIEVED_HISTORY_*` 已不控制生产检索，示例配置移除，当前使用
`CONTEXT_EPISODIC_HISTORY_*`。

### 索引失效必须显式可见

索引禁用或初始化失败所返回的 NullSemanticMemoryIndex，search/upsert/delete
会显式报告不可用。检索使用既有、受 owner 边界限制的 DB lexical fallback，
并标 degraded；outbox 同步进入既有 retry，rebuild 报 failed。不会把未写入向量库
的事件标为 completed。关闭全部 memory 功能仍不构造这些组件；仅关闭向量索引
并不意味着关闭已启用的 DB 记忆。

已被旧实现误标 completed 的记录不会自动重排；需要通过已有 rebuild 流程修复。
初始化时降级成 Null 的实例，在依赖恢复后需要重新装配。当前 outbox processing
没有新增租约回收，本次也未进行真实数据库重建或数据清理。

### RAG 实例与结果缓存

Bootstrap 将已有的安全知识 index 注入 SecurityRagPlugin。自动提示与显式工具
共用本应用的客户端/embedding 实例；显式 `security_rag_search` 传
`use_cache=False`，保证它不会因共享实例而开始返回旧缓存结果。独立启用工具、
没有注入 index 时保留 factory fallback。没有进程级、跨租户全局单例。

自动安全知识的本轮快照由 RunExecutionState 管理，见
[上下文与缓存合同](TASK_STATE_CONTEXT_ARCHITECTURE.md)。语义用户记忆不进入
这个缓存，其权限和版本检查仍在每次检索执行。

### 尚不能当死代码删除的部分

- 旧 memory 文件的 importer 和只读 Web adapter：先完成用户目录迁移并退役旧视图。
- 旧 TaskState 输入适配：先证明受支持会话均已迁移或过期。
- file/PostgreSQL/artifact 工具结果后端：当前读取只查所配置后端；需要引用路由或
  明确迁移清单，不能切换默认后端就删除旧读器。
- 历史向量读取：生产写入闭环目前缺失，但 benchmark 与既有 collection 仍是
  消费者；需明确停用/回填策略后再退役。
- artifact GC、tool-result TTL、completed outbox 清理：先建立引用存活与保留合同。
  本轮不删除历史产物、用户记忆、已有索引或数据库记录。

这些属于仍有消费者的数据迁移边界；新运行不能继续添加第二套长期记忆权威。
