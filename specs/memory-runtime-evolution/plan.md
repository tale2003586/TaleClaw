# Agent Memory Runtime Evolution Plan

## 架构

扩展现有依赖方向：`memory.domain` → governance/relation/enrichment services → lifecycle/context/tool adapters → Trace。PostgreSQL `MemoryItem` 仍是 stable semantic truth；MemoryNote 是兼容视图，proposal/audit 初期为结构化运行时对象与 Trace，不要求 schema migration。

## 模块

- `memory/notes.py`：MemoryNote、MemoryLink 与 legacy adapter。
- `memory/governance.py`：write request/candidate/classification/policy/result/audit、detector protocol、保守 pipeline。
- `memory/evolution.py`：RelationDecider、MemoryEvolutionProposal，永不自动 apply。
- `memory/enrichment.py`：PendingMemoryEnricher、validation、fallback。
- `runtime/context/pressure.py`：usage/snapshot/level/hint 与纯 evaluator。
- `runtime/trace/memory_injection.py`：检索和注入 explanation 数据结构与安全 preview/digest。
- `tools/tool_registry.py`：ToolGovernanceMetadata 作为 ToolSpec 内部 metadata，不注入模型 schema。
- `runtime/trace/summary.py`：聚合治理、pressure、proposal、injection 事件。

## 接入

1. governance 先接 candidate 进入 legacy pending 前的观测/decision；flag 关闭保持旧行为。
2. pressure 在 Context report 完成后只生成 Trace event；不裁剪 prompt。
3. relation/enrichment 接 pending proposal 路径，默认关闭。
4. injection explanation 从现有 semantic retrieval trace 与 Context report 派生；不改变 selection。

## Feature flags

- `MEMORY_GOVERNANCE_ENABLED=0`
- `MEMORY_RELATION_PROPOSALS_ENABLED=0`
- `MEMORY_PENDING_ENRICHMENT_ENABLED=0`
- `CONTEXT_PRESSURE_OBSERVATION_ENABLED=0`
- `MEMORY_INJECTION_TRACE_ENABLED=0`

## 决策

- 不实施自动 evolution 或 stable rewrite。
- Stage 9 仅保留 policy hint，不接动态裁剪，除非后续证据证明 prompt 不变性。
- 所有 LLM adapter 以 Protocol 表示，单测只用 deterministic fake。
