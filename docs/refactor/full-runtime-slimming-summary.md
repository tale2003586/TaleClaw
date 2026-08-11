# Full Runtime Slimming Summary

> Historical Phase 1 report. Compatibility and P2 items described below were
> resolved by the later aggressive pruning work; see
> `aggressive-runtime-pruning-summary.md` for the current result.

## 1. Baseline

- Start HEAD: `03a15802c4655df78d214ef70d5c80faade12b6c`
- Baseline: 84,948 tracked Python LOC; 694 tests passed.
- Final: 84,554 tracked Python LOC; 689 tests passed. Five tests were deleted with the dead evolution/link feature; every remaining test passes.
- Benchmark method: real offline `Runtime.run` requests captured by `ScriptedModel`, TaleClaw token estimator, plus 20-iteration `runtime_phase0.py` timings.
- Network TTFT was not measured because provider/network variance would make it non-reproducible.

## 2. Memory Architecture Before

```text
Session transcript
  -> MemoryLifecycle (always constructed)
     -> summary + legacy history/recent files
     -> archive Postgres store
     -> history vector index
     -> candidate processor -> governance/enrichment
     -> optional semantic command/promotion

ContextBuilder -> MemoryContextProvider -> ContextMemoryService
  -> semantic repository/index when enabled
  -> otherwise legacy scoped Markdown store

memorize / recall_memory -> semantic services or legacy compatibility store

RelationDecider -> proposal -> no caller, persistence, or consumer
```

The repository default disabled semantic memory but still constructed lifecycle, archive, candidate and background components. Relation/evolution existed only as tested design code.

## 3. Memory Architecture After

```text
Session History                 authoritative conversation state
TaskState                       authoritative optional task progress
ContextSnapshot                 compaction summary/boundary only
LongTermMemoryRepository        authoritative optional semantic durable memory
Vector index                    derived retrieval index only
Legacy scoped Markdown adapter  old-data/task-local compatibility only
MemoryContextProvider           sole context injection entry
```

The post-turn lifecycle is built only with `MEMORY_LIFECYCLE_ENABLED=1`. Semantic repository, retrieval and index services remain independently controlled by their existing semantic flags.

## 4. Deleted Memory Features

- Deleted `memory/evolution.py`: 219 production LOC plus 111 test LOC. It generated proposals but had no production caller, persistence, retrieval effect, update effect or consumer.
- Deleted `MemoryLink` and three relation enums from `memory/notes.py`: no production caller or persisted link model.
- Deleted stale `WORKING_MEMORY_*` example variables: no production reader or writer exists.
- Kept the historical `memory.evolution.proposed` trace renderer so old traces remain readable.

## 5. Disabled Memory Features

- Post-turn summary/candidate/archive/vector processing is now disabled by default. Enable with `MEMORY_LIFECYCLE_ENABLED=1`; background execution remains controlled by `MEMORY_LIFECYCLE_BACKGROUND` inside that boundary.
- Governance and enrichment remain off unless their existing flags are enabled.
- Semantic memory remains off by default and is enabled through `SEMANTIC_MEMORY_*` flags.
- No persisted user memory was removed or migrated.

## 6. WorkingMemory / TaskState / CodingContextState

| Concern | Final owner | Result |
|---|---|---|
| objective/progress/remaining/blockers/evidence | TaskState | one current writer |
| compaction summary and covered boundary | ContextSnapshot/CodingContextState projection | not task progress |
| recent active evidence | session events/context rebuild | rebuildable |
| recovery | checkpoints + TaskState version/checksum | durable |
| old WorkingMemory payload | migration reader only | no runtime subsystem |

WorkingMemory has no remaining runtime value. Its only justified code is the one-time compatibility reader; deleting it requires a persisted-data decision.

## 7. Tool Surface Before / After

| Agent | Registered before/after | Visible before -> after | Schema bytes | Tool tokens |
|---|---:|---:|---:|---:|
| Chat | 14 / 14 | 14 -> 10 | 8,221 -> 6,308 | 2,285 -> 1,758 |
| Coding | 49 / 49 | 36 -> 32 | 27,455 -> 25,542 | 7,661 -> 7,133 |
| Explore subagent | restricted / restricted | 15 -> 15 | 15,300 -> 15,300 | 4,311 -> 4,311 |

`memorize`, `recall_memory`, `load_skill` and `retrieve_tool_result` are deferred and can be unlocked for the turn through `tool_search`. Established Chat artifact, storage/sandbox and TaskState capabilities remain visible because the full behavior suite treats them as current product behavior.

The catalog now lists only deferred names. Visible tool descriptions are no longer repeated in both prompt text and provider schemas.

## 8. Prompt Surface Before / After

