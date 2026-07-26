# TaleClaw Memory Hierarchy

## 调查基线

- 分支：`feat/memory-runtime-evolution`
- 基础提交：`f0d5050`
- 2026-07-27 全量基线：`577 passed, 2 warnings in 34.50s`
- warning：Qdrant 依赖的 protobuf 类型面向 Python 3.14 的既有弃用提示。

## 代码事实表

| 领域 | 文件与接口 | 当前作用 | 主要调用者 | 确定性 |
|---|---|---|---|---|
| Session transcript | `runtime/sessions/session.py`, `SessionManager` | PostgreSQL 会话与消息真相 | AgentLoop、Web、Coding | CONFIRMED |
| Working memory | `runtime/working_memory.py`, `WorkingMemory` | 当前任务 checkpoint、finding、pending unit | reasoning loop、subagent | CONFIRMED |
| Context assembly | `runtime/context/builder.py`, `ContextBuilder` | 分区预算并构造模型 messages | Runtime | CONFIRMED |
| Semantic memory | `memory/domain.py`, `MemoryItem` | owner/status/version/evidence 的长期事实 | command/repository/retrieval | CONFIRMED |
| Semantic truth | `memory/postgres_repository.py` | item/evidence/outbox 持久化 | command/index sync | CONFIRMED |
| Derived index | `memory/qdrant_index.py` | semantic 与 episodic 候选索引 | retrieval/index sync | CONFIRMED |
| Candidate/pending | `MemoryStatus.CANDIDATE`, `memory/PENDING.json` | 新治理 candidate 与 legacy compatibility pending | promotion/lifecycle | CONFIRMED |
| Promotion | `memory/promotion_service.py` | 独立 evidence 和 confidence 晋升 | lifecycle/coding | CONFIRMED |
| Task-local | `applications/coding/session.py`, `memory_lifecycle.py` | Coding task 状态与结论 | CodingApplication | CONFIRMED |
| Episodic history | `memory/episodic_retrieval.py` | 当前 Session 或可信 Task/Workspace 的过去事件 | ContextRetrievalService | CONFIRMED |
| Memory injection | `runtime/context/memory.py` | semantic/working 分区渲染 | Context provider | CONFIRMED |
| Dedup/conflict | `memory/dedup.py`, `conflict_service.py` | exact/semantic duplicate 与版本冲突 | MemoryCommandService | CONFIRMED |
| Tools | `tools/tool_registry.py`, `ToolSpec` | 可见性、权限、执行与 risk | reasoning loop | CONFIRMED |
| Trace/archive | `runtime/trace/*`, `memory/archive_store.py` | run event、summary、历史审计 | Runtime/lifecycle | CONFIRMED |
| Injection planner | 无独立模块；选择分散在 retrieval/provider/builder | 当前没有统一动态裁剪 planner | ContextBuilder | NOT_FOUND |
| Relation/evolution | conflict 可识别部分关系，但无通用 pending proposal | 不自动演进旧事实 | 无 | DECLARED_ONLY |

## 当前层级

| 层级 | 内容 | 读/写者 | Prompt | 生命周期与边界 | Promotion/Audit | 自动更新 |
|---|---|---|---|---|---|---|
| Working context | 当前 system、历史、请求、工具结果 | ContextBuilder / Runtime | 直接 | 单次 reasoning step/turn | 无 promotion；Trace | 预算器可裁剪表示 |
| Recent conversation | Session messages 的预算视图 | history provider / SessionManager | 是 | 当前 Session | Trace | 不改原消息 |
| Session memory | 完整 messages、metadata、mode | AgentLoop / SessionStore | 经预算 | Session，不跨用户 | DB audit 时间 | 运行时追加 |
| Working memory | task goal、finding、pending、checkpoint | reasoning/coding/subagent | 独立分区 | 当前 task/session | checkpoint trace | 受控更新 |
| Task-local memory | Coding conclusion、artifact、task state | CodingApplication | Coding Context | Task/Workspace | proposal/promotion audit | 不自动 user-global |
| Pending candidate | candidate item 或 legacy pending | lifecycle/promotion | 默认不作为 stable 注入 | owner scope | 必须 promotion/audit | 不直接 active |
| Stable semantic | active current unexpired MemoryItem | semantic retrieval/commands | semantic 分区 | user/project/workspace/task owner | command/outbox Trace | 只经命令状态机 |
| Episodic index | event 粒度过去 turn | episodic retrieval/index | episodic 分区 | 当前 Session 或 trusted coding boundary | retrieval Trace | 派生、可重建 |
| Artifact | Coding 输出与 workspace snapshot | Coding/report | 按需 | Task/run | artifact audit | 不自动 memory |
| Trace/archive | event、report、memory archive | diagnostics | 否 | run/session/task | 本身即 audit | append/summary |

## 已实现的安全边界

1. PostgreSQL 是 semantic truth，Qdrant 是派生索引。
2. 普通 episodic retrieval 强制当前 user + Session；不跨 Session 召回原始消息。
3. inferred 与 Coding conclusion 默认 candidate/proposal，不静默写 user active。
4. owner scope 来自受信任 Session metadata，模型不能指定任意 owner。
5. superseded/revoked/expired/rejected 不进入 Context。
6. Working、Semantic、Episodic 使用不同 Context 分区与预算。

## 当前缺口

- MemoryItem 缺少统一 contextual description、keywords、tags、access stats 与关系视图。
- ToolSpec 只有 risk/visibility 等字段，没有 state/memory/context effect 的统一治理 metadata。
- Context report 有 token/section 数据，但没有统一 pressure level 与 policy hint。
- conflict/dedup 不输出通用 MemoryLink 或可审核 evolution proposal。
- legacy pending candidate enrichment 字段和 validation 不统一。
- retrieval Trace 尚不能逐候选解释 injected representation 和过滤原因。

## 目标与迁移路线

本任务以 adapter 和 feature flag 渐进扩展：MemoryNote 投影现有 MemoryItem；governance 在 candidate 前做保守 decision；relation/evolution 只产生 pending proposal；pressure 首先只观测；injection explanation 复用 TraceStore。观察期和独立审批前，不新增 schema、不自动 apply proposal、不动态删除核心 Context、不重写 stable content。