| Scenario | System tokens | Context tokens | Context + tools |
|---|---:|---:|---:|
| Chat | 913 -> 780 | 1,541 -> 850 | 3,826 -> 2,608 (-31.8%) |
| Coding | 3,319 -> 3,184 | 5,455 -> 3,350 | 13,116 -> 10,483 (-20.1%) |
| Explore subagent | 3,787 -> 3,654 | 4,702 -> 3,783 | 9,013 -> 8,094 (-10.2%) |

Removed global guidance duplicated by AgentSpec/tool schemas: memory tool use, deferred tool mechanics, and unconditional security-RAG instructions. Structured context/provider ownership is unchanged.

## 9. Runtime Compatibility Removed

- Removed unused `CHILD_TOOLS` and `PARENT_TOOLS` aliases.
- Subagents now derive from `Runtime.fork(tools=restricted_registry, ...)` instead of manually reconstructing provider, model, executor, pool, reflection and limits.
- Preserved `AgentSpec.from_profile`, coding fixture fallback construction and legacy TaskState/session migration because current callers/tests or persisted data still depend on them.

## 10. Code Reduction

| Area | Before -> after | Delta |
|---|---:|---:|
| Total tracked Python | 84,948 -> 84,554 | -394 |
| Production Python (total minus tests) | 63,951 -> 63,653 | -298 |
| Runtime | 18,215 -> 18,212 | -3 |
| Memory | 6,153 -> 5,877 | -276 |
| Tools | 6,149 -> 6,133 | -16 |
| Agents | 2,069 -> 2,062 | -7 |
| Tests | 20,997 -> 20,901 | -96 |

Across all tracked files including the required reports: 379 insertions, 539 deletions, net -160.

## 11. Test Results

- Baseline: `694 passed in 28.41s`.
- Final: `689 passed in 28.64s`; no failures, five deleted tests belonged exclusively to deleted dead code.
- Targeted suites passed after every behavior change (30 memory, 52 tools/context, 32 lifecycle/bootstrap, 28 subagent/runtime, and 42 established Chat capability tests).
- Offline timing medians: Chat no-tool 1.242 -> 1.058 ms; Chat context 0.385 -> 0.361 ms; Coding context 2.670 -> 2.396 ms. Explore subagent 5.896 -> 5.923 ms is effectively unchanged.

## 12. P1 Remaining

- Make AgentSpec `ToolSet.allow/deny` affect the registry view; today mode remains the effective capability gate.
- Split optional plugin/coding imports further. Construction is gated, but importing bootstrap still transitively imports several memory modules through handlers/applications.
- Reduce Coding's 32 visible tools around proven inspect/search/edit/shell primitives without breaking explicit product workflows.
- Rename `base_pipeline`/`pipeline` variables to Runtime after compatibility fixtures are updated; names no longer denote a separate abstraction.
- Break down the 2,077-line ReasoningLoop only where existing services already own behavior.

## 13. P2 Architecture Questions

- Can legacy Markdown durable memory become a read-only migration adapter without orphaning existing per-user/task data?
- When can old `working_memory` and `coding_context_state` metadata be removed from persisted sessions?
- Should ordinary Chat lose TaskState and storage/sandbox schemas entirely, or are those product-level Chat capabilities?
- Can profile compatibility (`AgentSpec.profile`, `from_profile`) be removed without changing external callers?
- Should task completion semantics and the rich coding TaskState projection be simplified? This requires a schema/behavior decision, not an overnight rewrite.

## Required Answers

1. TaleClaw has four semantic categories: conversation state, TaskState, rebuildable active context, and optional durable long-term memory. Only the last is Long-term Memory.
2. `LongTermMemoryRepository`, concretely `PostgresMemoryRepository`, is the semantic Long-term Memory source of truth. The vector store is an index.
3. WorkingMemory has no independent runtime value. Only its read-only migration compatibility remains.
4. TaskState and CodingContextState do not currently duplicate progress ownership; CodingContextState owns rendering/compaction metadata. Historical payload compatibility is still duplicate-shaped data.
5. Ordinary Chat does not create a WorkingMemory. It can avoid TaskState creation unless the exposed state tool is used; removing that schema entirely is a product decision left P2.
6. Automatic retrieval pushes a small top-k likely-relevant set; `recall_memory` is explicit deeper pull. The latter is now deferred instead of always visible.
7. Default Chat currently needs 10 visible tools to preserve tested product behavior; only `tool_search` is runtime-core, while nine are established Chat application capabilities. The previous 14 was unjustified.
8. Global System/Profile/AgentSpec duplication is reduced but not eliminated: AgentSpec owns behavior, while repository mode instructions and eager skill catalog still add static text.
9. With optional memory, RAG, reflection and lifecycle disabled, Core Runtime depends on model routing, sessions, context/history budgeting, the tool registry/executor, trace/cancellation, and the legacy scoped read adapter used by existing Chat tools.
10. The blocker to another 30% Memory deletion is persisted legacy data and tested lifecycle/migration behavior, not code mechanics. It requires deciding data migration, task-local memory retention, and whether candidate/history lifecycle is a supported product capability.
